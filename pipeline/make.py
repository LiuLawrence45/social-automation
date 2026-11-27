"""One entry point: a story in, final.mp4 and a quality report out, every stage in order, resumable.

  python -m pipeline.make --story FILE --out JOB [--narrator ID] [--from STAGE] [--to STAGE] [--budget USD]
                          [--dry-run] [--offline] [--writer DIR] [--fix-rounds N] [--accept all|a,b --reason TEXT]
  python -m pipeline.make --out JOB --status

Stages, in order (code unless marked AGENT):
  script    story.json in the job: copied when FILE is already a story.json, written by pipeline.writer (called from its
            own checkout, --writer or $MAKE_WRITER) when FILE is a story text or record; the house defaults are added
  cast      pipeline.cast: the narrator's and every speaker's voice, the members' avatars (cast.json)
  voice     pipeline.voice: narration.wav, words.json
  shots     AGENT: sets and shotlist.json. Stops with REQUEST-shots.md (or REQUEST-set-<name>.md for a set that does
            not exist yet); when the shot list is there it sets the members' avatars and builds timeline.json
  checks    pipeline.checks: the defaults, before Studio. A failing rule stops with REQUEST-checks.md (AGENT fixes shots)
  sound     sound.json (written from the story's cues when missing), pipeline.sound: bed.wav
  camera    pipeline.judge shots: a camera per shot (Studio, vision key). A head in the caption band stops with REQUEST-camera.md
  posture   pipeline.judge posture --no-vision: geometry in Studio. Failing shots stop with REQUEST-posture.md (AGENT)
  render    pipeline.render video: video.mp4 (Studio window on screen)
  edit      pipeline.edit: final.mp4
  quality   pipeline.quality: quality.json, QUALITY.md
  fix       AGENT decides a fix for each failing shot: REQUEST-fix-<round>.md; rerun after editing and the changed
            shots go through shots, checks, camera, posture, render, edit and quality again. Ends when no shot fails,
            the fix rounds run out, the spend ceiling is reached or the remaining fails are accepted (--accept)
  final     final.mp4 and QUALITY.md are in place; MAKE.md is the run's table of stages, time and spend

A stage is skipped when it is done and what it reads has not changed; --from runs that stage and every later
one again; --to stops after a stage. State is <job>/make.json: per stage its status, the hashes of its inputs,
its outputs, wall-clock seconds and model spend. Exit code 0: finished (or reached --to); 3: stopped for an agent,
the request file is printed; 1: a stage failed, its log is under <job>/logs/.

Spend is one ledger for the whole job, <job>/quality/ledger.json, the file the quality stage already counts
against its ceiling: every paid stage adds a run to it, and no paid stage starts once the total reaches --budget
(default $8, or story.json make.ceiling_usd). Voice and sound are billed in ElevenLabs credits; their dollars are
credits times make.prices.elevenlabs_usd_per_1k_credits (default 0.30), an estimate, the token spends are measured.

--dry-run prints what each stage would do and runs nothing: no Studio, no key, no network, no file written.
--offline uses the say voice, synthetic sound and geometry-only cameras and skips the quality judges, so only
Studio is needed. Keys are read from the environment by the stage that uses them; this program checks only
that the variable is set, by name.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import cast as cast_mod
from . import checks as checks_mod
from . import story as story_mod

STAGES = ("script", "cast", "voice", "shots", "checks", "sound", "camera", "posture", "render", "edit", "quality", "fix", "final")
AGENT_STAGES = ("shots", "checks", "camera", "posture", "fix")
CAPTION_BAND = (0.60, 0.78)
DEFAULTS = {
    "judge": {"provider": "anthropic", "model": "claude-haiku-5-5", "max_candidates": 5, "retries": 1, "posture": True,
              "prices": {"gemini-3.8-flash": [0.75, 3.75], "claude-haiku-5-5": [1, 5], "claude-sonnet-5-5": [3, 15]},
              "variety": {"cap": 0.2, "slack": 0.02, "min_moving": 0.5}},
    "mix": {"music_offset_lu": 7.5},
    "quality": {"accepted": ["The cast mixes detailed avatar styles on purpose (cartoon, semi-realistic, anime, faceted): each lead keeps its own style, "
                             "and that mix is not a consistency or style defect. A default blocky classic avatar beside detailed ones is still a defect."]},
}
SETTINGS = {"ceiling_usd": 8.0, "fix_rounds": 3, "camera_ceiling_usd": 2.0, "writer_ceiling_usd": 0.5, "writer_seconds": None,
            "quality": {"provider": "gemini", "model": "gemini-3.8-flash"},
            "prices": {"elevenlabs_usd_per_1k_credits": 0.30}}
MUSIC = {"scary": "Slow, uneasy horror film underscore for a short story told at night: a low sustained drone, sparse detuned notes far apart, "
                  "tension that builds toward the end and stops dead on the final note.",
         "default": "Light, curious underscore for a short story: soft plucked notes over a warm pad, unhurried, a small lift at the end."}
KEYS = {"elevenlabs": "ELEVENLABS_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
Runner = Callable[[List[str], Path, Dict[str, str], Path], int]


class Stop(Exception):
    """A stage needs an agent; the request file says what, with what, and how to tell it is done."""

    def __init__(self, request: Path, summary: str) -> None:
        super().__init__(summary)
        self.request = request


class Failed(Exception):
    """A stage failed and rerunning without a change will fail again."""


def run_command(command: List[str], cwd: Path, env: Dict[str, str], log: Path) -> int:
    """Run one stage command, appending its output to the stage log."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\n$ %s\n" % " ".join(command))
        handle.flush()
        return subprocess.call(command, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    partial.replace(path)


def digest(job: Path, spec: str) -> Optional[str]:
    """Hash of one input: a file, a folder ("sets/"), or some keys of a JSON file ("story.json#lines,voice")."""
    name, _, keys = spec.partition("#")
    path = Path(name) if os.path.isabs(name) else job / name
    if name.endswith("/"):
        if not path.is_dir():
            return None
        found = hashlib.sha1()
        for item in sorted(path.iterdir()):
            if item.is_file():
                found.update(item.name.encode("utf-8"))
                found.update(item.read_bytes())
        return found.hexdigest()[:16]
    if not path.is_file():
        return None
    if keys:
        data = read_json(path, {})
        subset = {key: data.get(key) for key in keys.split(",")} if isinstance(data, dict) else data
        return hashlib.sha1(json.dumps(subset, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    found = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            found.update(block)
    return found.hexdigest()[:16]


def ledger_total(job: Path) -> float:
    runs = (read_json(job / "quality" / "ledger.json", {}) or {}).get("runs") or []
    return round(sum(float(run.get("cost_usd", 0.0) or 0.0) for run in runs), 6)


def ledger_add(job: Path, stage: str, cost_usd: float, detail: Dict[str, Any]) -> None:
    """Add one stage's spend to the job's ledger, the file the quality stage counts against its ceiling."""
    path = job / "quality" / "ledger.json"
    ledger = read_json(path, None) or {"runs": []}
    ledger["runs"].append(dict(detail, stage=stage, by="pipeline.make", cost_usd=round(float(cost_usd), 6), at=time.strftime("%Y-%m-%dT%H:%M:%S")))
    write_json(path, ledger)


def is_story(path: Path) -> bool:
    data = read_json(path)
    return isinstance(data, dict) and data.get("version") == 1 and bool(data.get("lines") or data.get("script"))


def draft_shots(story: Dict[str, Any], words: Dict[str, Any], longest: float = 2.6) -> Dict[str, Any]:
    """A shot list skeleton: the script cut at sentence and clause ends into shots no longer than a few seconds.

    Only the words are decided, so every shot's text matches the script exactly; set, cast and camera are the agent's."""
    tokens = story_mod.tokenize(story["script"])
    timed = words["words"]
    speakers = [word.get("speaker") for word in timed]
    shots, first = [], 0
    for index, token in enumerate(tokens):
        last = index == len(tokens) - 1
        length = float(timed[index]["end"]) - float(timed[first]["start"])
        sentence = story_mod.SENTENCE_END.search(token) is not None and token.lower() not in story_mod.ABBREVIATIONS
        clause = token.rstrip("\"')]").endswith((",", ";", ":", "\u2014")) and length >= 1.0
        turn = not last and speakers[index + 1] != speakers[index]
        if last or sentence or turn or clause or length >= longest:
            shots.append({"id": "s%02d" % (len(shots) + 1), "text": " ".join(tokens[first:index + 1]), "speaker": speakers[first],
                          "seconds": round(float(timed[index]["end"]) - float(timed[first]["start"]), 2), "set": "", "cast": [], "camera": {}})
            first = index + 1
    return {"version": 1, "draft": "from pipeline.make: the words are cut for you; fill set, cast and camera, drop speaker and seconds, save as shotlist.json",
            "cast": {}, "shots": shots}


def sound_from_story(story: Dict[str, Any]) -> Dict[str, Any]:
    """sound.json from a writer story's sound_cues and music, placed by word; a quiet score when the story names none."""
    lines = story_mod.story_lines(story)
    starts, cursor = [], 0
    for line in lines:
        starts.append(cursor)
        cursor += len(story_mod.tokenize(line["text"]))
    genre = (story.get("writer") or {}).get("genre") or story.get("genre") or "default"
    prompt = story.get("music") if isinstance(story.get("music"), str) and story["music"].strip() else MUSIC.get(genre, MUSIC["default"])
    out = {"normalise": True, "prompt_influence": 0.4,
           "music": {"prompt": prompt.strip() + " Instrumental only, no vocals.", "gain_db": 0, "duck_db": -3.5, "fallback": "sad" if genre == "scary" else "warm",
                     "fade_in": 0.6, "fade_out": 0.05, "cuts": []},
           "ambience": [], "cues": []}  # type: Dict[str, Any]
    for index, cue in enumerate(story.get("sound_cues") or []):
        if not isinstance(cue, dict) or not isinstance(cue.get("prompt"), str) or not isinstance(cue.get("line"), int) or not 0 <= cue["line"] < len(lines):
            continue
        first = starts[cue["line"]]
        word = first if cue.get("at") != "end" else first + len(story_mod.tokenize(lines[cue["line"]]["text"])) - 1
        offset = 0.0 if cue.get("at") != "end" else 0.3
        seconds = float(cue.get("seconds") or 2.0)
        if cue.get("kind") == "ambience":
            out["ambience"].append({"name": "amb%d" % (len(out["ambience"]) + 1), "prompt": cue["prompt"], "seconds": 12, "gain_db": -12, "fade": 0.8,
                                    "from": {"word": word, "offset": 0}})
        elif cue.get("kind") == "silence":
            out["music"]["cuts"].append({"at": {"word": word, "offset": offset}, "seconds": seconds})
        else:
            out["cues"].append({"name": "cue%d" % (index + 1), "prompt": cue["prompt"], "seconds": seconds, "gain_db": -4, "at": {"word": word, "offset": offset}})
    return out


class Make:
    def __init__(self, job: Path, options: Dict[str, Any], runner: Optional[Runner] = None, out: Callable[[str], None] = print) -> None:
        self.job = Path(job).expanduser().resolve()
        self.options = options
        self.runner = runner or run_command
        self.say = out
        self.repo = Path(__file__).resolve().parent.parent
        self.state_path = self.job / "make.json"
        self.state = read_json(self.state_path, None) or {"version": 1, "stages": {}, "fix": {"round": 0, "accepted": [], "reason": None}}
        self.prefix = os.environ.get("MAKE_RUN", "python -m")

    def settings(self) -> Dict[str, Any]:
        story = read_json(self.job / "story.json", {}) or {}
        merged = cast_mod.merge(SETTINGS, story.get("make") or {})
        if self.options.get("budget") is not None:
            merged["ceiling_usd"] = float(self.options["budget"])
        if self.options.get("fix_rounds") is not None:
            merged["fix_rounds"] = int(self.options["fix_rounds"])
        return merged

    def story(self) -> Dict[str, Any]:
        return story_mod.validate_story(story_mod.load_json(self.job / "story.json"))

    def remaining(self) -> float:
        return round(float(self.settings()["ceiling_usd"]) - ledger_total(self.job), 6)

    def need_key(self, stage: str, provider: str) -> None:
        name = KEYS.get(provider)
        if name and not os.environ.get(name):
            raise Failed("%s is not set in the environment (stage %s needs it; only its name is checked here). Start this program from a shell that has it, "
                         "for example zsh -ic '...' on the Mac, or pass --offline" % (name, stage))

    def need_budget(self, stage: str) -> float:
        left = self.remaining()
        if left <= 0:
            raise Failed("the spend ceiling of $%.2f is reached ($%.4f spent); stage %s was not started. Raise --budget to continue"
                         % (float(self.settings()["ceiling_usd"]), ledger_total(self.job), stage))
        return left

    def command(self, stage: str, module: str, *args: str, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, ok: Sequence[int] = (0,)) -> int:
        full = [sys.executable, "-m", module] + [str(arg) for arg in args]
        merged = dict(os.environ)
        merged.setdefault("STUDIO_HOLDER", self.options.get("holder") or "make:%s" % self.job.name)
        merged.update(env or {})
        code = self.runner(full, cwd or self.repo, merged, self.job / "logs" / ("%s.log" % stage))
        if code not in ok:
            tail = ""
            try:
                tail = "\n".join((self.job / "logs" / ("%s.log" % stage)).read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
            except OSError:
                pass
            raise Failed("%s exited with code %d (python -m %s %s); the end of logs/%s.log:\n%s" % (stage, code, module, " ".join(str(arg) for arg in args), stage, tail))
        return code

    def request(self, name: str, title: str, needed: str, inputs: Sequence[str], commands: Sequence[str], done: str, detail: str = "") -> Path:
        path = self.job / name
        lines = ["# %s" % title, "", "Job: %s" % self.job, "Written by pipeline.make at %s. Do the work, then run the last command; the program picks up where it stopped." % time.strftime("%Y-%m-%d %H:%M:%S"),
                 "The full procedure is in RUNBOOK.md at the repository root.", "", "## What is needed", "", needed.strip(), ""]
        if detail.strip():
            lines += ["## Findings", "", detail.strip(), ""]
        fill = lambda text: text.replace("{run}", self.prefix).replace("{job}", str(self.job))
        lines += ["## Inputs", ""] + ["- %s" % fill(item) for item in inputs] + ["", "## Commands", ""]
        lines += ["    %s" % fill(line) for line in commands]
        lines += ["    %s pipeline.make --out %s" % (self.prefix, self.job), "", "## Done when", "", done.strip(), ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def clear_requests(self, prefix: str) -> None:
        for path in self.job.glob("%s*.md" % prefix):
            target = self.job / "requests_done"
            target.mkdir(exist_ok=True)
            path.replace(target / ("%s-%s" % (time.strftime("%H%M%S"), path.name)))

    def inputs(self, stage: str) -> List[str]:
        registry = str(cast_mod.REGISTRY)
        table = {
            "script": [self.state.get("source") or "story.json#lines,script"],
            "cast": ["story.json#lines,cast,casting", registry],
            "voice": ["story.json#lines,script,voice,names,display_overrides"],
            "shots": ["shotlist.json", "words.json", "cast.json#avatars", "sets/", "poses.json"],
            "checks": ["shotlist.json", "timeline.json", "story.json#checks,judge", "cast.json#members,avatars", "poses.json", "sets/"],
            "sound": ["sound.json", "words.json", "timeline.json#duration", "story.json#sound_cues,music"],
            "camera": ["timeline.json", "story.json#judge", "sets/", "poses.json"],
            "posture": ["timeline.json", "judge.json#shots", "sets/", "poses.json"],
            "render": ["timeline.json", "judge.json#shots", "sets/", "poses.json"],
            "edit": ["video.mp4", "narration.wav", "words.json", "shots.json", "bed.wav", "story.json#mix"],
            "quality": ["video.mp4", "final.mp4", "story.json#quality", "posture.json"],
            "fix": ["quality.json#summary"],
            "final": ["final.mp4", "QUALITY.md"],
        }
        return table[stage]

    def hashes(self, stage: str) -> Dict[str, Optional[str]]:
        found = {spec: digest(self.job, spec) for spec in self.inputs(stage)}
        if stage in ("camera", "posture", "render") and not self.options.get("dry_run"):
            found["director"] = self.director()
        if stage == "cast":
            found["narrator"] = self.options.get("narrator")
        if stage in ("voice", "sound", "camera", "quality"):
            found["offline"] = "1" if self.options.get("offline") else None
        return found

    def director(self) -> Optional[str]:
        try:
            from . import studio
            studio.use_job(self.job)
            return studio.director_hash()
        except Exception:
            return None

    def outputs(self, stage: str) -> List[str]:
        return {"script": ["story.json"], "cast": ["cast.json"], "voice": ["narration.wav", "words.json"], "shots": ["shotlist.json", "timeline.json"],
                "checks": ["checks.json"], "sound": ["bed.wav"], "camera": ["judge.json"], "posture": [], "render": ["video.mp4", "shots.json"],
                "edit": ["final.mp4"], "quality": [], "fix": [], "final": ["final.mp4", "MAKE.md"]}[stage]

    def fresh(self, stage: str) -> bool:
        """Whether a stage is done, its outputs exist and nothing it reads has changed."""
        record = self.state["stages"].get(stage) or {}
        if record.get("status") not in ("done", "skipped"):
            return False
        if any(not (self.job / name).exists() for name in self.outputs(stage)):
            return False
        return record.get("inputs") == self.hashes(stage)

    def plan(self) -> List[Dict[str, Any]]:
        first, last = self.options.get("from"), self.options.get("to")
        rows, forced, upstream = [], False, False
        for stage in STAGES:
            forced = forced or stage == first
            fresh = self.fresh(stage)
            run = forced or upstream or not fresh
            upstream = upstream or (run and stage not in ("fix",))
            rows.append({"stage": stage, "run": run, "why": "forced by --from" if forced else "first run or its inputs changed" if not fresh else "an earlier stage runs" if run else "done, inputs unchanged"})
            if stage == last:
                break
        return rows

    def run(self) -> int:
        self.job.mkdir(parents=True, exist_ok=True)
        if self.options.get("accept"):
            self.state["fix"]["accepted"] = sorted(set(self.state["fix"].get("accepted") or []) | set(self.options["accept"]))
            self.state["fix"]["reason"] = self.options.get("reason") or self.state["fix"].get("reason")
            if not self.state["fix"]["reason"]:
                self.say("error: --accept needs --reason: say why the remaining fails are acceptable")
                return 1
        first, last = self.options.get("from"), self.options.get("to")
        forced = False
        for stage in STAGES:
            forced = forced or stage == first
            if not forced and self.fresh(stage):
                self.say("%-8s done, inputs unchanged" % stage)
            else:
                code = self.run_stage(stage, forced)
                if code:
                    return code
            if stage == last:
                self.say("stopped after %s (--to)" % stage)
                break
        return 0

    def run_stage(self, stage: str, forced: bool) -> int:
        record = self.state["stages"].setdefault(stage, {"runs": 0, "seconds_total": 0.0, "spend_usd_total": 0.0})
        started = time.time()
        before = ledger_total(self.job)
        record.update(status="running", started=time.strftime("%Y-%m-%dT%H:%M:%S"), note=None, request=None)
        self.forced = forced
        try:
            note = getattr(self, "stage_" + stage)() or ""
            status = "skipped" if note.startswith("skipped") else "done"
            code = 0
        except Stop as stop:
            note, status, code = str(stop), "needs_agent", 3
            record["request"] = str(stop.request)
        except (Failed, story_mod.StoryError, cast_mod.CastError) as exc:
            note, status, code = str(exc), "failed", 1
        seconds = round(time.time() - started, 1)
        spent = round(ledger_total(self.job) - before, 6)
        record.update(status=status, note=note, seconds=seconds, spend_usd=spent, finished=time.strftime("%Y-%m-%dT%H:%M:%S"),
                      inputs=self.hashes(stage), outputs=[name for name in self.outputs(stage) if (self.job / name).exists()],
                      runs=int(record.get("runs", 0)) + 1, seconds_total=round(float(record.get("seconds_total", 0.0)) + seconds, 1),
                      spend_usd_total=round(float(record.get("spend_usd_total", 0.0)) + spent, 6))
        self.state["spend_usd"] = ledger_total(self.job)
        self.state["ceiling_usd"] = float(self.settings()["ceiling_usd"]) if (self.job / "story.json").is_file() else None
        write_json(self.state_path, self.state)
        self.say("%-8s %s in %.1f s%s%s" % (stage, status.replace("_", " "), seconds, ", $%.4f" % spent if spent else "", ": " + note.splitlines()[0] if note else ""))
        if status == "needs_agent":
            self.say("stopped for an agent: %s" % record["request"])
        elif status == "failed":
            for line in note.splitlines()[1:]:
                self.say("    " + line)
        return code

    def stage_script(self) -> str:
        given = self.options.get("story")
        target = self.job / "story.json"
        note = "story.json was already in the job"
        if given:
            source = Path(given).expanduser().resolve()
            if not source.is_file():
                raise Failed("--story %s does not exist" % source)
            self.state["source"] = "story.json#lines,script" if source == target else str(source)
            if is_story(source):
                if source != target:
                    shutil.copyfile(str(source), str(target))
                    for name in ("shotlist.json", "sound.json", "poses.json"):
                        if (source.parent / name).is_file() and not (self.job / name).exists():
                            shutil.copyfile(str(source.parent / name), str(self.job / name))
                    if (source.parent / "sets").is_dir() and not (self.job / "sets").exists():
                        shutil.copytree(str(source.parent / "sets"), str(self.job / "sets"))
                note = "copied %s (already a story with lines; the writer was not called)" % source.name
            else:
                writer = self.options.get("writer") or os.environ.get("MAKE_WRITER")
                if not writer or not (Path(writer).expanduser() / "pipeline" / "writer").is_dir():
                    raise Failed("%s is not a story.json, so the script writer has to write one, and its checkout is not set: pass --writer DIR or set "
                                 "MAKE_WRITER to the folder that holds pipeline/writer (branch script-style)" % source.name)
                settings = cast_mod.merge(SETTINGS, {})
                ceiling = min(float(settings["writer_ceiling_usd"]), max(0.0, float(self.options.get("budget") or settings["ceiling_usd"]) - ledger_total(self.job)))
                if ceiling <= 0:
                    raise Failed("the spend ceiling is reached; the writer was not started")
                ledger = self.job / "writer_ledger.jsonl"
                seen = ledger.read_text(encoding="utf-8").count("\n") if ledger.is_file() else 0
                extra = ["--seconds", str(self.options["seconds"])] if self.options.get("seconds") else []
                self.command("script", "pipeline.writer", "write", "--story", str(source), "--out", str(self.job), "--ceiling", "%.2f" % ceiling, *extra,
                             cwd=Path(writer).expanduser(), env={"WRITER_LEDGER": str(ledger)})
                rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()[seen:] if line.strip()] if ledger.is_file() else []
                cost = sum(float(row.get("cost_usd") or 0.0) for row in rows)
                ledger_add(self.job, "script", cost, {"calls": len(rows), "ledger": ledger.name})
                judged = read_json(self.job / "script_judge.json", {}) or {}
                note = "written by pipeline.writer from %s (%d model calls); script judge %s" % (source.name, len(rows), "passed" if judged.get("passed") else "did not pass: read script_judge.json")
        elif not target.is_file():
            raise Failed("no story: pass --story FILE (a story.json with lines, or a story text or record for the writer)")
        story = read_json(target, None)
        if not isinstance(story, dict):
            raise Failed("%s is not a JSON object" % target)
        added = [key for key in DEFAULTS if key not in story]
        for key in added:
            story[key] = copy.deepcopy(DEFAULTS[key])
        if story.get("slug") != self.job.name and not story.get("slug"):
            story["slug"] = self.job.name
        story_mod.validate_story(copy.deepcopy(story))
        if added:
            write_json(target, story)
            note += "; added the defaults for %s" % ", ".join(added)
        return note

    def stage_cast(self) -> str:
        expanded = cast_mod.write_job(self.job, narrator=self.options.get("narrator"))
        auto = sorted(name for name, member in expanded["members"].items() if member.get("auto"))
        told = (expanded.get("narrator") or {}).get("is")
        return "narrator %s; speakers %s%s" % (told or "keeps the story's own voice", ", ".join("%s=%s" % (name, entry.get("voice_name")) for name, entry in expanded["speakers"].items()) or "none",
                                               "; chosen automatically, listen and override in story.json casting: %s" % ", ".join(auto) if auto else "")

    def stage_voice(self) -> str:
        story = self.story()
        provider = "say" if self.options.get("offline") else (story.get("voice") or {}).get("provider", "auto")
        args = ["--job", str(self.job)] + (["--provider", "say"] if self.options.get("offline") else [])
        if provider != "say":
            self.need_key("voice", "elevenlabs")
            self.need_budget("voice")
        self.command("voice", "pipeline.voice", *args)
        words = read_json(self.job / "words.json", {}) or {}
        tokens = story_mod.tokenize(story["script"])
        if len(words.get("words") or []) != len(tokens):
            raise Failed("words.json has %d words and the script has %d" % (len(words.get("words") or []), len(tokens)))
        report = read_json(self.job / "voice_report.json", {}) or {}
        credits = sum(int(entry.get("characters_billed") or 0) for entry in (report.get("speakers") or {}).values())
        price = float(self.settings()["prices"]["elevenlabs_usd_per_1k_credits"])
        if report.get("provider") == "elevenlabs":
            ledger_add(self.job, "voice", credits * price / 1000.0, {"credits": credits, "usd_per_1k_credits": price, "estimate": True})
        return "%s, %.1f s, %s wpm, %d credits billed" % (report.get("provider", provider), float(words.get("duration") or 0.0), report.get("final_wpm", "?"), credits)

    def stage_shots(self) -> str:
        from . import studio
        story = self.story()
        words = story_mod.load_json(self.job / "words.json")
        if not (self.job / "shotlist.json").is_file():
            write_json(self.job / "shotlist.draft.json", draft_shots(story, words))
            raise Stop(self.request(
                "REQUEST-shots.md", "Author the sets and the shot list",
                "shotlist.json for this story: for every stretch of words a set, the cast present with an action and a face, and a camera. "
                "shotlist.draft.json already cuts the script into shots whose text matches the words; give each a set, cast and camera, "
                "merge or split shots as the story needs, and save it as shotlist.json. Use the cast ids in cast.json (avatars); leave the top-level "
                "cast object to this program, except for extras with a body, which you add there. If the story needs a place no existing set gives, "
                "write it as sets/<name>.luau in this folder (RUNBOOK.md, 'Design a set').",
                ["story.json (lines, speakers), cast.json (who plays whom, actor ids), words.json (timings), shotlist.draft.json",
                 "the sets, anchors, poses, faces, props and moods you can name: {run} pipeline.catalog --job {job}",
                 "CAST.md and POSES.md beside the job folders on the Mac; RUNBOOK.md, 'Author the shots'"],
                ["{run} pipeline.catalog --job {job}", "{run} pipeline.story check --job {job}", "{run} pipeline.checks --job {job}",
                 "{run} pipeline.preview --job {job} --shots s01,s02 --check --holder make:%s    # optional look, needs Studio; after a first run of this program built timeline.json" % self.job.name],
                "pipeline.story check prints ok and pipeline.checks reports no failing rule. This program then builds the timeline and goes on to the camera judge."),
                "shotlist.json is not written yet")
        try:
            loaded = story_mod.load_story(self.job)
        except story_mod.StoryError as exc:
            raise Stop(self.request("REQUEST-shots.md", "The shot list does not match the story", "Correct shotlist.json: " + str(exc),
                                    ["shotlist.json, story.json"], ["{run} pipeline.story check --job {job}"], "pipeline.story check prints ok."), str(exc))
        studio.use_job(self.job)
        known = studio.set_files()
        missing = sorted({shot["set"] for shot in loaded["shotlist"]["shots"]} - set(known))
        if missing:
            name = missing[0]
            raise Stop(self.request(
                "REQUEST-set-%s.md" % name, "Design the set %s" % name,
                "The shot list stages shots in the set %r, which does not exist in the repository or in this job. Write sets/%s.luau in this folder "
                "(copy the nearest existing set from studio/director/sets/ and change it): the room, its anchors for every place a character stands or sits, "
                "its set cameras, lights, moving pieces and wording. RUNBOOK.md, 'Design a set', has the rules." % (name, name),
                ["shots in this set: %s" % ", ".join(shot["id"] for shot in loaded["shotlist"]["shots"] if shot["set"] == name),
                 "existing sets to copy from: %s" % ", ".join(sorted(known))],
                ["mkdir -p {job}/sets && cp %s/studio/director/sets/<nearest>.luau {job}/sets/%s.luau" % (self.repo, name),
                 "{run} pipeline.catalog --job {job} --set %s" % name,
                 "{run} pipeline.preview --job {job} --shots <one shot per camera of the set> --check --holder make:%s    # needs Studio" % self.job.name],
                "pipeline.catalog --job lists the set with the anchors and cameras the shots name, and a preview of one shot per set camera shows the room with nothing floating."),
                "set %s does not exist" % name)
        record = read_json(self.job / "cast.json", None)
        changed = []  # type: List[str]
        if record:
            shotlist = story_mod.load_json(self.job / "shotlist.json")
            changed = cast_mod.apply_shotlist(shotlist, record)
            if changed:
                write_json(self.job / "shotlist.json", shotlist)
        self.command("shots", "pipeline.story", "timeline", "--job", str(self.job))
        self.clear_requests("REQUEST-shots")
        self.clear_requests("REQUEST-set-")
        timeline = read_json(self.job / "timeline.json", {}) or {}
        return "%d shots over %.1f s in sets %s%s" % (len(timeline.get("shots") or []), float(timeline.get("duration") or 0.0),
                                                     ", ".join(sorted({shot["set"] for shot in timeline.get("shots") or []})),
                                                     "; cast entries set from the registry: %s" % ", ".join(changed) if changed else "")

    def stage_checks(self) -> str:
        result = checks_mod.check_job(self.job)
        write_json(self.job / "checks.json", result)
        if result["errors"]:
            by_shot = sorted({entry["shot"] for entry in result["errors"] if entry["shot"]})
            raise Stop(self.request(
                "REQUEST-checks.md", "The shot list breaks the defaults",
                "Change shotlist.json so every rule below passes. Each rule is something an earlier video failed its judges on after a render; "
                "fixing it here costs nothing. A rule can be passed on purpose for one shot with \"allow\": [rule] and \"allow_reason\".",
                ["shotlist.json; checks.json (the same findings as data)", "the rules: {run} pipeline.checks --help"],
                ["{run} pipeline.checks --job {job}"],
                "pipeline.checks --job reports 0 failing rule checks.",
                detail=checks_mod.describe(result)),
                "%d rule failures in %s" % (len(result["errors"]), ", ".join(by_shot) or "the shot list"))
        self.clear_requests("REQUEST-checks")
        return "%d shots pass the defaults, %d warnings" % (result["shots"], len(result["warnings"]))

    def stage_sound(self) -> str:
        story = self.story()
        made = ""
        if not (self.job / "sound.json").is_file():
            write_json(self.job / "sound.json", sound_from_story(story))
            made = "sound.json written from the story's cues; "
        synthetic = bool(self.options.get("offline"))
        if not synthetic:
            self.need_key("sound", "elevenlabs")
            self.need_budget("sound")
        self.command("sound", "pipeline.sound", "--job", str(self.job), *(["--provider", "synthetic"] if synthetic else []))
        report = read_json(self.job / "sound_report.json", {}) or {}
        credits = 0.0
        for item in list(report.get("items") or []) + [report.get("music") or {}]:
            try:
                credits += float(item.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
        price = float(self.settings()["prices"]["elevenlabs_usd_per_1k_credits"])
        if report.get("provider") == "elevenlabs":
            ledger_add(self.job, "sound", credits * price / 1000.0, {"credits": credits, "usd_per_1k_credits": price, "estimate": True})
        music = report.get("music") or {}
        unpriced = report.get("provider") == "elevenlabs" and music.get("cached") is False and not music.get("cost") and "generation" in str(music.get("source"))
        return "%s%s, %.1f s bed, %s generated, %s reused, %d credits%s" % (made, report.get("provider", "?"), float(report.get("duration") or 0.0),
                                                                          report.get("generated", "?"), report.get("reused", "?"), credits,
                                                                          "; a score was generated and the sound stage reports no cost for it, so it is not in the ledger" if unpriced else "")

    def changed_shots(self, stage: str) -> Optional[List[str]]:
        """Shot ids whose staging changed since this stage last finished; None means every shot."""
        timeline = read_json(self.job / "timeline.json", {}) or {}
        shared = hashlib.sha1(json.dumps([timeline.get("cast"), timeline.get("defaults"), digest(self.job, "sets/"), digest(self.job, "poses.json")], sort_keys=True).encode("utf-8")).hexdigest()
        now = {shot["id"]: hashlib.sha1(json.dumps({key: value for key, value in shot.items() if key not in ("index",)}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
               for shot in timeline.get("shots") or []}
        seen = (self.state.get("staged") or {}).get(stage) or {}
        self.pending_staged = {"shared": shared, "shots": now}
        if getattr(self, "forced", False) or seen.get("shared") != shared or not seen.get("shots"):
            return None
        return [shot_id for shot_id, value in now.items() if seen["shots"].get(shot_id) != value]

    def remember_staged(self, stage: str) -> None:
        self.state.setdefault("staged", {})[stage] = self.pending_staged

    def stage_camera(self) -> str:
        story = self.story()
        settings = self.settings()
        provider = "none" if self.options.get("offline") else (story.get("judge") or {}).get("provider", "gemini")
        changed = self.changed_shots("camera")
        if changed is not None and not changed:
            self.remember_staged("camera")
            return "no shot's staging changed; cameras kept"
        args = ["shots", "--job", str(self.job)] + (["--shots", ",".join(changed)] if changed else [])
        if provider == "none":
            args += ["--provider", "none"]
        else:
            self.need_key("camera", provider)
            args += ["--ceiling", "%.2f" % min(self.need_budget("camera"), float(settings["camera_ceiling_usd"]))]
        before = float(((read_json(self.job / "judge.json", {}) or {}).get("usage") or {}).get("cost_usd") or 0.0)
        self.command("camera", "pipeline.judge", *args)
        doc = read_json(self.job / "judge.json", {}) or {}
        usage = doc.get("usage") or {}
        cost = max(0.0, float(usage.get("cost_usd") or 0.0) - before)
        if cost:
            ledger_add(self.job, "camera", cost, {"provider": provider, "model": (doc.get("config") or {}).get("model"), "shots": len(changed) if changed else len(doc.get("shots") or [])})
        self.remember_staged("camera")
        problems = caption_band_problems(doc)
        problems += ["variety: " + problem for problem in ((doc.get("variety") or {}).get("problems") or [])]
        if problems:
            raise Stop(self.request(
                "REQUEST-camera.md", "Cameras that put the subject under the caption or break variety",
                "The caption sits between %.0f%% and %.0f%% of the height. For each shot below, move the camera (frame, yaw, pitch, head) or the subject so the head, "
                "the hands doing the action and any in-world text are outside that band, or fix the variety break. A camera you are sure of can be kept with "
                "\"locked\": true and \"allow\": [\"caption_band\"] with an \"allow_reason\" on the shot." % (100 * CAPTION_BAND[0], 100 * CAPTION_BAND[1]),
                ["judge.json (chosen cameras and their geometry), judge/ (the candidate sheets), shotlist.json"],
                ["{run} pipeline.judge show --job {job}", "{run} pipeline.preview --job {job} --shots <ids> --judged --holder make:%s    # the sheet draws the caption band" % self.job.name],
                "A rerun of this program gets through the camera stage with no shot listed.", detail="\n".join("- " + problem for problem in problems)),
                "%d camera findings" % len(problems))
        self.clear_requests("REQUEST-camera")
        return "%s shots judged with %s, $%.4f" % (len(changed) if changed else "all", provider, cost)

    def stage_posture(self) -> str:
        story = self.story()
        if not (story.get("judge") or {}).get("posture", False):
            return "skipped: story.json judge.posture is off"
        changed = self.changed_shots("posture")
        if changed is not None and not changed and not self.options.get("from"):
            self.remember_staged("posture")
            return "no shot's staging changed; the geometry on record stands"
        args = ["posture", "--job", str(self.job), "--no-vision"] + (["--shots", ",".join(changed)] if changed else [])
        code = self.command("posture", "pipeline.judge", *args, ok=(0, 1))
        self.remember_staged("posture")
        record = (read_json(self.job / "judge.json", {}) or {}).get("posture") or {}
        failed = list((record.get("summary") or {}).get("failed_ids") or [])
        if code == 1 and not failed:
            raise Failed("the posture check exited with an error; see logs/posture.log")
        if failed:
            lines = []
            for entry in record.get("shots") or []:
                if entry.get("id") in failed:
                    lines.append("- %s: %s" % (entry["id"], "; ".join(entry.get("reasons") or []) or "see posture.json"))
            raise Stop(self.request(
                "REQUEST-posture.md", "Shots that fail the geometry check",
                "Re-pose each shot below so the geometry check passes: nothing through scenery, the body or another character, feet planted, hands on their "
                "targets, every prop and set piece touching something, nothing built twice, and an action for every character. posture.json has the findings per frame.",
                ["posture.json, shotlist.json, the job's poses.json and sets/", "POSES.md on the Mac: the verified poses and what each needs in its cast entry"],
                ["{run} pipeline.preview --job {job} --shots %s --check --holder make:%s    # a few shots at a time; look at preview/sheet.jpg" % (",".join(failed[:6]), self.job.name)],
                "pipeline.preview --check prints pass for every shot listed. Rerun this program: it rebuilds the timeline and checks the changed shots again.",
                detail="\n".join(lines)),
                "%d shots fail geometry: %s" % (len(failed), ", ".join(failed)))
        self.clear_requests("REQUEST-posture")
        return "geometry clean for %s shots" % (len(changed) if changed else (record.get("summary") or {}).get("shots", "all"))

    def stage_render(self) -> str:
        self.command("render", "pipeline.render", "video", "--job", str(self.job), *(["--force"] if self.options.get("from") == "render" else []))
        report = read_json(self.job / "render_report.json", {}) or {}
        return "%s of %s frames rendered at %s fps, %s shots kept" % (report.get("frames_rendered", "?"), report.get("frames_total", "?"),
                                                                    report.get("frames_per_second", "?"), report.get("shots_kept", 0))

    def stage_edit(self) -> str:
        args = ["--job", str(self.job)] + (["--music", str(self.job / "bed.wav")] if (self.job / "bed.wav").is_file() else [])
        self.command("edit", "pipeline.edit", *args)
        report = read_json(self.job / "edit_report.json", {}) or {}
        return "final.mp4 %.2f s, %s caption events" % (float(report.get("output_duration") or 0.0), report.get("caption_events", "?"))

    def stage_quality(self) -> str:
        if self.options.get("offline"):
            return "skipped: --offline runs no model judges"
        settings = self.settings()
        provider, model = settings["quality"]["provider"], settings["quality"]["model"]
        self.need_key("quality", provider)
        if self.remaining() <= 0:
            return "skipped: the spend ceiling of $%.2f was reached before the judges ran" % float(settings["ceiling_usd"])
        args = ["--job", str(self.job), "--provider", provider, "--model", model, "--ceiling", "%.2f" % float(settings["ceiling_usd"])]
        if (self.job / "quality.json").is_file() and self.options.get("from") != "quality":
            args.append("--changed")
        self.command("quality", "pipeline.quality", *args)
        summary = (read_json(self.job / "quality.json", {}) or {}).get("summary") or {}
        return "%s shots: %s pass, %s fail, %s incomplete" % (summary.get("shots", "?"), summary.get("pass", "?"), summary.get("fail", "?"), summary.get("incomplete", "?"))

    def failing(self) -> Dict[str, List[str]]:
        doc = read_json(self.job / "quality.json", {}) or {}
        out = {}  # type: Dict[str, List[str]]
        for key, row in ((doc.get("summary") or {}).get("judges") or {}).items():
            for shot_id in row.get("failing") or []:
                out.setdefault(shot_id, []).append(key)
        return out

    def stage_fix(self) -> str:
        if self.options.get("offline") or not (self.job / "quality.json").is_file():
            return "skipped: no quality report to act on"
        failing = self.failing()
        fix = self.state["fix"]
        accepted = set(fix.get("accepted") or [])
        open_ids = sorted(shot_id for shot_id in failing if "all" not in accepted and shot_id not in accepted)
        if not failing:
            self.clear_requests("REQUEST-fix")
            return "no shot fails a judge"
        if not open_ids:
            self.clear_requests("REQUEST-fix")
            return "%d failing shots accepted: %s" % (len(failing), fix.get("reason"))
        settings = self.settings()
        if self.remaining() <= 0:
            return "stopped by the spend ceiling ($%.2f): %d shots still fail: %s" % (float(settings["ceiling_usd"]), len(open_ids), ", ".join(open_ids))
        if int(fix.get("round", 0)) >= int(settings["fix_rounds"]):
            return "stopped after %d fix rounds: %d shots still fail: %s" % (int(fix["round"]), len(open_ids), ", ".join(open_ids))
        seen = digest(self.job, "quality.json#summary")
        if fix.get("asked_for") != seen:
            fix["round"] = int(fix.get("round", 0)) + 1
            fix["asked_for"] = seen
        doc = read_json(self.job / "quality.json", {}) or {}
        lines = []
        for entry in doc.get("shots") or []:
            if entry.get("id") in open_ids:
                reasons = ["%s: %s" % (key, (entry["judges"].get(key) or {}).get("reason") or (entry["judges"].get(key) or {}).get("fails") or "failed") for key in failing[entry["id"]]]
                lines.append("- %s (%s) \"%s\"\n  %s" % (entry["id"], entry.get("set"), str(entry.get("line") or "")[:80], "\n  ".join(str(reason)[:300] for reason in reasons)))
        raise Stop(self.request(
            "REQUEST-fix-%d.md" % fix["round"], "Decide a fix for each failing shot (round %d of %d)" % (fix["round"], int(settings["fix_rounds"])),
            "For every shot below decide one of: restage it (shotlist.json: pose, face, place, props, light, camera), change a pose (poses.json) or the set "
            "(sets/<set>.luau), or accept it with a reason. Look at the sheet the judge saw before deciding; a judge can be wrong, and a ruling that holds for "
            "the whole video goes in story.json quality.accepted. Group shots that fail for one cause and fix the cause once. Keep ids, text and holds unchanged.",
            ["QUALITY.md, quality.json, quality/sheets/ (every sheet a judge saw), posture.json", "shotlist.json, poses.json, sets/"],
            ["{run} pipeline.preview --job {job} --shots %s --check --holder make:%s" % (",".join(open_ids[:6]), self.job.name),
             "{run} pipeline.checks --job {job}",
             "# to accept what is left instead:  {run} pipeline.make --out {job} --accept %s --reason \"why\"" % ",".join(open_ids)],
            "A rerun of this program re-renders the changed shots, judges them again (--changed) and either finishes or writes the next round's request. "
            "$%.2f of the $%.2f ceiling is left." % (self.remaining(), float(settings["ceiling_usd"])),
            detail="\n".join(lines)),
            "round %d: %d shots fail: %s" % (fix["round"], len(open_ids), ", ".join(open_ids)))

    def stage_final(self) -> str:
        if not (self.job / "final.mp4").is_file():
            raise Failed("final.mp4 is missing; run from --from edit")
        (self.job / "MAKE.md").write_text(self.report(), encoding="utf-8")
        failing = self.failing()
        return "final.mp4, %s, MAKE.md%s" % ("QUALITY.md" if (self.job / "QUALITY.md").is_file() else "no quality report",
                                             "; %d shots still fail a judge" % len(failing) if failing else "")

    def report(self) -> str:
        lines = ["# %s: run report" % self.job.name, "",
                 "| stage | status | runs | seconds (all runs) | spend USD (all runs) | result |", "|---|---|---|---|---|---|"]
        for stage in STAGES:
            record = self.state["stages"].get(stage) or {}
            status = "done" if stage == "final" else record.get("status", "not run")
            lines.append("| %s | %s | %s | %s | %.4f | %s |" % (stage, status, record.get("runs", 0), record.get("seconds_total", 0.0),
                                                              float(record.get("spend_usd_total", 0.0)), str(record.get("note") or "").splitlines()[0][:200] if record.get("note") else ""))
        total_seconds = sum(float((self.state["stages"].get(stage) or {}).get("seconds_total", 0.0)) for stage in STAGES)
        lines += ["", "Total: %.0f s of machine time in stages, $%.4f of the $%.2f ceiling (ledger: quality/ledger.json). Time an agent spent between stops is not in this table."
                  % (total_seconds, ledger_total(self.job), float(self.settings()["ceiling_usd"])), ""]
        return "\n".join(lines)

    def dry_run(self) -> int:
        self.say("dry run for %s: nothing is run or written" % self.job)
        given = self.options.get("story")
        have = lambda name: (self.job / name).is_file()
        for row in self.plan():
            stage = row["stage"]
            what = {
                "script": "copy the story" if (given and is_story(Path(given).expanduser())) or (not given and have("story.json")) else "call pipeline.writer from its checkout (model key)",
                "cast": "python -m pipeline.cast expand --job JOB --write" + (" --narrator %s" % self.options["narrator"] if self.options.get("narrator") else ""),
                "voice": "python -m pipeline.voice --job JOB" + (" --provider say" if self.options.get("offline") else "  (ElevenLabs key unless the story's provider is say)"),
                "shots": "build the timeline from shotlist.json" if have("shotlist.json") or (given and (Path(given).expanduser().parent / "shotlist.json").is_file())
                         else "STOP for an agent: REQUEST-shots.md (sets and shot list)",
                "checks": "python -m pipeline.checks --job JOB; a failing rule is a STOP for an agent (REQUEST-checks.md)",
                "sound": "python -m pipeline.sound --job JOB" + (" --provider synthetic" if self.options.get("offline") else "  (ElevenLabs key)"),
                "camera": "python -m pipeline.judge shots --job JOB" + (" --provider none" if self.options.get("offline") else "  (Studio, vision key)") + "; caption-band findings are a STOP (REQUEST-camera.md)",
                "posture": "python -m pipeline.judge posture --job JOB --no-vision  (Studio); failing shots are a STOP (REQUEST-posture.md)",
                "render": "python -m pipeline.render video --job JOB  (Studio window on screen)",
                "edit": "python -m pipeline.edit --job JOB --music JOB/bed.wav  (FFmpeg with libass)",
                "quality": "skipped (--offline)" if self.options.get("offline") else "python -m pipeline.quality --job JOB --provider gemini --model gemini-3.8-flash  (vision key)",
                "fix": "failing shots are a STOP for an agent (REQUEST-fix-N.md) until none fail, the rounds or the ceiling run out, or they are accepted",
                "final": "final.mp4, QUALITY.md, MAKE.md",
            }[stage]
            self.say("%-8s %-4s %-32s %s" % (stage, "run" if row["run"] else "skip", row["why"], what if row["run"] else ""))
        self.say("spend so far $%.4f" % ledger_total(self.job))
        return 0

    def status(self) -> int:
        if not self.state["stages"]:
            self.say("no run recorded in %s" % self.state_path)
            return 0
        self.say(self.report())
        for stage in STAGES:
            record = self.state["stages"].get(stage) or {}
            if record.get("status") in ("needs_agent", "failed"):
                self.say("%s: %s%s" % (stage, record.get("note", ""), "\nrequest: %s" % record["request"] if record.get("request") else ""))
        return 0


def caption_band_problems(doc: Dict[str, Any]) -> List[str]:
    """Shots whose chosen camera puts the subject's head, or in-world text, in the caption band."""
    problems = []
    for entry in doc.get("shots") or []:
        chosen = entry.get("chosen") or {}
        geometry = chosen.get("geometry") or {}
        camera = chosen.get("camera") or {}
        if "caption_band" in (camera.get("allow") or []):
            continue
        head = (chosen.get("metrics") or geometry.get("metrics") or {}).get("head")
        noted = [str(item) for key in ("notes", "issues", "warnings") for item in (geometry.get(key) or []) if "caption" in str(item) or "band" in str(item)]
        if isinstance(head, (list, tuple)) and len(head) == 2 and CAPTION_BAND[0] <= float(head[1]) <= CAPTION_BAND[1]:
            problems.append("%s: the head sits at %.0f%% of the height, inside the caption band" % (entry.get("id"), 100 * float(head[1])))
        elif noted:
            problems.append("%s: %s" % (entry.get("id"), ", ".join(noted)))
    return problems


def main(argv: Optional[List[str]] = None, runner: Optional[Runner] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.make", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--story", help="a story.json with lines, or a story text or record for the script writer")
    parser.add_argument("--out", required=True, type=Path, help="the job folder")
    parser.add_argument("--narrator", help="the cast member who narrates")
    parser.add_argument("--from", dest="from_stage", choices=STAGES, help="run this stage and every later one again")
    parser.add_argument("--to", dest="to_stage", choices=STAGES, help="stop after this stage")
    parser.add_argument("--budget", type=float, help="spend ceiling for the job in USD (default 8)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; run and write nothing")
    parser.add_argument("--offline", action="store_true", help="say voice, synthetic sound, geometry-only cameras, no model judges")
    parser.add_argument("--writer", help="checkout that holds pipeline/writer (default $MAKE_WRITER)")
    parser.add_argument("--seconds", type=float, help="target length for the script writer")
    parser.add_argument("--fix-rounds", type=int, help="fix requests before giving up (default 3)")
    parser.add_argument("--accept", help="all, or comma separated shot ids whose remaining fails are accepted")
    parser.add_argument("--reason", help="why they are accepted")
    parser.add_argument("--holder", help="name shown to others waiting for Studio (default make:<job>)")
    parser.add_argument("--status", action="store_true", help="print the recorded stage table and stop")
    args = parser.parse_args(argv)
    options = {"story": args.story, "narrator": args.narrator, "from": args.from_stage, "to": args.to_stage, "budget": args.budget, "dry_run": args.dry_run,
               "offline": args.offline, "writer": args.writer, "seconds": args.seconds, "fix_rounds": args.fix_rounds,
               "accept": [piece.strip() for piece in args.accept.split(",") if piece.strip()] if args.accept else None, "reason": args.reason, "holder": args.holder}
    maker = Make(args.out, options, runner)
    if args.status:
        return maker.status()
    if args.dry_run:
        return maker.dry_run()
    return maker.run()


if __name__ == "__main__":
    sys.exit(main())
