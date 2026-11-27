"""Model passes over the corpus: structure and speech from the transcript text, voices from the audio.

annotate_corpus sends each transcript as numbered sentences to a cheap text model and stores what it
returns (genre, hook, beats, turn, ending, who speaks which words) in annotations/<id>.json.
voices_sample sends audio to a Gemini model, which hears how many distinct voices speak and which
sentences belong to which voice, and stores it in voices/<id>.json.
"""
from __future__ import annotations

import concurrent.futures
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import llm, textstats
from .corpus import read_json, write_json

GENRES = ["scary", "rule_world", "wholesome", "comedy", "drama", "mystery", "other"]
HOOKS = ["rule", "event", "warning", "question", "character", "confession", "claim", "other"]
ENDINGS = ["twist", "cliffhanger", "loop", "resolution", "moral", "punchline", "open", "cta"]
BEATS = ["hook", "world", "character", "problem", "attempt", "escalation", "turn", "climax", "aftermath", "ending", "cta"]

ANNOTATE_PROMPT = """You are annotating the transcript of a short vertical video (a Roblox "story short") for a study of how these scripts are written. The transcript came from speech recognition, so punctuation and a few words may be wrong.

Title: {title}
Length: {duration} seconds

Sentences, numbered from 0:
{numbered}

Return one JSON object and nothing else, with exactly these keys:
- "is_story": true when this is a narrated or acted story with a beginning and an end (false for gameplay talk, memes, song lyrics, lists, promos, or transcripts too garbled to follow).
- "format": "narrated" (one storyteller, any dialogue is quoted inside the narration), "acted" (mostly characters talking to each other), "mixed", or "other".
- "genre": one of {genres}. Use "rule_world" for "in this world/town, everyone ..." premises that are not meant to frighten. Use "scary" for horror, creepy and unsettling stories, including rule worlds that are meant to frighten.
- "scary": true or false, is it meant to frighten or unsettle.
- "person": "first", "second", "third" or "mixed" (the narration's grammatical person).
- "tense": "past", "present" or "mixed": the tense of the storytelling once the story proper is under way (a present-tense rule followed by a story told in past tense is "past").
- "hook": one of {hooks}: what the first sentence does. "rule" states a rule of the world, "event" drops into something happening, "warning" tells the viewer or a character not to do something, "question" asks, "character" introduces a person, "confession" is the narrator admitting something, "claim" is a surprising statement of fact.
- "premise_sentence": the number of the earliest sentence by whose end a viewer who knows nothing could say in one line what this video is about (its rule, situation or threat). It is usually 0, 1 or 2. Do not wait for the main conflict or the turn.
- "turn_sentence": the number of the sentence where the main turn or reveal begins, or null if there is none.
- "beats": a list of {{"s": sentence number where the beat starts, "label": one of {beats}}} in order, covering the whole transcript in 3 to 9 beats.
- "ending": one of {endings}: "twist" recasts what came before, "cliffhanger" stops on unresolved danger, "loop" returns to the opening line so a replay runs on, "resolution" settles the story, "moral" states a lesson, "punchline" is a joke, "open" just stops, "cta" asks the viewer to do something.
- "loops": true when the final sentence repeats, restates or completes the opening sentence (the same words, or the same rule said again), so that a replay runs on from the end into the start.
- "viewer_address": true when the script talks to the viewer ("you", "subscribe", "comment").
- "speech": every stretch of direct speech (words a character says aloud, texts or writes), in order, as {{"s": sentence number, "speaker": short lowercase name or role such as "mom", "jake", "me", "the voice", "words": the exact spoken words copied from the transcript, "intro": "before" if a tag such as "she said" or "he whispered" comes before the words, "after" if it comes after, "none" if the words stand alone}}. Do not include narration, thoughts, or reported speech.
- "characters": lowercase names or roles of the people in the story, narrator included as "me" when first person.
- "named": the proper names used for characters.
- "gore": 0 (none), 1 (mild, implied), 2 (blood or injury described), 3 (graphic).
- "devices": up to four short lowercase tags for what makes it work, such as "countdown", "rule broken", "impostor voice", "planted object", "repeated line", "wrong detail", "false relief".
"""

VOICES_PROMPT = """Listen to this short video's audio. Its speech-recognition transcript is below as numbered sentences.

{numbered}

Return one JSON object and nothing else:
- "voices": the distinct speaking voices you hear, as a list of {{"id": "V1", "role": "narrator" or a short character role, "description": age, gender and tone in a few words, "synthetic": true if it sounds like text-to-speech, "processing": any audible effect such as "phone filter", "pitch shifted", "reverb", or "none"}}. V1 is the voice that speaks most.
- "turns": for every sentence that is NOT spoken entirely by V1, {{"s": sentence number, "voice": the id of the voice speaking it, or "mixed" when two voices share it}}.
- "music": true if there is background music.
- "sound_effects": "none", "few" or "many".
- "singing": true if the words are mostly sung.
"""


def numbered_sentences(text: str) -> List[str]:
    return textstats.sentences(text)


def _numbered(sentences: List[str]) -> str:
    return "\n".join("%d. %s" % (index, sentence) for index, sentence in enumerate(sentences))


def annotate_one(transcript: Dict[str, Any], meta: Dict[str, Any], client: llm.Client, **options: Any) -> Dict[str, Any]:
    sentences = numbered_sentences(transcript["text"])
    prompt = ANNOTATE_PROMPT.format(title=meta.get("title") or "", duration=int(round(transcript.get("duration") or meta.get("duration") or 0)),
                                    numbered=_numbered(sentences), genres=", ".join(GENRES), hooks=", ".join(HOOKS),
                                    beats=", ".join(BEATS), endings=", ".join(ENDINGS))
    reply = client(prompt, tier="cheap", max_tokens=6000, purpose="corpus annotate", effort="medium", **options)
    if reply.get("truncated"):
        raise llm.LLMError("annotation for %s was cut off" % transcript["id"])
    data = llm.extract_json(reply["text"])
    if not isinstance(data, dict):
        raise llm.LLMError("annotation for %s is not an object" % transcript["id"])
    data.update({"id": transcript["id"], "sentences": len(sentences), "model": reply["model"]})
    return data


def _pending(corpus: Path, folder: str, limit: Optional[int]) -> List[str]:
    done = {path.stem for path in (corpus / folder).glob("*.json")}
    todo = [path.stem for path in sorted((corpus / "transcripts").glob("*.json")) if path.stem not in done]
    return todo[:limit] if limit else todo


def _usable(transcript: Dict[str, Any]) -> bool:
    return transcript.get("language") == "en" and len(textstats.words(transcript.get("text") or "")) >= 40


def annotate_corpus(corpus: Path, provider: Optional[str], model: Optional[str], ceiling: float, workers: int,
                    limit: Optional[int] = None, client: Optional[llm.Client] = None) -> Dict[str, Any]:
    ledger = llm.run_ledger(ceiling)
    client = client or llm.complete
    tally = {"annotated": 0, "skipped": 0, "failed": 0}

    def run(video: str) -> str:
        transcript = read_json(corpus / "transcripts" / ("%s.json" % video))
        meta_path = corpus / "meta" / ("%s.json" % video)
        meta = read_json(meta_path) if meta_path.exists() else {}
        if not _usable(transcript):
            write_json(corpus / "annotations" / ("%s.json" % video), {"id": video, "is_story": False, "skipped": "not English or under 40 words"})
            return "skipped"
        try:
            write_json(corpus / "annotations" / ("%s.json" % video), annotate_one(transcript, meta, client, provider=provider, model=model, ledger=ledger))
            return "annotated"
        except llm.BudgetError:
            return "budget"
        except llm.LLMError as exc:
            write_json(corpus / "annotations_failed" / ("%s.json" % video), {"id": video, "error": str(exc)})
            return "failed"

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for status in pool.map(run, _pending(corpus, "annotations", limit)):
            tally[status] = tally.get(status, 0) + 1
    tally["spent_usd"] = round(ledger.run_spent(), 4)
    return tally


def voices_one(corpus: Path, video: str, client: llm.Client, **options: Any) -> Dict[str, Any]:
    transcript = read_json(corpus / "transcripts" / ("%s.json" % video))
    audio = sorted((corpus / "audio").glob(video + ".*"))
    if not audio:
        raise llm.LLMError("no audio for %s" % video)
    sentences = numbered_sentences(transcript["text"])
    reply = client(VOICES_PROMPT.format(numbered=_numbered(sentences)), max_tokens=4000, temperature=0, purpose="corpus voices",
                   audio=audio[0], want_json=True, **options)
    data = llm.extract_json(reply["text"])
    if not isinstance(data, dict):
        raise llm.LLMError("voices for %s is not an object" % video)
    data.update({"id": video, "sentences": len(sentences), "model": reply["model"]})
    return data


def voices_sample(corpus: Path, provider: Optional[str], model: Optional[str], ceiling: float, sample: int, workers: int,
                  client: Optional[llm.Client] = None, seed: int = 7) -> Dict[str, Any]:
    """Voice turns from audio for a seeded sample of story shorts (sample 0 means all of them)."""
    ledger = llm.run_ledger(ceiling)
    client = client or llm.complete
    stories = []
    for path in sorted((corpus / "annotations").glob("*.json")):
        if read_json(path).get("is_story") and not (corpus / "voices" / path.name).exists():
            stories.append(path.stem)
    random.Random(seed).shuffle(stories)
    chosen = stories[:sample] if sample else stories
    tally = {"voiced": 0, "failed": 0}

    def run(video: str) -> str:
        try:
            write_json(corpus / "voices" / ("%s.json" % video),
                       voices_one(corpus, video, client, provider=provider or "gemini", model=model or "gemini-3.1-flash-lite", ledger=ledger))
            return "voiced"
        except llm.BudgetError:
            return "budget"
        except llm.LLMError as exc:
            write_json(corpus / "voices_failed" / ("%s.json" % video), {"id": video, "error": str(exc)})
            return "failed"

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for status in pool.map(run, chosen):
            tally[status] = tally.get(status, 0) + 1
    tally["spent_usd"] = round(ledger.run_spent(), 4)
    return tally
