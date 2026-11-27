"""The shorts backlog: find Roblox story shorts, fetch audio and counts, transcribe, annotate, index, search.

Usage:
  python -m pipeline.writer corpus update     --corpus DIR [--route yt-dlp] [--no-discover] [--pause 1.0]
  python -m pipeline.writer corpus discover   --corpus DIR [--sources FILE] [--per-channel 45] [--per-tag 40]
  python -m pipeline.writer corpus fetch      --corpus DIR [--workers 4] [--pause 0] [--limit N]
  python -m pipeline.writer corpus refresh    --corpus DIR [--workers 4]
  python -m pipeline.writer corpus rank       --corpus DIR
  python -m pipeline.writer corpus transcribe --corpus DIR [--model large-v3-turbo] [--workers 2]
  python -m pipeline.writer corpus annotate   --corpus DIR [--provider P --model M --ceiling 1.0]
  python -m pipeline.writer corpus voices     --corpus DIR [--sample 0] [--ceiling 1.0]
  python -m pipeline.writer corpus index      --corpus DIR
  python -m pipeline.writer corpus search     --corpus DIR [WORDS] [--channel C] [--genre scary] [--stories] [--min-views N] [--sort views_over_median]
  python -m pipeline.writer corpus export     --corpus DIR --out FILE.jsonl|FILE.csv [--stories] [--no-transcripts]

One record per short lives in DIR/corpus.sqlite (shorts, snapshots, transcripts, annotations, voices),
built by "index" from the working files beside it (meta/, audio/, transcripts/, annotations/, voices/).
"update" is the standing run: add new shorts from the tracked channels, hashtags and searches, fetch
them, re-read every short's counts into a new snapshot, rank within channel, and rebuild the index.
The folder is job data: transcripts of other people's videos never belong in the repository.
discover, fetch, refresh and rank need yt-dlp on PATH; transcribe needs the faster-whisper package
(the "writer" extra) and ffmpeg; annotate and voices call a model and read its key from the environment.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import math
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import collectors

HERE = Path(__file__).resolve().parent
BLOCK_LIMIT = 8
NON_LATIN = re.compile(r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u097f\u3040-\u30ff\u4e00-\u9fff]")


class CorpusError(RuntimeError):
    """Raised for failures the operator can act on."""


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=1)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def percentile_rank(views: List[int], value: int) -> float:
    """Share of the other entries with strictly fewer views."""
    if len(views) < 2:
        return 0.5
    return round(min(1.0, sum(1 for other in views if other < value) / float(len(views) - 1)), 4)


def median(values: List[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def pick_weighted(entries: List[Dict[str, Any]], cap: int, seed: int) -> List[Dict[str, Any]]:
    """Half the cap from the top by views, the rest a seeded sample of the remainder."""
    ordered = sorted(entries, key=lambda entry: -(entry.get("view_count") or 0))
    if len(ordered) <= cap:
        return ordered
    top = ordered[:int(math.ceil(cap / 2.0))]
    rest = ordered[len(top):]
    random.Random(seed).shuffle(rest)
    return top + rest[:cap - len(top)]


def channel_listing(collector: collectors.Collector, channel: str) -> Dict[str, Any]:
    data = collector.list_channel(channel)
    views = [int(entry.get("view_count") or 0) for entry in data["videos"]]
    return {
        "channel": data["channel"], "channel_id": data["channel_id"], "listed": len(views), "listed_on": utc_today(),
        "median_views": median([float(value) for value in views]),
        "videos": [{"id": entry["id"], "title": entry.get("title") or "", "view_count": views[index], "channel_pct": percentile_rank(views, views[index])}
                   for index, entry in enumerate(data["videos"])],
    }


def discover(corpus: Path, sources: Dict[str, Any], per_channel: int, per_tag: int, collector: collectors.Collector, seed: int = 7) -> Dict[str, int]:
    """Add shorts from every tracked channel, hashtag and search to candidates.jsonl. Shorts already there are kept."""
    jobs = [("channel", name) for name in sources.get("channels", [])]
    jobs += [("hashtag", name) for name in sources.get("hashtags", [])]
    jobs += [("search", name) for name in sources.get("searches", [])]

    def run(job: Tuple[str, str]) -> Tuple[Tuple[str, str], Any]:
        kind, name = job
        try:
            if kind == "channel":
                return job, channel_listing(collector, name)
            if kind == "hashtag":
                return job, collector.list_hashtag(name, 400)
            return job, collector.search(name, 80)
        except collectors.CollectError as exc:
            return job, exc

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        results = list(pool.map(run, jobs))
    existing = {row["id"]: row for row in read_jsonl(corpus / "candidates.jsonl")}
    counts = {}
    for (kind, name), data in results:
        key = "%s:%s" % (kind, name)
        if isinstance(data, collectors.CollectError):
            print("warning: %s: %s" % (key, data), file=sys.stderr)
            counts[key] = 0
            continue
        if kind == "channel":
            write_json(corpus / "channels" / ("%s.json" % data["channel_id"]), data)
            known = [video for video in data["videos"] if video["id"] in existing]
            fresh = [video for video in data["videos"] if video["id"] not in existing]
            picked = pick_weighted(fresh, max(0, per_channel - len(known)), seed) if known else pick_weighted(fresh, per_channel, seed)
            rows = [{"id": video["id"], "title": video["title"], "view_count": video["view_count"],
                     "channel": data["channel"], "channel_id": data["channel_id"]} for video in picked]
        else:
            entries = [entry for entry in data if re.search(r"[A-Za-z]{3}", entry.get("title") or "") and not NON_LATIN.search(entry.get("title") or "")]
            picked = pick_weighted(entries, per_tag, seed)
            rows = [{"id": entry["id"], "title": entry.get("title") or "", "view_count": int(entry.get("view_count") or 0),
                     "channel": entry.get("channel"), "channel_id": entry.get("channel_id")} for entry in picked]
        added = 0
        for row in rows:
            if row["id"] not in existing:
                row["found_by"] = key
                existing[row["id"]] = row
                added += 1
        counts[key] = added
    write_jsonl(corpus / "candidates.jsonl", existing.values())
    return counts


def utc_today() -> str:
    """Today's date in UTC, so machines in different time zones stamp the same collection day."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _snapshot(info: Dict[str, Any], today: str) -> Dict[str, Any]:
    return {"collected": today, "view_count": info.get("view_count"), "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"), "channel_followers": info.get("channel_follower_count")}


def _trim_info(info: Dict[str, Any], candidate: Dict[str, Any], today: str) -> Dict[str, Any]:
    return {
        "id": info["id"],
        "url": "https://www.youtube.com/shorts/%s" % info["id"],
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_handle": info.get("uploader_id"),
        "channel_followers": info.get("channel_follower_count"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "tags": info.get("tags") or [],
        "language": info.get("language"),
        "retrieved": today,
        "first_collected": today,
        "found_by": candidate.get("found_by"),
        "history": [_snapshot(info, today)],
    }


def fetch_one(corpus: Path, candidate: Dict[str, Any], today: str, collector: collectors.Collector, pause: float = 0.0,
              state: Optional[Dict[str, int]] = None) -> str:
    video = candidate["id"]
    meta_path = corpus / "meta" / ("%s.json" % video)
    audio_dir = corpus / "audio"
    if meta_path.exists() and list(audio_dir.glob(video + ".*")):
        return "have"
    if state is not None and state.get("blocked", 0) >= BLOCK_LIMIT:
        return "blocked"
    if pause:
        time.sleep(pause * (0.5 + random.random()))
    try:
        info = collector.fetch(video, audio_dir)
    except collectors.Blocked:
        if state is not None:
            state["blocked"] = state.get("blocked", 0) + 1
        return "blocked"
    except collectors.CollectError as exc:
        return "error: %s" % exc
    if state is not None:
        state["blocked"] = 0
    if info is None:
        return "skipped"
    write_json(meta_path, _trim_info(info, candidate, today))
    return "fetched"


def fetch(corpus: Path, workers: int, collector: collectors.Collector, limit: Optional[int] = None, pause: float = 0.0) -> Dict[str, int]:
    """Download audio and metadata for every candidate not fetched yet.

    YouTube starts refusing a data-centre address after a few hundred downloads ("Sign in to confirm
    you're not a bot"). After BLOCK_LIMIT refusals in a row the rest are left as "blocked" for a later
    run, from a home connection or with a pause between requests.
    """
    candidates = read_jsonl(corpus / "candidates.jsonl")
    if limit:
        candidates = candidates[:limit]
    today = utc_today()
    tally = {}
    failures = []
    state = {"blocked": 0}
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for candidate, status in zip(candidates, pool.map(lambda item: fetch_one(corpus, item, today, collector, pause, state), candidates)):
            key = status.split(":")[0]
            tally[key] = tally.get(key, 0) + 1
            if key == "error":
                failures.append({"id": candidate["id"], "error": status})
    write_jsonl(corpus / "fetch_failures.jsonl", failures)
    return tally


def refresh(corpus: Path, workers: int, collector: collectors.Collector, pause: float = 0.0) -> Dict[str, int]:
    """Re-read every short's counts and add them to its history, at most one snapshot a day."""
    today = utc_today()
    paths = sorted((corpus / "meta").glob("*.json"))
    state = {"blocked": 0}

    def run(path: Path) -> str:
        meta = read_json(path)
        history = meta.setdefault("history", [{"collected": meta.get("retrieved"), "view_count": meta.get("view_count"), "like_count": meta.get("like_count"),
                                               "comment_count": meta.get("comment_count"), "channel_followers": meta.get("channel_followers")}])
        if history and history[-1].get("collected") == today:
            return "current"
        if state["blocked"] >= BLOCK_LIMIT:
            return "blocked"
        if pause:
            time.sleep(pause * (0.5 + random.random()))
        try:
            info = collector.counts(meta["id"])
        except collectors.Blocked:
            state["blocked"] += 1
            return "blocked"
        except collectors.CollectError:
            return "gone"
        state["blocked"] = 0
        history.append(_snapshot(info, today))
        meta.update({"view_count": info.get("view_count"), "like_count": info.get("like_count"), "comment_count": info.get("comment_count"),
                     "channel_followers": info.get("channel_follower_count"), "retrieved": today})
        meta.setdefault("first_collected", history[0].get("collected"))
        write_json(path, meta)
        return "refreshed"

    tally = {}
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for status in pool.map(run, paths):
            tally[status] = tally.get(status, 0) + 1
    return tally


def rank(corpus: Path, collector: collectors.Collector, relist: bool = False) -> Dict[str, int]:
    """Give every fetched short its place among its own channel's shorts."""
    metas = [read_json(path) for path in sorted((corpus / "meta").glob("*.json"))]
    wanted = sorted({meta["channel_id"] for meta in metas if meta.get("channel_id")
                     and (relist or not (corpus / "channels" / ("%s.json" % meta["channel_id"])).exists())})

    def run(channel_id: str) -> None:
        try:
            write_json(corpus / "channels" / ("%s.json" % channel_id), channel_listing(collector, channel_id))
        except collectors.CollectError as exc:
            if not (corpus / "channels" / ("%s.json" % channel_id)).exists():
                write_json(corpus / "channels" / ("%s.json" % channel_id), {"channel_id": channel_id, "listed": 0, "videos": [], "error": str(exc)})

    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        list(pool.map(run, wanted))
    ranked = 0
    for meta in metas:
        listing_path = corpus / "channels" / ("%s.json" % meta.get("channel_id"))
        listing = read_json(listing_path) if listing_path.exists() else {"listed": 0, "videos": []}
        views = [video["view_count"] for video in listing["videos"]]
        meta["channel_listed"] = listing["listed"]
        meta["channel_median_views"] = listing.get("median_views")
        meta["channel_pct"] = None
        meta["views_over_median"] = None
        if listing["listed"] >= 8 and meta.get("view_count") is not None:
            meta["channel_pct"] = percentile_rank(views, meta["view_count"])
            if listing.get("median_views"):
                meta["views_over_median"] = round(meta["view_count"] / float(listing["median_views"]), 3)
            ranked += 1
        write_json(corpus / "meta" / ("%s.json" % meta["id"]), meta)
    return {"shorts": len(metas), "ranked": ranked, "channels_listed": len(wanted)}


def _transcribe_batch(args: Tuple[str, List[str], str, int]) -> int:
    corpus_text, paths, model_name, threads = args
    import numpy
    from faster_whisper import WhisperModel
    corpus = Path(corpus_text)
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    done = 0
    for audio_text in paths:
        audio = Path(audio_text)
        target = corpus / "transcripts" / ("%s.json" % audio.stem)
        if target.exists():
            continue
        try:
            decoded = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(audio), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            samples = numpy.frombuffer(decoded.stdout, dtype=numpy.float32)
            segments, info = model.transcribe(samples, word_timestamps=True, vad_filter=True, beam_size=5,
                                              condition_on_previous_text=False)
            rows = []
            words = []
            for segment in segments:
                rows.append({"start": round(segment.start, 2), "end": round(segment.end, 2), "text": segment.text.strip()})
                for word in segment.words or []:
                    words.append({"w": word.word.strip(), "start": round(word.start, 2), "end": round(word.end, 2)})
            write_json(target, {"id": audio.stem, "model": model_name, "language": info.language,
                                "language_probability": round(info.language_probability, 3), "duration": round(info.duration, 2),
                                "text": " ".join(row["text"] for row in rows), "segments": rows, "words": words})
            done += 1
        except Exception as exc:
            write_json(corpus / "transcripts_failed" / ("%s.json" % audio.stem), {"id": audio.stem, "error": repr(exc)})
    return done


def transcribe(corpus: Path, model_name: str, workers: int, threads: int) -> int:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise CorpusError("transcribe needs faster-whisper: python -m pip install -e '.[writer]'")
    todo = [str(path) for path in sorted((corpus / "audio").iterdir())
            if not (corpus / "transcripts" / ("%s.json" % path.stem)).exists()]
    if not todo:
        return 0
    batches = [(str(corpus), todo[index::workers], model_name, threads) for index in range(workers)]
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        return sum(pool.map(_transcribe_batch, [batch for batch in batches if batch[1]]))


def load_sources(path: Optional[str]) -> Dict[str, Any]:
    return read_json(Path(path) if path else HERE / "sources.json")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.writer corpus", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "discover", "fetch", "refresh", "rank", "transcribe", "annotate", "voices", "index", "search", "export"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--corpus", required=True)
        if name in ("update", "discover", "fetch", "refresh", "rank"):
            cmd.add_argument("--route", default="yt-dlp")
        if name in ("update", "discover"):
            cmd.add_argument("--sources")
            cmd.add_argument("--per-channel", type=int, default=45)
            cmd.add_argument("--per-tag", type=int, default=40)
        if name in ("update", "fetch", "refresh"):
            cmd.add_argument("--workers", type=int, default=4)
            cmd.add_argument("--pause", type=float, default=0.0)
        if name == "update":
            cmd.add_argument("--no-discover", action="store_true")
        if name == "fetch":
            cmd.add_argument("--limit", type=int)
        if name == "rank":
            cmd.add_argument("--relist", action="store_true")
        if name == "transcribe":
            cmd.add_argument("--model", default="large-v3-turbo")
            cmd.add_argument("--workers", type=int, default=2)
            cmd.add_argument("--threads", type=int, default=4)
        if name in ("annotate", "voices"):
            cmd.add_argument("--provider", default=None)
            cmd.add_argument("--model", default=None)
            cmd.add_argument("--ceiling", type=float, default=1.0)
            cmd.add_argument("--workers", type=int, default=4)
        if name == "annotate":
            cmd.add_argument("--limit", type=int)
        if name == "voices":
            cmd.add_argument("--sample", type=int, default=0)
        if name in ("search", "export"):
            cmd.add_argument("--channel")
            cmd.add_argument("--genre")
            cmd.add_argument("--stories", action="store_true")
            cmd.add_argument("--min-views", type=int)
        if name == "search":
            cmd.add_argument("words", nargs="*")
            cmd.add_argument("--sort", default="views", choices=["views", "views_per_day", "views_over_median", "views_per_follower", "upload_date"])
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--text", action="store_true")
        if name == "export":
            cmd.add_argument("--out", required=True)
            cmd.add_argument("--no-transcripts", action="store_true")
    args = parser.parse_args(argv)
    corpus = Path(args.corpus)
    try:
        if args.command in ("update", "discover", "fetch", "refresh", "rank"):
            collector = collectors.get(args.route)
        if args.command == "update":
            from . import store
            if not args.no_discover:
                counts = discover(corpus, load_sources(args.sources), args.per_channel, args.per_tag, collector)
                print("discover: %d new shorts" % sum(counts.values()))
            print("fetch: %s" % json.dumps(fetch(corpus, args.workers, collector, None, args.pause)))
            print("refresh: %s" % json.dumps(refresh(corpus, args.workers, collector, args.pause)))
            print("rank: %s" % json.dumps(rank(corpus, collector, relist=True)))
            print("index: %s" % json.dumps(store.build(corpus)))
        elif args.command == "discover":
            counts = discover(corpus, load_sources(args.sources), args.per_channel, args.per_tag, collector)
            for key in sorted(counts):
                print("%4d  %s" % (counts[key], key))
            print("candidates.jsonl: %d shorts" % len(read_jsonl(corpus / "candidates.jsonl")))
        elif args.command == "fetch":
            print(json.dumps(fetch(corpus, args.workers, collector, args.limit, args.pause)))
        elif args.command == "refresh":
            print(json.dumps(refresh(corpus, args.workers, collector, args.pause)))
        elif args.command == "rank":
            print(json.dumps(rank(corpus, collector, args.relist)))
        elif args.command == "transcribe":
            print("transcribed %d" % transcribe(corpus, args.model, args.workers, args.threads))
        elif args.command == "annotate":
            from . import annotate
            print(json.dumps(annotate.annotate_corpus(corpus, args.provider, args.model, args.ceiling, args.workers, args.limit)))
        elif args.command == "voices":
            from . import annotate
            print(json.dumps(annotate.voices_sample(corpus, args.provider, args.model, args.ceiling, args.sample, args.workers)))
        else:
            from . import store
            if args.command == "index":
                print(json.dumps(store.build(corpus)))
            elif args.command == "search":
                rows = store.search(corpus, " ".join(args.words), channel=args.channel, genre=args.genre, stories=args.stories,
                                    min_views=args.min_views, sort=args.sort, limit=args.limit)
                store.print_rows(rows, args.text)
            else:
                print("%d shorts written to %s" % (store.export(corpus, Path(args.out), channel=args.channel, genre=args.genre, stories=args.stories,
                                                                 min_views=args.min_views, transcripts=not args.no_transcripts), args.out))
        return 0
    except (CorpusError, collectors.CollectError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
