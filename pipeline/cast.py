"""Cast registry: the recurring characters, each with one avatar and one voice, expanded into a job.

  python -m pipeline.cast list                         the pool and the costumes
  python -m pipeline.cast expand --job J [--narrator ID] [--write]   what the story's cast becomes; --write puts it in the job
  python -m pipeline.cast sheet --out J [--ids a,b]    a small job that shows the pool, for pipeline.preview

The pool lives in pipeline/cast.json. A script names a member by its lowercase id as a speaker (willow,
lawrence) and that speaker gets the member's voice and avatar with nothing else written. story.json may
carry "casting", an object keyed by speaker or actor id:

  "casting": {
    "narrator": "lawrence",                              the narrator is Lawrence: his voice, and his avatar as actor "lawrence"
    "narrator": {"is": "lawrence", "as": "me"},          the same, with the avatar under the actor id "me"
    "mom": "willow",                                     the role "mom" is played by Willow
    "allan": {"costume": "officer"},                     a member in a costume from the registry
    "jesslyn": {"costume": "dispatcher", "avatar": {"headset": {"side": "R"}}},   any key overridden for this job; null removes one
    "thing": {"avatar": {"rig": "avatar", "bundle": 572, "size": 1.28}, "voice": {"voice_id": "...", "process": {...}}},   an extra, defined here only
    "machine": {"voice": {"voice_id": "..."}},           a voice with no avatar
    "kiosk": {"stock": "female", "effect": "machine"},   an extra with a stock voice and a voice effect from the registry
    "dark_close": {"voice": "dark", "effect": "close"},  the same voice as another speaker, with its own effect
    "crowd": {"voice": "narrator"}                       an extra that speaks in the narrator's voice
  }

A member who narrates takes the registry's narrator_voice settings over its own, so every voice narrates
at one pace. --narrator ID says who narrates without editing the story (it is written into casting.narrator).
A story written by pipeline.writer carries "cast" as a list; its entries of kind "person" whose name is in
the pool are cast without a casting entry, and its roles and voices (a machine, a creature, a stranger)
get a stock voice by the gender it describes and an effect by its processing note, unless casting says
otherwise; cast.json marks those "auto". The writer names the narrator only when the story names its
teller, so say who narrates with --narrator or casting.narrator. --write sets story.json voice (the
narrator's voice, voice.speakers), adds the names for the captions, sets the avatars in shotlist.json cast
when that file exists, and records the result in <job>/cast.json.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import story as story_mod

REGISTRY = Path(__file__).with_name("cast.json")
NARRATOR = "narrator"
ID = re.compile(r"^[a-z][a-z0-9_]*$")
SPEAKER_KEYS = ("voice_id", "voice_name", "settings", "process")
CASTING_KEYS = ("is", "as", "costume", "effect", "stock", "avatar", "voice")
MACHINE_WORDS = ("machine", "kiosk", "screen reader", "computer", "automated", "system", "speaker", "announcement", "terminal", "robot", "assistant")


class CastError(ValueError):
    """Raised when the registry or a story's casting cannot be expanded."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CastError(message)


def merge(base: Any, over: Any) -> Any:
    """Lay one object over another, key by key; a null in the upper one removes the key."""
    if not isinstance(base, dict) or not isinstance(over, dict):
        return copy.deepcopy(over)
    out = copy.deepcopy(base)
    for key, value in over.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge(out.get(key), value)
    return out


def load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    source = Path(path) if path else REGISTRY
    try:
        with open(source, "r", encoding="utf-8") as handle:
            registry = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CastError("cast registry %s cannot be read: %s" % (source, exc))
    _require(isinstance(registry, dict) and registry.get("version") == 1, "cast registry: version must be 1")
    characters = registry.get("characters")
    _require(isinstance(characters, dict) and bool(characters), "cast registry: characters must be a non-empty object")
    voices = {}  # type: Dict[str, str]
    for name, entry in characters.items():
        where = "cast registry: characters.%s" % name
        _require(ID.match(name) is not None and name != NARRATOR, where + " must be a lowercase id other than narrator")
        _require(isinstance(entry, dict), where + " must be an object")
        _require(isinstance(entry.get("name"), str) and entry["name"].strip(), where + ".name must be the display name")
        _require(isinstance(entry.get("avatar"), dict) and entry["avatar"], where + ".avatar must be a shot-list cast entry")
        voice = entry.get("voice")
        _require(isinstance(voice, dict) and isinstance(voice.get("voice_id"), str) and voice["voice_id"].strip(), where + ".voice.voice_id must be a voice id")
        unknown = sorted(set(voice) - set(SPEAKER_KEYS))
        _require(not unknown, "%s.voice has unknown keys %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(SPEAKER_KEYS)))
        _require(voice["voice_id"] not in voices, "%s shares its voice with %s; every member has its own" % (where, voices.get(voice["voice_id"])))
        voices[voice["voice_id"]] = name
    for name, costume in (registry.get("costumes") or {}).items():
        _require(isinstance(costume, dict) and isinstance(costume.get("avatar", {}), dict) and isinstance(costume.get("voice", {}), dict),
                 "cast registry: costumes.%s must be an object with avatar and voice objects" % name)
    for name, effect in (registry.get("effects") or {}).items():
        _require(isinstance(effect, dict) and isinstance(effect.get("settings", {}), dict) and isinstance(effect.get("process", {}), dict),
                 "cast registry: effects.%s must be an object with settings and process objects" % name)
    for group, choices in (registry.get("stock_voices") or {}).items():
        _require(isinstance(choices, list) and bool(choices), "cast registry: stock_voices.%s must be a non-empty list" % group)
        for choice in choices:
            _require(isinstance(choice, dict) and isinstance(choice.get("voice_id"), str), "cast registry: stock_voices.%s entries need a voice_id" % group)
            _require(choice["voice_id"] not in voices, "cast registry: stock voice %s is %s's voice; stock voices are not pool voices" % (choice.get("voice_name"), voices.get(choice["voice_id"])))
    return registry


def registry_hash(registry: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(registry, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def speakers_of(story: Dict[str, Any]) -> List[str]:
    """Every speaker of the story's lines, in order of first appearance."""
    seen = []  # type: List[str]
    for line in story.get("lines") or [{"speaker": NARRATOR}]:
        if line["speaker"] not in seen:
            seen.append(line["speaker"])
    return seen


def writer_people(story: Dict[str, Any], registry: Dict[str, Any]) -> Dict[str, str]:
    """Speaker -> pool id for the people a pipeline.writer story named from the pool."""
    found = {}  # type: Dict[str, str]
    entries = story.get("cast")
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") != "person":
            continue
        member = str(entry.get("name") or "").strip().lower()
        if isinstance(entry.get("speaker"), str) and member in registry["characters"]:
            found[entry["speaker"]] = member
    return found


def casting_of(story: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    casting = story.get("casting", {})
    _require(isinstance(casting, dict), "story.json: casting must be an object of speaker or actor id -> who plays it")
    out = {}  # type: Dict[str, Dict[str, Any]]
    for name, spec in casting.items():
        where = "story.json: casting.%s" % name
        _require(isinstance(name, str) and ID.match(name) is not None, where + " must be keyed by a lowercase id")
        if isinstance(spec, str):
            spec = {"is": spec}
        _require(isinstance(spec, dict), where + " must be a pool id or an object")
        unknown = sorted(set(spec) - set(CASTING_KEYS))
        _require(not unknown, "%s has unknown keys %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(CASTING_KEYS)))
        out[name] = spec
    return out


def _speaker_entry(voice: Dict[str, Any], where: str) -> Dict[str, Any]:
    unknown = sorted(set(voice) - set(SPEAKER_KEYS))
    _require(not unknown, "%s.voice has unknown keys %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(SPEAKER_KEYS)))
    _require(isinstance(voice.get("voice_id"), str) and voice["voice_id"].strip(), where + ".voice.voice_id must be a voice id")
    return voice


def effect_for(entry: Dict[str, Any]) -> Optional[str]:
    """The registry effect a pipeline.writer cast entry asks for, read from its role and processing note."""
    described = entry.get("voice") if isinstance(entry.get("voice"), dict) else {}
    note = str(described.get("processing") or "").lower()
    role = ("%s %s" % (entry.get("role") or "", entry.get("name") or "")).lower()
    if "intercom" in note or "tannoy" in note or "loudspeaker" in note:
        return "intercom"
    if "phone" in note or "radio" in note:
        return "phone_line"
    if entry.get("kind") == "voice" and any(word in role for word in MACHINE_WORDS):
        return "machine"
    if "close" in note or "whisper" in note:
        return "close"
    if "wrong" in note or "pitched" in note or "echo" in note or "slow" in note:
        return "wrong"
    return None


def auto_extras(story: Dict[str, Any], registry: Dict[str, Any], taken: List[str]) -> Dict[str, Dict[str, Any]]:
    """Casting for the roles and voices a pipeline.writer story lists and nobody cast: a stock voice by gender, an effect by its note."""
    entries = story.get("cast")
    if not isinstance(entries, list):
        return {}
    listed = {entry["speaker"]: entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("speaker"), str)}
    stock = registry.get("stock_voices") or {}
    out = {}  # type: Dict[str, Dict[str, Any]]
    for speaker, entry in listed.items():
        if speaker == NARRATOR or speaker in taken:
            continue
        described = entry.get("voice") if isinstance(entry.get("voice"), dict) else {}
        if speaker.endswith("_close") and speaker[:-6] in listed:
            out[speaker] = {"voice": speaker[:-6], "effect": "close"}
            continue
        gender = str(described.get("gender") or "").lower()
        spec = {"stock": gender if gender in stock else "neutral"}  # type: Dict[str, Any]
        effect = effect_for(entry)
        if effect in (registry.get("effects") or {}):
            spec["effect"] = effect
        out[speaker] = spec
    return out


def expand(story: Dict[str, Any], registry: Optional[Dict[str, Any]] = None, narrator: Optional[str] = None) -> Dict[str, Any]:
    """Turn the story's speakers and casting into voice.speakers entries and shot-list cast entries."""
    registry = registry or load_registry()
    characters = registry["characters"]
    costumes = registry.get("costumes") or {}
    effects = registry.get("effects") or {}
    stock = registry.get("stock_voices") or {}
    casting = casting_of(story)
    if narrator:
        casting[NARRATOR] = dict(casting.get(NARRATOR, {}), **{"is": narrator})
    people = writer_people(story, registry)
    speakers = speakers_of(story)
    for speaker in speakers:
        _require(ID.match(speaker) is not None, "story.json: speaker %r must be a lowercase id (letters, digits, underscores)" % speaker)
    auto = auto_extras(story, registry, list(casting) + list(characters) + list(people))
    order = list(speakers)
    for name in list(casting) + [NARRATOR]:
        if name not in order:
            order.append(name)
    specs = {name: dict(casting.get(name) or auto.get(name) or {}) for name in order}
    members = {name: specs[name].get("is") or (name if name in characters else people.get(name)) for name in order}
    result = {"version": 1, "registry": registry_hash(registry), "narrator": None, "voice": {}, "speakers": {}, "avatars": {}, "members": {}, "names": [], "unvoiced": []}  # type: Dict[str, Any]
    problems = []  # type: List[str]
    used = {characters[member]["voice"]["voice_id"] for member in members.values() if member in characters}
    base = {}  # type: Dict[str, Dict[str, Any]]
    for name in order:
        spec, member, where = specs[name], members[name], "story.json: casting.%s" % name
        if member is not None:
            if member not in characters:
                problems.append("%s.is %r is not a pool member (pool: %s)" % (where, member, ", ".join(sorted(characters))))
            else:
                base[name] = copy.deepcopy(characters[member]["voice"])
        elif spec.get("stock") is not None:
            choices = stock.get(spec["stock"])
            if not choices:
                problems.append("%s.stock %r is not in the registry (stock voices: %s)" % (where, spec["stock"], ", ".join(sorted(stock)) or "none"))
                continue
            pick = next((choice for choice in choices if choice["voice_id"] not in used), choices[0])
            used.add(pick["voice_id"])
            base[name] = copy.deepcopy(pick)
        elif isinstance(spec.get("voice"), dict):
            base[name] = {}
    for name in order:
        borrowed = specs[name].get("voice")
        if isinstance(borrowed, str) and borrowed != NARRATOR and name not in base:
            if borrowed in base and base[borrowed]:
                base[name] = {key: copy.deepcopy(base[borrowed][key]) for key in ("voice_id", "voice_name", "settings") if key in base[borrowed]}
            else:
                problems.append("story.json: casting.%s.voice names %r, which has no voice of its own to borrow" % (name, borrowed))
    for name in order:
        spec, member, where = specs[name], members[name], "story.json: casting.%s" % name
        if member is not None and member not in characters:
            continue
        costume, effect = spec.get("costume"), spec.get("effect")
        if costume is not None and costume not in costumes:
            problems.append("%s.costume %r is not in the registry (costumes: %s)" % (where, costume, ", ".join(sorted(costumes)) or "none"))
            continue
        if effect is not None and effect not in effects:
            problems.append("%s.effect %r is not in the registry (effects: %s)" % (where, effect, ", ".join(sorted(effects)) or "none"))
            continue
        dressed = costumes.get(costume, {})
        voice = None  # type: Any
        if name in base:
            voice = merge(base[name], dressed.get("voice", {}))
            if name == NARRATOR and member is not None:
                voice = merge(voice, registry.get("narrator_voice") or {})
            if effect is not None:
                voice = merge(voice, {key: effects[effect][key] for key in ("settings", "process") if key in effects[effect]})
            if isinstance(spec.get("voice"), dict):
                voice = merge(voice, spec["voice"])
        elif spec.get("voice") == NARRATOR:
            voice = NARRATOR
        if member is not None:
            avatar = merge(merge(characters[member]["avatar"], dressed.get("avatar", {})), spec.get("avatar", {})) if spec.get("avatar") is not False else None
            if characters[member]["name"] not in result["names"]:
                result["names"].append(characters[member]["name"])
        else:
            avatar = merge(dressed.get("avatar", {}), spec["avatar"]) if isinstance(spec.get("avatar"), dict) else None
            if name == NARRATOR:
                if voice is None and avatar is None:
                    continue
            elif name in speakers and voice is None:
                if name in base or isinstance(spec.get("voice"), str):
                    continue
                problems.append("speaker %r is not a pool member and has no voice: add %s as a pool id (%s), as a stock voice {\"stock\": \"%s\"}, "
                                "or as an extra {\"voice\": {\"voice_id\": ...}, \"avatar\": {...}} (\"voice\": \"narrator\" borrows the narrator's voice)"
                                % (name, where, ", ".join(sorted(characters)), "|".join(sorted(stock)) or "..."))
                continue
            elif name not in speakers and avatar is None:
                problems.append("%s is not a speaker and defines no avatar; give it an avatar or remove it" % where)
                continue
        actor = name
        if name == NARRATOR:
            actor = spec.get("as") or member or NARRATOR
            result["narrator"] = {"is": member, "actor": actor if avatar else None}
        elif spec.get("as"):
            actor = spec["as"]
        try:
            if name == NARRATOR:
                if isinstance(voice, dict):
                    entry = _speaker_entry(voice, where)
                    result["voice"] = {key: entry[key] for key in ("voice_id", "voice_name", "settings") if key in entry}
                    if entry.get("process"):
                        result["speakers"][NARRATOR] = {"process": entry["process"]}
            elif voice == NARRATOR:
                result["unvoiced"].append(name)
            elif isinstance(voice, dict) and name in speakers:
                result["speakers"][name] = _speaker_entry(voice, where)
        except CastError as exc:
            problems.append(str(exc))
            continue
        if avatar:
            if actor in result["avatars"] and result["avatars"][actor] != avatar:
                problems.append("actor id %r would get two different avatars; give one of them another \"as\"" % actor)
                continue
            result["avatars"][actor] = avatar
        result["members"][name] = {"is": member, "costume": costume, "effect": effect, "stock": spec.get("stock"), "auto": name in auto and name not in casting,
                                   "actor": actor if avatar else None, "voice_name": voice.get("voice_name") if isinstance(voice, dict) else None}
    _require(not problems, "cast: " + "; ".join(problems))
    return result


def apply_story(story: Dict[str, Any], expanded: Dict[str, Any], registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The story with its voice block set from the expansion; explicit story settings other than the cast's stay."""
    registry = registry or load_registry()
    out = copy.deepcopy(story)
    voice = merge(registry.get("voice_defaults") or {}, out.get("voice") or {})
    voice.update(copy.deepcopy(expanded["voice"]))
    speakers = dict(voice.get("speakers") or {})
    for name in expanded["unvoiced"]:
        speakers.pop(name, None)
    speakers.update(copy.deepcopy(expanded["speakers"]))
    if speakers:
        voice["speakers"] = speakers
    out["voice"] = voice
    names = list(out.get("names") or [])
    for name in expanded["names"]:
        if name not in names:
            names.append(name)
    out["names"] = names
    return out


def apply_shotlist(shotlist: Dict[str, Any], expanded: Dict[str, Any]) -> List[str]:
    """Set the expanded avatars in a shot list's cast in place; returns the actor ids that changed."""
    cast = shotlist.setdefault("cast", {})
    changed = []
    for actor, avatar in expanded["avatars"].items():
        if cast.get(actor) != avatar:
            cast[actor] = copy.deepcopy(avatar)
            changed.append(actor)
    return changed


def check_shotlist(shotlist: Dict[str, Any], expanded: Dict[str, Any]) -> List[str]:
    """Where a shot list's cast differs from the expansion: a member must look the same in every video."""
    cast = shotlist.get("cast") or {}
    problems = []
    for actor, avatar in expanded["avatars"].items():
        if actor not in cast:
            continue
        if cast[actor] != avatar:
            problems.append("shotlist.json: cast.%s differs from the cast registry; run python -m pipeline.cast expand --job J --write, "
                            "or override it for this job in story.json casting" % actor)
    return problems


def write_job(job: Path, registry: Optional[Dict[str, Any]] = None, narrator: Optional[str] = None) -> Dict[str, Any]:
    """Expand the job's story and write story.json, shotlist.json (when present) and cast.json."""
    registry = registry or load_registry()
    job = Path(job)
    story = story_mod.validate_story(story_mod.load_json(job / "story.json"))
    if narrator:
        casting = dict(story.get("casting") or {})
        held = casting.get(NARRATOR)
        casting[NARRATOR] = dict(held, **{"is": narrator}) if isinstance(held, dict) else narrator
        story["casting"] = casting
    expanded = expand(story, registry)
    updated = apply_story(story, expanded, registry)
    if "lines" in updated:
        updated.pop("script", None)
    _dump(job / "story.json", updated)
    changed = []  # type: List[str]
    if (job / "shotlist.json").is_file():
        shotlist = story_mod.load_json(job / "shotlist.json")
        changed = apply_shotlist(shotlist, expanded)
        if changed:
            _dump(job / "shotlist.json", shotlist)
    _dump(job / "cast.json", dict(expanded, shotlist_changed=changed))
    return expanded


def sheet_job(out: Path, ids: Optional[List[str]] = None, registry: Optional[Dict[str, Any]] = None, set_name: str = "street",
              anchor: str = "sidewalk_mid", pose: str = "explain") -> List[str]:
    """Write a job (story.json, shotlist.json) with one full-figure shot per member, for pipeline.preview."""
    registry = registry or load_registry()
    ids = ids or list(registry["characters"])
    for name in ids:
        _require(name in registry["characters"], "%r is not a pool member" % name)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    story = {"version": 1, "slug": "cast-sheet", "title": "Cast sheet", "source": {"kind": "original", "note": "the cast pool, one shot each; no story"},
             "names": [registry["characters"][name]["name"] for name in ids], "display_overrides": {}, "voice": {"provider": "say", "target_wpm": 200},
             "script": " ".join(registry["characters"][name]["name"] + "." for name in ids)}
    shots = [{"id": name, "text": registry["characters"][name]["name"] + ".", "set": set_name, "time": "day",
              "cast": [{"who": name, "at": anchor, "facing": 180, "pose": pose, "face": "neutral"}],
              "camera": {"on": name, "type": "wide", "frame": "full", "yaw": 18, "move": "static", "locked": True}} for name in ids]
    _dump(out / "story.json", story)
    _dump(out / "shotlist.json", {"version": 1, "cast": {name: registry["characters"][name]["avatar"] for name in ids}, "shots": shots})
    return ids


def _dump(path: Path, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, ensure_ascii=False)
        handle.write("\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.cast", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, help="another registry file (default pipeline/cast.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    grow = sub.add_parser("expand")
    grow.add_argument("--job", required=True, type=Path)
    grow.add_argument("--narrator", help="the pool member who narrates (written to story.json casting.narrator with --write)")
    grow.add_argument("--write", action="store_true", help="write story.json voice, shotlist.json cast and cast.json")
    sheet = sub.add_parser("sheet")
    sheet.add_argument("--out", required=True, type=Path)
    sheet.add_argument("--ids", help="comma separated pool ids (default: all)")
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.registry)
        if args.command == "list":
            for name, entry in registry["characters"].items():
                print("%-9s %-9s voice %-8s avatar %s  %s" % (name, entry["name"], entry["voice"].get("voice_name"), json.dumps(entry["avatar"]), entry.get("look", "")))
            for name, costume in (registry.get("costumes") or {}).items():
                print("costume %-11s %s" % (name, costume.get("about", "")))
            for name, effect in (registry.get("effects") or {}).items():
                print("effect  %-11s %s" % (name, effect.get("about", "")))
            for group, choices in (registry.get("stock_voices") or {}).items():
                print("stock   %-11s %s" % (group, ", ".join(str(choice.get("voice_name")) for choice in choices)))
            return 0
        if args.command == "sheet":
            ids = sheet_job(args.out.expanduser(), [piece.strip() for piece in args.ids.split(",") if piece.strip()] if args.ids else None, registry)
            print("wrote %s: shots %s" % (args.out, ",".join(ids)))
            return 0
        job = args.job.expanduser()
        if args.write:
            expanded = write_job(job, registry, args.narrator)
        else:
            expanded = expand(story_mod.validate_story(story_mod.load_json(job / "story.json")), registry, args.narrator)
        print(json.dumps(expanded, indent=1))
        return 0
    except (CastError, story_mod.StoryError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
