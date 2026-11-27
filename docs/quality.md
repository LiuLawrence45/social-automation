# Quality stage

`pipeline.quality` judges the finished video on its own: it reads only rendered frames and the story's words, never how a shot was built. The camera judge in `pipeline.judge` picks angles before the render; this stage runs after it and decides whether each shot passes.

```
python -m pipeline.quality --job J --provider gemini --model gemini-3.8-flash
python -m pipeline.quality --job J --shots a2,d2,f1                 # re-judge re-rendered shots, merged into quality.json
python -m pipeline.quality --job J --judges posture,motion           # some judges only
python -m pipeline.quality --job J --report-only --human J/quality/human.json --baseline J/quality/first.json
python -m pipeline.quality --job J --report-only --compare J/quality.pass6.json
```

Reads `video.mp4` (no captions; `--video` overrides), `final.mp4` (captions, for the framing judge; `--final`), `shots.json`, `words.json`, `story.json`, the set of a shot from `timeline.json` or `shotlist.json` when shots.json lacks it, and `posture.json` when present. Writes `quality.json`, `QUALITY.md`, every sheet a judge saw under `quality/sheets/` and a spend ledger `quality/ledger.json`. With `--out other.json` the Markdown goes beside it as `other.md`. Python standard library and FFmpeg only; FFmpeg needs no drawtext, labels are drawn from a built-in 5x7 font.

## Judges

Each judge asks one question per shot about one labelled sheet of frames and returns a score from 0 to 10, the fail conditions it saw and a one-line reason.

| judge | frames it sees | fails on |
|---|---|---|
| posture | 3 across the shot, no captions | idle or default stance, A-pose or T-pose leftovers, arms held out, splayed or frozen limbs, bad joints, a body that does the opposite of the words |
| clipping | the same 3 | limbs through the body, hands through props, bodies through the set, floating, sunk feet, props not in the hand |
| framing | 2 from the captioned video | subject cut off, dead space, the caption on a face or on what the shot is about, no subject, blocked subject |
| lighting | 2 at phone size, plus measured brightness | unreadable in one second, subject lost in the dark, blown-out or glare artefacts; also fails by measurement when under `min_lit_fraction` of the pixels are lit |
| story | 3, with the shot's words and the script | wrong subject, the action in the words not shown, contradiction, wrong place, wrong on-screen text |
| continuity | 2 of this shot, the middle of the previous shot and of the last earlier shot in the same set | avatar or outfit changed, a blocky classic avatar beside detailed ones, position or prop jumps, time-of-day jumps, a set that changed |
| motion | 8 evenly spaced frames as a 4x2 flip-book, plus measured frame-to-frame change | sliding, pops, limb snaps, a character frozen like a statue, a camera move that starts or ends on nothing |
| video | one middle frame per shot on sheets of 18, asked three times, median score | pacing, repetition, the ending, consistency under the threshold |

A judge fails a shot when its score is under the threshold (6), or when it lists a fail condition and scores under `defect_pass_score` (8). A shot fails when any judge fails it. A judge that could not answer (model error, unusable reply, spend ceiling) leaves the shot `incomplete`; it is never counted as a pass. A judge may say a shot is not applicable (no character in an insert); that is a pass with no score.

Motion is judged on a dense frame strip, not on the clip itself: `pipeline.vision` sends images to both providers, a strip can be kept and read by a person as evidence, and the measured change between video frames is passed to the judge so a jump or a still shot does not depend on the eye alone. A model that takes video would see timing better; that needs a video path in `pipeline/vision.py`, which this stage does not touch.

## Geometry

`<job>/posture.json` (`{"version":1,"shots":{"<id>":{"status":"pass"|"fail","frames":[{"frame":123,"issues":[{"kind":...,"actor":...,"detail":...}]}]}}}`) is merged on every run, including `--report-only`. `intersect`, `floating` and `sunk` fail the clipping judge; `joint_limit`, `stiff_arms` and `idle` fail the posture judge; unknown kinds go to clipping. A geometric fail fails the judge even when the picture passed, and a picture fail stands when geometry passed. Each judge's entry keeps both under `geometry`.

## Settings

Rubrics live in `pipeline/quality_rubric.json`: the questions, fail conditions, thresholds, frames per shot, tile heights, layout and the model defaults. `story.json` `"quality"` overrides any of it (`{"model": "...", "judges": {"posture": {"frames": 4, "threshold": 7}}}`), and `--set posture.frames=4`, `--set video.votes=5` or `--set workers=12` override that. Prices come from `story.json` `judge.prices`, the table the camera judge uses.

Model calls go through `pipeline.vision.ask` in a thread pool (`workers`, default 8). Spend is one `vision.Budget` shared across threads: every call first reserves `reserve_usd`, so the ceiling holds even with calls in flight, and the budget starts from the total in `quality/ledger.json`, so the ceiling covers every run against the job. Token counts are measured; the dollar figure is as good as the price table.

## Shots, passes and comparison

`--shots` judges the listed shots and merges them into the existing report; the shot after each listed shot gets its continuity judged again, because its neighbour changed. Every judge entry records the video file and modification time it was judged on (`source`). To compare passes, keep a frozen copy of the earlier report (`cp quality.json quality.pass6.json`) and pass it with `--compare`: QUALITY.md gains a table per judge of shots failing before, failing now, fixed, newly failing and still failing.

## Calibration against a person

`--human FILE` takes a person's verdicts, `{"reader": "...", "notes": [...], "verdicts": {"a2": {"posture": "fail", "clipping": "pass"}}}`, and reports how many of them the judges match, which fails the judges passed and which passes they failed. `--baseline OLD.json` measures the same verdicts against an earlier report, which gives agreement before and after a rubric change. Read the same sheets the judges saw (`quality/sheets/`) and include shots the judges passed.

## Tests

`tests/test_quality.py` fakes the model as `tests/test_vision.py` does and needs no key, network or Studio; one test cuts real sheets and skips without FFmpeg.
