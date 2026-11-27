"""The writer: one command that turns a story into a narrated, voiced script in the style guide's form.

Usage:
  python -m pipeline.writer write --story STORY --out JOB [--id ID] [--names Willow,Allan] [--seconds 55] [--wpm 250]
                                  [--genre auto] [--retries 2] [--ceiling 0.5] [--no-judge] [--brief FILE]
                                  [--provider anthropic --model M --judge-model M --effort medium] [--style FILE]

STORY is a text file (the first short line is taken as the title) or a JSON file: one record with
"text" (or "selftext") plus optional title, author, url, subreddit, id, created, score, license,
attribution, brief; or a list of such records with --id naming one.

Writes into JOB: story.json (title, lines with a speaker on every line, names, cast with a voice
description for every speaker, sound cues, source), script.md (the same to read), source.json (the
story as given) and script_judge.json (checks, rubric scores and every draft). The model key is read
from the environment; --ceiling is the most this one run may spend, in dollars.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import judge as judge_mod, llm, style as style_mod, textstats

GENRES = ["scary", "rule_world", "wholesome", "comedy", "drama", "mystery"]
MAX_SOURCE_WORDS = 3500
TEXT_KEYS = ("text", "selftext", "body", "story", "content")

SYSTEM = """You write scripts for 45 to 60 second narrated Roblox story shorts: a fast narrator, each speaking character in their own voice, a new staged shot about every second, one-word captions. You adapt a written story into that form. You follow the style guide exactly, because a judge will measure the script against it.

{guide}

OUTPUT
Return one JSON object and nothing else:
{{
 "plan": {{"premise": "the story in one line", "keep": ["the 4 to 7 moments you keep, in order"], "cut": ["what you drop or merge"], "turn": "the turn", "last_line": "the idea of the last line"}},
 "title": "video title, 2 to 6 words, title case",
 "genre": "one of {genres}",
 "lines": [{{"speaker": "narrator", "text": "..."}}, {{"speaker": "mom", "text": "..."}}],
 "cast": [{{"speaker": "mom", "name": "Mom", "role": "who they are in one phrase", "voice": {{"age": "...", "gender": "...", "tone": "...", "processing": "none"}}}}],
 "names": ["every proper name spoken in the script"],
 "sound_cues": [{{"line": 0, "at": "start", "kind": "sfx", "prompt": "what to generate, one plain sentence", "seconds": 1.0}}],
 "music": "one sentence describing the music bed"
}}

FORM RULES
- "lines" is the whole spoken script in order. "narrator" speaks the narration. Every other speaker is a character, with an id in lowercase letters, digits and underscores, and exactly one "cast" entry. Include a cast entry for the narrator too.
- A character whose voice changes (for example the same voice heard up close at the end) may have a second id such as "thing_close" with its own cast entry.
- Text is only the words to be spoken: no quotation marks, no stage directions, no speaker names, no emoji, no dashes used as punctuation. Write numbers as words.
- voice.processing is "none" or an audible treatment such as {processing}.
- "sound_cues": 5 to 10 cues. "line" is the index of the line it belongs to, "at" is "start" or "end", "kind" is one of {kinds}. A "silence" cue marks a beat of quiet before a spoken line or after the last one.
"""

DRAFT = """SOURCE STORY
Title: {title}
Author: {author}

{text}
{brief}
SETTINGS
- Length: {words} words ({low} to {high}). Count them.
- Genre: {genre}
- Names to include: {names}

Write the script now."""

REVISE = """Here is your script and what the judge found. Fix every finding. Change only the lines the findings touch and keep every other line word for word, so nothing that works is lost. Keep the length at {words} words ({low} to {high}).

SCRIPT ({count} words)
{script}

JUDGE FINDINGS, most important first
{fixes}

SOURCE STORY, for reference
Title: {title}

{text}

SETTINGS
- Genre: {genre}
- Names to include: {names}

Return the same JSON object as before, complete."""


class WriterError(ValueError):
    """Raised when the input or the model's script cannot be used."""


class Settings(object):
    """Everything a run can be told: length, pace, names, genre, retries, spend and models."""

    def __init__(self, seconds: Optional[float] = None, wpm: Optional[float] = None, names: Optional[List[str]] = None, genre: Optional[str] = None,
                 retries: int = 2, ceiling: float = 0.5, provider: Optional[str] = None, model: Optional[str] = None, judge_model: Optional[str] = None,
                 style_path: Optional[str] = None, use_judge: bool = True, brief: Optional[str] = None, effort: Optional[str] = "medium") -> None:
        self.seconds = seconds
        self.wpm = wpm
        self.names = list(names or [])
        self.genre = genre if genre and genre != "auto" else None
        self.retries = retries
        self.ceiling = ceiling
        self.provider = provider
        self.model = model
        self.judge_model = judge_model
        self.style_path = style_path
        self.use_judge = use_judge
        self.brief = brief
        self.effort = effort


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "story"


def load_record(path: Path, record_id: Optional[str] = None) -> Dict[str, Any]:
    """A story record from a text file, a JSON record, or one record of a JSON list."""
    if not path.exists():
        raise WriterError("%s not found" % path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".json":
        rows = raw.strip().splitlines()
        title = path.stem.replace("_", " ").replace("-", " ").title()
        if len(rows) > 2 and len(rows[0].split()) <= 14 and not rows[1].strip():
            title, raw = rows[0].strip().lstrip("# ").strip(), "\n".join(rows[2:])
        return {"id": slugify(path.stem), "title": title, "text": raw.strip(), "kind": "file", "file": path.name}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WriterError("%s is not valid JSON: %s" % (path, exc))
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if isinstance(data, list):
        if record_id is None:
            raise WriterError("%s holds %d records; name one with --id" % (path, len(data)))
        matches = [row for row in data if isinstance(row, dict) and str(row.get("id")) == record_id]
        if not matches:
            raise WriterError("no record with id %r in %s" % (record_id, path))
        data = matches[0]
    if not isinstance(data, dict):
        raise WriterError("%s must hold a story record or a list of them" % path)
    record = dict(data)
    for key in TEXT_KEYS:
        if isinstance(record.get(key), str) and record[key].strip():
            record["text"] = record[key].strip()
            break
    else:
        raise WriterError("the story record has no text (looked for %s)" % ", ".join(TEXT_KEYS))
    for key in TEXT_KEYS[1:]:
        record.pop(key, None)
    record.setdefault("title", "Untitled")
    if record.get("created") is None and record.get("created_utc") is not None:
        record["created"] = datetime.datetime.fromtimestamp(int(float(record["created_utc"])), datetime.timezone.utc).strftime("%Y-%m-%d")
    return record


def source_block(record: Dict[str, Any], today: Optional[str] = None) -> Dict[str, Any]:
    """story.json's source: where the story came from, so permission could be sought later."""
    url = record.get("url") or (("https://www.reddit.com" + record["permalink"]) if record.get("permalink") else None)
    subreddit = record.get("subreddit")
    if not url and subreddit and record.get("id"):
        url = "https://www.reddit.com/r/%s/comments/%s/" % (subreddit[2:] if subreddit.startswith("r/") else subreddit, record["id"])
    reddit = bool(subreddit) or bool(url and "reddit.com" in url)
    source = {"kind": record.get("kind") or ("reddit" if reddit else "text")}
    if subreddit:
        source["subreddit"] = subreddit if subreddit.startswith("r/") else "r/" + subreddit
    if url:
        source["url"] = url
    source["title"] = record.get("title")
    if record.get("author"):
        author = str(record["author"])
        source["author"] = author if not reddit or author.startswith("u/") else "u/" + author
    for key in ("id", "created", "file", "license", "license_url", "attribution", "rights", "retrieved_via"):
        if record.get(key):
            source[key] = record[key]
    source["retrieved"] = record.get("retrieved") or today or datetime.date.today().isoformat()
    source["use"] = record.get("use") or "private, not for distribution"
    return source


def clean_text(text: str) -> str:
    """Only the words to be spoken: plain punctuation, no quotes, no stage directions, no stray symbols."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u2026", "...")
    text = re.sub(r"[\"\u201c\u201d]", "", text)
    text = re.sub(r"\s*[\u2014\u2013]\s*|\s+-\s+", ", ", text)
    tokens = []
    for token in text.split():
        if re.search(r"[A-Za-z0-9]", token):
            tokens.append(token)
        elif tokens and re.fullmatch(r"[.!?,;:]+", token):
            tokens[-1] += token
    return re.sub(r"([.!?,;:]),", r"\1", re.sub(r",([.!?,])", r"\1", " ".join(tokens))).strip()


def speaker_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    if not slug:
        raise WriterError("a line has an empty speaker")
    return slug if slug[0].isalpha() else "s_" + slug


def normalise(data: Any) -> Dict[str, Any]:
    """Turn the model's reply into the script half of story.json, or raise WriterError."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list) or not data["lines"]:
        raise WriterError("the model reply has no lines")
    lines = []
    for item in data["lines"]:
        if not isinstance(item, dict):
            raise WriterError("a line is not an object")
        text = clean_text(str(item.get("text") or ""))
        if not text:
            continue
        speaker = speaker_id(item.get("speaker") or "")
        if lines and lines[-1]["speaker"] == speaker:
            lines[-1]["text"] += " " + text
        else:
            lines.append({"speaker": speaker, "text": text})
    if not lines:
        raise WriterError("the model reply has no spoken words")
    speakers = []
    for line in lines:
        if line["speaker"] not in speakers:
            speakers.append(line["speaker"])
    given = {}
    for entry in data.get("cast") or []:
        if isinstance(entry, dict) and entry.get("speaker"):
            given[speaker_id(entry["speaker"])] = entry
    cast = []
    for speaker in speakers:
        entry = given.get(speaker, {})
        voice = entry.get("voice") if isinstance(entry.get("voice"), dict) else {}
        cast.append({"speaker": speaker, "name": str(entry.get("name") or speaker.replace("_", " ").title()), "role": str(entry.get("role") or ""),
                     "voice": {"age": str(voice.get("age") or ""), "gender": str(voice.get("gender") or ""), "tone": str(voice.get("tone") or ""),
                               "processing": str(voice.get("processing") or "none")}})
    cues = []
    for cue in data.get("sound_cues") or []:
        if not isinstance(cue, dict) or not str(cue.get("prompt") or "").strip():
            continue
        try:
            index = int(cue.get("line", 0))
        except (TypeError, ValueError):
            continue
        entry = {"line": max(0, min(len(lines) - 1, index)), "at": "end" if cue.get("at") == "end" else "start",
                 "kind": cue.get("kind") if cue.get("kind") in ("sfx", "ambience", "music", "silence") else "sfx", "prompt": str(cue["prompt"]).strip()}
        if isinstance(cue.get("seconds"), (int, float)):
            entry["seconds"] = round(float(cue["seconds"]), 2)
        cues.append(entry)
    text = textstats.script_text(lines)
    names = []
    for name in list(data.get("names") or []) + [entry["name"] for entry in cast]:
        name = str(name).strip()
        if name and name[0].isupper() and name.lower() != "narrator" and name not in names and re.search(r"\b%s\b" % re.escape(name), text):
            names.append(name)
    genre = data.get("genre") if data.get("genre") in GENRES else None
    return {"title": clean_text(str(data.get("title") or "")) or "Untitled", "genre": genre, "lines": lines, "cast": cast, "names": names,
            "sound_cues": cues, "music": str(data.get("music") or "").strip(), "plan": data.get("plan") if isinstance(data.get("plan"), dict) else {}}


def _source_text(record: Dict[str, Any]) -> str:
    words = record["text"].split()
    if len(words) <= MAX_SOURCE_WORDS:
        return record["text"]
    head = int(MAX_SOURCE_WORDS * 0.6)
    return " ".join(words[:head]) + "\n[... middle omitted for length ...]\n" + " ".join(words[-(MAX_SOURCE_WORDS - head):])


def _names_line(names: List[str]) -> str:
    if not names:
        return "none required"
    return ("%s. Use every one, spelled exactly. Give up to three of them to characters who speak; the rest are names that are seen or called out "
            "(a name tag, a locker, a roll call, a place). The narrator is named only if another character says the name." % ", ".join(names))


def _system(style: Dict[str, Any], genre: str, targets: Dict[str, Any]) -> str:
    return SYSTEM.format(guide=style_mod.render(style, genre, targets), genres=", ".join(GENRES),
                         processing=", ".join('"%s"' % item for item in style["voice_descriptions"]["processing_examples"][1:]),
                         kinds=", ".join(style["sound_cue_kinds"]))


def _bounds(targets: Dict[str, Any]) -> Dict[str, int]:
    return {"words": targets["words"], "low": int(round(targets["words"] * (1 - targets["length_tolerance"]))),
            "high": int(round(targets["words"] * (1 + targets["length_tolerance"])))}


def draft(record: Dict[str, Any], settings: Settings, style: Dict[str, Any], client: llm.Client, ledger: llm.Ledger) -> Dict[str, Any]:
    """First script from the source story."""
    genre = settings.genre or "scary"
    targets = style_mod.targets_for(style, genre, {"seconds": settings.seconds, "wpm": settings.wpm})
    brief = settings.brief or record.get("brief")
    if isinstance(brief, (dict, list)):
        brief = json.dumps(brief, ensure_ascii=False)
    prompt = DRAFT.format(title=record.get("title") or "Untitled", author=record.get("author") or "unknown", text=_source_text(record),
                          brief="\nBRIEF (follow it)\n%s\n" % brief if brief else "",
                          genre=settings.genre or "pick the one that fits; a creepy or horror story is scary", names=_names_line(settings.names), **_bounds(targets))
    reply = client(prompt, system=_system(style, genre, targets), tier="strong", max_tokens=8000, purpose="script draft",
                   provider=settings.provider, model=settings.model, ledger=ledger, effort=settings.effort)
    return normalise(llm.extract_json(reply["text"]))


def revise(script: Dict[str, Any], report: Dict[str, Any], record: Dict[str, Any], settings: Settings, style: Dict[str, Any], genre: str,
           client: llm.Client, ledger: llm.Ledger) -> Dict[str, Any]:
    """One rewrite that answers the judge's findings."""
    targets = style_mod.targets_for(style, genre, {"seconds": settings.seconds, "wpm": settings.wpm})
    prompt = REVISE.format(count=report["measures"]["words"], script=judge_mod.format_script(script["lines"]),
                           fixes="\n".join("- " + fix for fix in report["fixes"]) or "- Tighten it.", title=record.get("title") or "Untitled",
                           text=_source_text(record) if record.get("text") else "(not available)", genre=genre, names=_names_line(settings.names), **_bounds(targets))
    reply = client(prompt, system=_system(style, genre, targets), tier="strong", max_tokens=8000, purpose="script rewrite",
                   provider=settings.provider, model=settings.model, ledger=ledger, effort=settings.effort)
    revised = normalise(llm.extract_json(reply["text"]))
    revised["genre"] = revised["genre"] or genre
    return revised


def _judge(script: Dict[str, Any], record: Dict[str, Any], settings: Settings, style: Dict[str, Any], genre: str, client: Optional[llm.Client],
           ledger: llm.Ledger, previous: Optional[List[str]] = None) -> Dict[str, Any]:
    targets = style_mod.targets_for(style, genre, {"seconds": settings.seconds, "wpm": settings.wpm})
    return judge_mod.judge_story(script, targets, genre, settings.names, record.get("text"), client if settings.use_judge else None, previous or [],
                                 provider=settings.provider, model=settings.judge_model, ledger=ledger)


def _rank(report: Dict[str, Any]) -> Any:
    return (report["summary"]["passed"], report["summary"]["score"])


def improve(script: Dict[str, Any], record: Dict[str, Any], settings: Settings, client: Optional[llm.Client] = None,
            ledger: Optional[llm.Ledger] = None) -> Dict[str, Any]:
    """Judge a script and rewrite it until it passes, the retries run out, or the ceiling is spent. Returns the best version."""
    client = client or llm.complete
    ledger = ledger or llm.run_ledger(settings.ceiling)
    style = style_mod.load(settings.style_path)
    genre = settings.genre or script.get("genre") or (script.get("writer") or {}).get("genre") or "scary"
    history = []
    stopped = "passed"
    try:
        report = _judge(script, record, settings, style, genre, client, ledger)
        history.append({"attempt": len(history), "script": script, "report": report})
        while not report["summary"]["passed"]:
            if len(history) > settings.retries:
                stopped = "retries used"
                break
            script = revise(script, report, record, settings, style, genre, client, ledger)
            report = _judge(script, record, settings, style, genre, client, ledger, [fix for item in history for fix in item["report"]["fixes"]])
            history.append({"attempt": len(history), "script": script, "report": report})
    except llm.BudgetError:
        stopped = "spend ceiling reached"
        if not history:
            report = _judge(script, record, Settings(seconds=settings.seconds, wpm=settings.wpm, names=settings.names, use_judge=False), style, genre, None, ledger)
            history.append({"attempt": 0, "script": script, "report": report})
    best = max(history, key=lambda item: _rank(item["report"]))
    return {"script": best["script"], "report": best["report"], "history": history, "genre": genre, "stopped": stopped, "chosen": best["attempt"],
            "spent_usd": round(ledger.spent() - getattr(ledger, "start", 0.0), 4), "settings": settings}


def write_story(record: Dict[str, Any], settings: Settings, client: Optional[llm.Client] = None, ledger: Optional[llm.Ledger] = None) -> Dict[str, Any]:
    """Draft, judge and rewrite. Returns {"script", "report", "history", "genre", "stopped", "chosen", "spent_usd"}."""
    client = client or llm.complete
    ledger = ledger or llm.run_ledger(settings.ceiling)
    style = style_mod.load(settings.style_path)
    script = draft(record, settings, style, client, ledger)
    return improve(script, record, settings, client, ledger)


def build_story(result: Dict[str, Any], record: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """story.json from a finished script, keeping any production fields an existing story.json already has."""
    script = result["script"]
    settings = result["settings"]
    style = style_mod.load(settings.style_path)
    targets = style_mod.targets_for(style, result["genre"], {"seconds": settings.seconds, "wpm": settings.wpm})
    story = dict(existing or {})
    voice = dict(story.get("voice") or {})
    voice.setdefault("target_wpm", int(targets["wpm"]))
    voice.setdefault("speakers", {})
    story.update({
        "version": 1,
        "slug": story.get("slug") or slugify(script["title"]),
        "title": script["title"],
        "source": story.get("source") if existing and not record.get("text") else source_block(record),
        "names": script["names"],
        "display_overrides": story.get("display_overrides") or {},
        "voice": voice,
        "lines": script["lines"],
        "cast": script["cast"],
        "sound_cues": script["sound_cues"],
        "music": script["music"],
        "writer": {"genre": result["genre"], "seconds": targets["seconds"], "wpm": targets["wpm"], "target_words": targets["words"],
                   "names_required": settings.names, "words": result["report"]["measures"]["words"], "score": result["report"]["summary"]["score"],
                   "passed": result["report"]["summary"]["passed"], "drafts": len(result["history"]), "spent_usd": result["spent_usd"]},
    })
    story["script"] = textstats.script_text(script["lines"])
    return story


def script_markdown(story: Dict[str, Any], report: Dict[str, Any]) -> str:
    measures = report["measures"]
    summary = report["summary"]
    out = ["# %s" % story["title"], "",
           "%d words, about %.0f seconds at %d words a minute. Genre: %s. Judge score %.1f (%s)."
           % (measures["words"], measures["seconds"], story["writer"]["wpm"], story["writer"]["genre"], summary["score"], "pass" if summary["passed"] else "not passed"),
           "", "Source: %s%s%s" % (story["source"].get("title") or "", " by %s" % story["source"]["author"] if story["source"].get("author") else "",
                                   ", %s" % story["source"]["url"] if story["source"].get("url") else ""), "", "## Script", ""]
    cues = {}
    for cue in story.get("sound_cues") or []:
        cues.setdefault((cue["line"], cue["at"]), []).append("[%s: %s]" % (cue["kind"], cue["prompt"]))
    for index, line in enumerate(story["lines"]):
        for cue in cues.get((index, "start"), []):
            out.append("> " + cue)
        out.append("**%s:** %s" % (line["speaker"].upper(), line["text"]))
        for cue in cues.get((index, "end"), []):
            out.append("> " + cue)
        out.append("")
    out += ["## Voices", ""]
    for entry in story.get("cast") or []:
        voice = entry["voice"]
        out.append("- **%s** (%s): %s. Voice: %s, %s, %s; processing: %s." % (entry["speaker"], entry["name"], entry["role"] or "no role given",
                                                                           voice["age"] or "any age", voice["gender"] or "any", voice["tone"] or "neutral", voice["processing"]))
    if story.get("music"):
        out += ["", "Music: " + story["music"]]
    out += ["", "## Judge", "", "| Check | Result | Value | Target |", "|---|---|---|---|"]
    for check in report["checks"]:
        out.append("| %s | %s | %s | %s |" % (check["id"], "ok" if check["ok"] else "FAIL", json.dumps(check["value"]), json.dumps(check["target"])))
    if report.get("rubric"):
        out += ["", "| Rubric | Score | Why |", "|---|---|---|"]
        for key in judge_mod.RUBRIC_KEYS:
            out.append("| %s | %d/5 | %s |" % (key, report["rubric"][key]["score"], report["rubric"][key]["why"].replace("|", "/")))
    return "\n".join(out) + "\n"


def save(job: Path, result: Dict[str, Any], record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Write story.json, script.md, source.json and script_judge.json into the job folder."""
    job.mkdir(parents=True, exist_ok=True)
    existing = None
    if (job / "story.json").exists():
        with open(job / "story.json", "r", encoding="utf-8") as handle:
            existing = json.load(handle)
    story = build_story(result, record or {}, existing)
    with open(job / "story.json", "w", encoding="utf-8") as handle:
        json.dump(story, handle, ensure_ascii=False, indent=1)
    (job / "script.md").write_text(script_markdown(story, result["report"]), encoding="utf-8")
    if record and record.get("text") and not (job / "source.json").exists():
        with open(job / "source.json", "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=1)
    settings = result["settings"]
    with open(job / "script_judge.json", "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "genre": result["genre"], "stopped": result["stopped"], "chosen": result["chosen"], "spent_usd": result["spent_usd"],
                   "settings": {"seconds": story["writer"]["seconds"], "wpm": story["writer"]["wpm"], "names": settings.names, "retries": settings.retries,
                                "ceiling_usd": settings.ceiling},
                   "final": result["report"],
                   "drafts": [{"attempt": item["attempt"], "title": item["script"]["title"], "lines": item["script"]["lines"],
                               "summary": item["report"]["summary"], "measures": item["report"]["measures"],
                               "checks": item["report"]["checks"], "rubric": item["report"]["rubric"], "fixes": item["report"]["fixes"]}
                              for item in result["history"]]}, handle, ensure_ascii=False, indent=1)
    return story


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.writer write", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--story", required=True)
    parser.add_argument("--id")
    parser.add_argument("--out", required=True)
    parser.add_argument("--names", default="")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--wpm", type=float)
    parser.add_argument("--genre", default="auto", choices=["auto"] + GENRES)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--ceiling", type=float, default=0.5)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--brief")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--judge-model")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--style")
    args = parser.parse_args(argv)
    try:
        record = load_record(Path(args.story), args.id)
        brief = Path(args.brief).read_text(encoding="utf-8") if args.brief else None
        settings = Settings(seconds=args.seconds, wpm=args.wpm, names=[name.strip() for name in args.names.split(",") if name.strip()], genre=args.genre,
                            retries=args.retries, ceiling=args.ceiling, provider=args.provider, model=args.model, judge_model=args.judge_model,
                            style_path=args.style, use_judge=not args.no_judge, brief=brief, effort=args.effort)
        result = write_story(record, settings)
        story = save(Path(args.out), result, record)
    except (WriterError, judge_mod.JudgeError, llm.LLMError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("%s: \"%s\", %d drafts, stopped: %s, spent $%.4f" % (Path(args.out) / "story.json", story["title"], len(result["history"]), result["stopped"], result["spent_usd"]))
    judge_mod.print_report(result["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
