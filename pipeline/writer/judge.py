"""Script judge: measurable checks, a model rubric, and one score for a story.json script.

Usage:
  python -m pipeline.writer judge --job JOB [--checks-only] [--rewrite] [--retries 2] [--ceiling 0.5]
                                  [--seconds 55] [--wpm 250] [--names A,B] [--provider P] [--model M]

Reads JOB/story.json (and JOB/source.json when it is there, to judge faithfulness), prints the checks
and scores, and writes JOB/script_judge.json. --checks-only runs the measurable checks alone and
calls no model. --rewrite hands a failing script back to the writer until it passes, the retries run
out, or the spend ceiling is reached, and saves the best version as story.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import llm, style as style_mod, textstats

NARRATOR = "narrator"
SPEAKER = re.compile(r"^[a-z][a-z0-9_]*$")
SAID_TAG = re.compile(r"^(?:he|she|they|it|i|we|mom|dad|mum|grandma|grandpa|the \w+|[A-Z][a-z]+) (?:said|says|whispered|whispers|asked|asks|replied|replies|yelled|shouted|screamed|answered|muttered)\b[^.!?]{0,20}[.!?,]")
QUOTES = re.compile(r"[\"“”]")
RUBRIC_KEYS = ["premise_clear", "filmable", "dialogue_spoken", "escalation", "ending_lands", "tone_safe", "faithful"]

RUBRIC_PROMPT = """You are the script judge for 45 to 60 second narrated Roblox story shorts. A fast narrator reads the script, each named character's lines are spoken in that character's own voice, and a new staged shot appears about every second. Judge the script below strictly; a 5 is rare.

GENRE: {genre}
{names_block}{source_block}
SCRIPT (speaker: text), {words} words:
{script}

Score each item from 1 to 5. Use these anchors and do not drift from them:
1 = broken, 2 = a clear fault a viewer would notice, 3 = works but has one fault you can name, 4 = ready to film as it is, 5 = as good as the best shorts in the genre.
Give a 4 unless you can quote the words that are at fault. Taste alone ("could be punchier") is not a fault.
- "premise_clear": after the first 5 seconds (about 20 words) can a viewer who knows nothing say what this story is about? Give "by_word": the count of words by which it is clear.
- "filmable": is every sentence something a camera can show or a microphone can record (an action, an object, a place, a spoken line)? List up to 4 sentences that are not, in "weak".
- "dialogue_spoken": do the spoken lines sound like people talking (short, contractions, no explaining for the viewer's sake), and does each one earn its place? List up to 4 stiff lines in "weak".
- "escalation": does each beat raise the stakes, with the turn late and no slack in the middle?
- "ending_lands": does the last line land a twist, a cliffhanger or a return to the opening, in few words, with no moral, summary or call to action?
- "tone_safe": {tone_question}
- "faithful": {faithful_question}

Return one JSON object and nothing else:
{{"premise_clear": {{"score": n, "by_word": n, "why": "..."}}, "filmable": {{"score": n, "weak": ["..."], "why": "..."}}, "dialogue_spoken": {{"score": n, "weak": ["..."], "why": "..."}}, "escalation": {{"score": n, "why": "..."}}, "ending_lands": {{"score": n, "why": "..."}}, "tone_safe": {{"score": n, "why": "..."}}, "faithful": {{"score": n, "why": "..."}}, "fixes": ["one concrete edit for each item scored under 4, most important first, naming the line number and the new wording; an empty list when every item is 4 or 5"]}}
Keep every "why" under 25 words. Leave "weak" empty for an item you score 4 or 5. In "fixes", never ask for anything the script already does, never ask to undo a change listed under EARLIER FINDINGS, never ask for the source's names back when REQUIRED NAMES are given, and never ask for more words than the script has room for.
{previous_block}"""


class JudgeError(ValueError):
    """Raised when a script cannot be judged."""


def _check(key: str, ok: bool, value: Any, target: Any, fix: str) -> Dict[str, Any]:
    return {"id": key, "ok": bool(ok), "value": value, "target": target, "fix": "" if ok else fix}


def check_script(story: Dict[str, Any], targets: Dict[str, Any], names: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The measurable checks. Every entry has id, ok, value, target and, when it fails, a fix instruction."""
    lines = story.get("lines") or []
    if not lines or any(not isinstance(line, dict) or not str(line.get("speaker", "")).strip() or not str(line.get("text", "")).strip() for line in lines):
        raise JudgeError("story.json: lines must be a non-empty list of {speaker, text}")
    stats = textstats.measure_lines(lines, targets["wpm"], NARRATOR)
    text = textstats.script_text(lines)
    words = stats["words"]
    low = int(round(targets["words"] * (1 - targets["length_tolerance"])))
    high = int(round(targets["words"] * (1 + targets["length_tolerance"])))
    checks = []
    checks.append(_check("length", low <= words <= high, words, [low, high],
                         "The script is %d words; make it %d to %d (%s %d words)." % (words, low, high, "cut" if words > high else "add", abs(words - targets["words"]))))
    checks.append(_check("hook", stats["first_sentence_words"] <= targets["hook_max_words"], stats["first_sentence_words"], targets["hook_max_words"],
                         "The first sentence is %d words; say the hook in %d or fewer." % (stats["first_sentence_words"], targets["hook_max_words"])))
    mean_low, mean_high = targets["sentence_mean_words"]
    checks.append(_check("sentence_mean", mean_low <= stats["sentence_mean"] <= mean_high, stats["sentence_mean"], [mean_low, mean_high],
                         "Mean sentence length is %.1f words; bring it to %s-%s by %s." % (stats["sentence_mean"], mean_low, mean_high,
                                                                                         "splitting long sentences" if stats["sentence_mean"] > mean_high else "joining fragments")))
    longest = max(textstats.sentences(text), key=lambda part: len(textstats.words(part)))
    checks.append(_check("sentence_max", stats["sentence_max"] <= targets["sentence_max_words"], stats["sentence_max"], targets["sentence_max_words"],
                         "Split this %d-word sentence: \"%s\"" % (stats["sentence_max"], longest)))
    checks.append(_check("short_sentences", stats["short_sentence_share"] >= targets["short_sentence_share_min"], stats["short_sentence_share"],
                         targets["short_sentence_share_min"], "Too few short sentences; make more of them 5 words or fewer."))
    share_low, share_high = targets["dialogue_share"]
    checks.append(_check("dialogue_share", share_low <= stats["dialogue_share"] <= share_high, stats["dialogue_share"], [share_low, share_high],
                         "Spoken dialogue is %d%% of the words; bring it to %d%%-%d%% by %s." % (round(stats["dialogue_share"] * 100), share_low * 100, share_high * 100,
                                                                                               "turning narrated speech into spoken lines" if stats["dialogue_share"] < share_low else "narrating more of the action")))
    checks.append(_check("dialogue_lines", stats["dialogue_lines"] >= targets["dialogue_lines_min"], stats["dialogue_lines"], targets["dialogue_lines_min"],
                         "Only %d spoken lines; give characters at least %d." % (stats["dialogue_lines"], targets["dialogue_lines_min"])))
    checks.append(_check("dialogue_line_max", stats["dialogue_line_max"] <= targets["dialogue_line_max_words"], stats["dialogue_line_max"],
                         targets["dialogue_line_max_words"], "A spoken line runs %d words; keep each under %d." % (stats["dialogue_line_max"], targets["dialogue_line_max_words"])))
    first = stats["first_dialogue_word"]
    first_share = round(first / float(words), 3) if first is not None and words else None
    checks.append(_check("first_dialogue", first_share is not None and first_share <= targets["first_dialogue_by_share"], first_share, targets["first_dialogue_by_share"],
                         "The first spoken line comes too late; put one in the first third."))
    speakers = stats["speakers"]
    checks.append(_check("speakers", 2 <= len(speakers) <= targets["speakers_max"] and NARRATOR in speakers and all(SPEAKER.match(name) for name in speakers),
                         speakers, [2, targets["speakers_max"]],
                         "Use a narrator plus one to %d speaking characters, with ids in lowercase letters, digits and underscores." % (targets["speakers_max"] - 1)))
    quoted = [line["text"] for line in lines if line["speaker"] == NARRATOR and QUOTES.search(line["text"])]
    checks.append(_check("narrator_quotes", not quoted, len(quoted), 0,
                         "The narrator quotes speech in %d line(s); move the quoted words to a line spoken by the character: \"%s\"" % (len(quoted), quoted[0] if quoted else "")))
    tagged = [lines[index]["text"] for index in range(1, len(lines))
              if lines[index]["speaker"] == NARRATOR and lines[index - 1]["speaker"] != NARRATOR and SAID_TAG.match(lines[index]["text"])]
    checks.append(_check("speech_tags", not tagged, len(tagged), 0,
                         "Do not follow a spoken line with a tag such as 'she said': \"%s\"" % (tagged[0] if tagged else "")))
    checks.append(_check("last_line", stats["last_sentence_words"] <= targets["last_line_max_words"], stats["last_sentence_words"], targets["last_line_max_words"],
                         "The last sentence is %d words; land it in %d or fewer." % (stats["last_sentence_words"], targets["last_line_max_words"])))
    checks.append(_check("filler", not stats["filler"], stats["filler"], [], "Remove filler: %s." % ", ".join(sorted(set(stats["filler"])))))
    checks.append(_check("gore", not stats["gore"], stats["gore"], [], "Remove gore words (%s); imply the threat instead." % ", ".join(sorted(set(stats["gore"])))))
    missing = [name for name in names if not re.search(r"\b%s\b" % re.escape(name), text, re.I)]
    checks.append(_check("names", not missing, missing, [], "Work these names in as characters or places: %s." % ", ".join(missing)))
    checks.append(_check("grade", stats["grade"] <= targets["grade_max"], stats["grade"], targets["grade_max"],
                         "Reading grade is %.1f; use shorter words and sentences (target %.1f or lower)." % (stats["grade"], targets["grade_max"])))
    cast = {entry.get("speaker") for entry in story.get("cast") or [] if isinstance(entry, dict)}
    uncast = [name for name in speakers if name != NARRATOR and name not in cast]
    checks.append(_check("cast", not uncast, uncast, [], "Add a cast entry with a voice description for: %s." % ", ".join(uncast)))
    return checks


def format_script(lines: Sequence[Dict[str, Any]]) -> str:
    return "\n".join("%d. %s: %s" % (index, line["speaker"], line["text"]) for index, line in enumerate(lines))


def rubric(story: Dict[str, Any], genre: str, source_text: Optional[str], client: llm.Client, names: Sequence[str] = (),
           previous: Sequence[str] = (), **options: Any) -> Dict[str, Any]:
    """The model rubric: seven scores from 1 to 5 with reasons and up to five fixes."""
    scary = genre == "scary"
    names_block = ""
    if names:
        names_block = ("REQUIRED NAMES: the producer asked for these names to be used for characters or places: %s. Renaming the source's people and places "
                       "to them is correct and is not a loss of faithfulness.\n" % ", ".join(names))
    previous_block = ""
    if previous:
        previous_block = "\nEARLIER FINDINGS (already acted on by the writer):\n%s\n" % "\n".join("- " + item for item in previous)
    source_block = ""
    if source_text:
        source_words = source_text.split()
        source_block = "\nSOURCE STORY (what the script adapts):\n%s\n" % " ".join(source_words[:1800])
    prompt = RUBRIC_PROMPT.format(
        genre=genre, source_block=source_block, names_block=names_block, previous_block=previous_block, words=len(textstats.words(textstats.script_text(story["lines"]))),
        script=format_script(story["lines"]),
        tone_question=("is it unsettling without gore: no blood, wounds, bodies or pain described, the threat implied through sound, voice and wrong details, safe for a young audience?"
                       if scary else "does it hold one clear tone, and is it safe for a young audience (no gore, slurs or sexual content)?"),
        faithful_question=("does it keep the source's premise, its strongest image and its ending, without inventing a different story?"
                           if source_text else "is the story self-consistent, with nothing set up and dropped? (No source was given.)"))
    reply = client(prompt, tier="strong", max_tokens=4000, purpose="script rubric", effort="low", **options)
    data = llm.extract_json(reply["text"])
    if not isinstance(data, dict):
        raise llm.LLMError("the rubric reply is not an object")
    result = {"model": reply["model"], "cost_usd": reply["cost_usd"], "fixes": [str(item) for item in data.get("fixes") or []][:5]}
    for key in RUBRIC_KEYS:
        item = data.get(key) if isinstance(data.get(key), dict) else {}
        try:
            value = int(round(float(item.get("score"))))
        except (TypeError, ValueError):
            raise llm.LLMError("the rubric reply has no score for %s" % key)
        entry = {"score": max(1, min(5, value)), "why": str(item.get("why") or "")}
        if item.get("weak"):
            entry["weak"] = [str(text) for text in item["weak"]][:4]
        if key == "premise_clear" and item.get("by_word") is not None:
            entry["by_word"] = item["by_word"]
        result[key] = entry
    return result


def summarise(checks: List[Dict[str, Any]], scores: Optional[Dict[str, Any]], targets: Dict[str, Any]) -> Dict[str, Any]:
    """One 0-100 score and a pass flag from the checks and, when it ran, the rubric."""
    passed_checks = sum(1 for check in checks if check["ok"])
    fraction = passed_checks / float(len(checks))
    summary = {"checks_passed": passed_checks, "checks_total": len(checks), "failed": [check["id"] for check in checks if not check["ok"]]}
    if scores is None:
        summary.update({"rubric_mean": None, "rubric_low": [], "score": round(100 * fraction, 1), "passed": passed_checks == len(checks)})
        return summary
    values = [scores[key]["score"] for key in RUBRIC_KEYS]
    mean = sum(values) / float(len(values))
    low = [key for key in RUBRIC_KEYS if scores[key]["score"] < targets["rubric_floor"]]
    summary.update({"rubric_mean": round(mean, 2), "rubric_low": low, "score": round(50 * fraction + 50 * (mean - 1) / 4.0, 1),
                    "passed": passed_checks == len(checks) and not low and mean >= targets["rubric_pass"]})
    return summary


def judge_story(story: Dict[str, Any], targets: Dict[str, Any], genre: str, names: Sequence[str] = (), source_text: Optional[str] = None,
                client: Optional[llm.Client] = None, previous: Sequence[str] = (), **options: Any) -> Dict[str, Any]:
    """Checks plus, when a client is given, the rubric. Returns {"measures", "checks", "rubric", "summary", "fixes"}.

    The script passes when every check is ok, the rubric mean reaches targets["rubric_pass"] and no rubric
    item is under targets["rubric_floor"]. previous is the list of fixes already asked for, so the rubric does not reverse itself.
    """
    checks = check_script(story, targets, names)
    scores = rubric(story, genre, source_text, client, names, previous, **options) if client is not None else None
    fixes = [check["fix"] for check in checks if not check["ok"]]
    if scores is not None:
        fixes += scores["fixes"]
        for key, label in (("filmable", "Not filmable, show it as an action or cut it"), ("dialogue_spoken", "Stiff spoken line, make it sound said aloud")):
            if scores[key]["score"] < targets["rubric_pass"]:
                fixes += ["%s: \"%s\"" % (label, text) for text in scores[key].get("weak", []) if not any(text in fix for fix in fixes)]
    return {"measures": textstats.measure_lines(story["lines"], targets["wpm"], NARRATOR), "checks": checks, "rubric": scores,
            "summary": summarise(checks, scores, targets), "fixes": fixes}


def print_report(report: Dict[str, Any]) -> None:
    summary = report["summary"]
    measures = report["measures"]
    print("%d words, %.1f s, %d lines, %d speakers, dialogue %d%%, mean sentence %.1f words"
          % (measures["words"], measures["seconds"], measures["lines"], len(measures["speakers"]), round(measures["dialogue_share"] * 100), measures["sentence_mean"]))
    for check in report["checks"]:
        print("  %s %-17s %s (target %s)" % ("ok  " if check["ok"] else "FAIL", check["id"], json.dumps(check["value"]), json.dumps(check["target"])))
    if report["rubric"]:
        for key in RUBRIC_KEYS:
            print("  %d/5 %-16s %s" % (report["rubric"][key]["score"], key, report["rubric"][key]["why"]))
    print("score %.1f, %s (%d of %d checks%s)" % (summary["score"], "PASS" if summary["passed"] else "FAIL", summary["checks_passed"], summary["checks_total"],
                                                 ", rubric mean %.2f" % summary["rubric_mean"] if summary["rubric_mean"] is not None else ""))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.writer judge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True)
    parser.add_argument("--checks-only", action="store_true")
    parser.add_argument("--rewrite", action="store_true")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--ceiling", type=float, default=0.5)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--wpm", type=float)
    parser.add_argument("--names", default="")
    parser.add_argument("--genre")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--judge-model")
    parser.add_argument("--style")
    args = parser.parse_args(argv)
    job = Path(args.job)
    try:
        with open(job / "story.json", "r", encoding="utf-8") as handle:
            story = json.load(handle)
    except (OSError, ValueError) as exc:
        print("error: cannot read %s: %s" % (job / "story.json", exc), file=sys.stderr)
        return 2
    source = None
    if (job / "source.json").exists():
        with open(job / "source.json", "r", encoding="utf-8") as handle:
            source = json.load(handle)
    writer_meta = story.get("writer") or {}
    genre = args.genre or writer_meta.get("genre") or "scary"
    names = [name.strip() for name in args.names.split(",") if name.strip()] or writer_meta.get("names_required") or []
    style = style_mod.load(args.style)
    targets = style_mod.targets_for(style, genre, {"seconds": args.seconds or writer_meta.get("seconds"), "wpm": args.wpm or writer_meta.get("wpm")})
    try:
        if args.rewrite:
            from . import write
            settings = write.Settings(seconds=targets["seconds"], wpm=targets["wpm"], names=names, genre=genre, retries=args.retries, ceiling=args.ceiling,
                                      provider=args.provider, model=args.model, judge_model=args.judge_model, style_path=args.style)
            result = write.improve(story, source or {}, settings)
            write.save(job, result, source)
            report = result["report"]
        else:
            ledger = llm.run_ledger(args.ceiling)
            client = None if args.checks_only else llm.complete
            report = judge_story(story, targets, genre, names, (source or {}).get("text"), client, provider=args.provider, model=args.judge_model, ledger=ledger)
            with open(job / "script_judge.json", "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "genre": genre, "targets": targets, "final": report}, handle, ensure_ascii=False, indent=1)
        print_report(report)
        return 0 if report["summary"]["passed"] else 1
    except (JudgeError, llm.LLMError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
