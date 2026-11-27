# Quality stage

`pipeline.quality` judges the finished video on its own: it reads only rendered frames and the story's words, never how a shot was built. The camera judge in `pipeline.judge` picks angles before the render; this stage runs after it and decides whether each shot passes.

```
python -m pipeline.quality --job J --provider gemini --model gemini-3.8-flash
python -m pipeline.quality --job J --shots a2,d2,f1                 # re-judge re-rendered shots, merged into quality.json
python -m pipeline.quality --job J --judges posture,motion           # some judges only
python -m pipeline.quality --job J --report-only --human J/quality/human.json --baseline J/quality/first.json
python -m pipeline.quality --job J --report-only --compare J/quality.pass6.json
python -m pipeline.quality --job J --report-only --posture J/pass6/posture_pass6.json   # merge another geometry file
python -m pipeline.quality --job J --confirm                         # judge what is missing or stale, and vote on borderline fails
```

Reads `video.mp4` (no captions; `--video` overrides), `final.mp4` (captions, for the framing judge; `--final`), `shots.json`, `words.json`, `story.json`, the set of a shot from `timeline.json` or `shotlist.json` when shots.json lacks it, and `posture.json` when present (`--posture` names another file). Writes `quality.json`, `QUALITY.md`, every sheet a judge saw under `quality/sheets/` and a spend ledger `quality/ledger.json`. With `--out other.json` the Markdown goes beside it as `other.md`. Python standard library and FFmpeg only; FFmpeg needs no drawtext, labels are drawn from a built-in 5x7 font.

## Judges

Each judge asks one question per shot about one labelled sheet of frames (motion gets two) and returns a score from 0 to 10, the fail conditions it saw and a one-line reason.

| judge | frames it sees | fails on |
|---|---|---|
| posture | 3 across the shot, no captions | idle or default stance, A-pose or T-pose leftovers, arms held out, splayed or frozen limbs, bad joints, a body that does the opposite of the words |
| expression | the same 3, with the script | a face that shows the opposite of the moment (smiling during a scare), a blank default face on fear, crying, pain or urgency, eyes or mouth stuck in one state; not applicable when no face can be read |
| clipping | the same 3 | limbs through the body, hands through props, bodies through the set, floating, sunk feet, props not in the hand |
| framing | 2 from the captioned video | subject cut off, dead space, the caption on a face or on what the shot is about, no subject, blocked subject |
| lighting | 2 at phone size, plus measured brightness | unreadable in one second, subject lost in the dark, blown-out or glare artefacts; also fails by measurement when under `min_lit_fraction` of the pixels are lit |
| story | 3, with the shot's words and the script | wrong subject, the action in the words not shown, contradiction, wrong place, wrong on-screen text, no visible evidence of the time of day the script has reached (after "the next morning" every shot must show daylight; a dim windowless corridor fails) |
| continuity | 2 of this shot, the middle of the previous shot and of the last earlier shot in the same set | avatar or outfit changed, a blocky classic avatar beside detailed ones, position or prop jumps, time-of-day jumps, a set that changed |
| motion | 8 evenly spaced frames as a 4x2 flip-book, a second sheet with the first, middle and last frame at full size, plus measured frame-to-frame change | sliding, pops, limb snaps, a character frozen like a statue, a camera move that starts or ends on nothing |
| video | one middle frame per shot on sheets of 18, asked three times, median score | pacing, repetition, the ending, consistency under the threshold |

A judge fails a shot when its score is under the threshold (6), or when it lists a fail condition and scores under `defect_pass_score` (8). A rubric may also name `flags`: yes-or-no questions the model must answer in its reply, each tied to a fail condition; a flag answered true fails the judge whatever the score. Motion has one, `frozen_character`: a visible character whose words or moment call for action, speech or a reaction keeps one pose from the first frame to the last. Breathing, a slight drift of the head, blinks and a moving camera do not count as acting. A shot fails when any judge fails it. A judge that could not answer (model error, unusable reply, spend ceiling) leaves the shot `incomplete`; it is never counted as a pass. A judge may say a shot is not applicable (no character in an insert); that is a pass with no score.

Motion is judged on a dense frame strip, not on the clip itself: `pipeline.vision` sends images to both providers, a strip can be kept and read by a person as evidence, and the measured change between video frames is passed to the judge so a jump or a still shot does not depend on the eye alone. The measured change is for the whole picture, so it cannot tell a still character from a still shot once the camera moves, which it does in most shots; that is why the frozen-character question is put to the judge on larger frames and not decided by the number. A model that takes video would see timing better; that needs a video path in `pipeline/vision.py`, which this stage does not touch.

## Borderline fails

One answer from a model is noisy near the line, so a fail is not recorded on one answer when it is borderline: the score is within `confirm.margin` (1) of the threshold, or no other judge fails the shot. The judge is asked again until it has `confirm.votes` (3) answers on the same sheet, the majority decides, the entry takes the answer of the middle-scoring vote on the winning side, and every vote is kept under `votes` with its status, score, fail conditions and reason. When the votes clear the only borderline fail beside one other fail, that other fail is now alone on its shot and is put to the vote as well. A clear fail on a shot that another judge also fails is recorded as it is, and so is any fail backed by geometry or by measured darkness: no vote can clear those. A vote that could not answer leaves the fail standing, to be asked again in a later run. A pass is never asked again. A run votes on the verdicts it judged; `--confirm` does what `--changed` does and also votes on every borderline fail already in the report that has no votes. QUALITY.md lists which fails stayed and which became passes, and `--set confirm.votes=1` turns the voting off.

## Geometry

`<job>/posture.json`, or the file given with `--posture`, is merged for the shots judged in a run: all of them, or the ones named with `--shots`, so a report that mixes re-rendered shots with older ones keeps each shot's geometry from the file that measured it. `--report-only` judges nothing and merges only a file named with `--posture`. Two shapes are read:

- current: `{"version":1,"describes":{"label":...},"shots":{"<id>":{"status":"pass"|"fail","current":true,"frames":[{"frame":123,"issues":[{"kind":...,"actor":...,"detail":...,"in_frame":true}]}]}}}`
- earlier (the posture report of the camera judge): `{"posture":{"shots":[{"id":...,"reasons":[...],"geometry":[{"code":...,"who":...,"detail":...,"seen":true,"at":[0,0.5],"depth":0.3}]}]}}`. Its status and its `vision_*` reasons come partly from a model and are ignored; the shot counts as a geometric fail only when a measured reason is listed.

`joint_limit`, `stiff_arms`, `idle`, `reach_miss`, `arm_out`, `arms_out`, `arms_splayed`, `stance_wide`, `no_action` and `hand_off_target` fail the posture judge; `intersect`, `floating`, `sunk`, `prop_missing` and any unknown kind (the `clip_*` codes among them) fail the clipping judge. Issues marked out of frame (`in_frame` or `seen` false) count only when the failed shot has no issue in frame. A shot marked `"current": false` was restaged after it was measured: it is not merged and the report lists it.

A geometric fail fails the judge even when the picture passed, and a picture fail stands when geometry passed. Each judge's entry keeps the geometric result under `geometry` and the picture's own verdict under `picture_status`; the summary lists per judge the shots failing on geometry alone (`geometry_only`), and each `geometry` result names its `source`: the file, its time and its `describes.label`. The file must describe the render being judged: the report of one pass merged with the geometry of another compares unlike things.

## Settings

Rubrics live in `pipeline/quality_rubric.json`: the questions, fail conditions, thresholds, frames per shot, tile heights, layout and the model defaults. `story.json` `"quality"` overrides any of it (`{"model": "...", "judges": {"posture": {"frames": 4, "threshold": 7}}}`), and `--set posture.frames=4`, `--set video.votes=5` or `--set workers=12` override that. `"accepted"` is a list of sentences every judge and the whole-video judge are told as "accepted for this video and not a defect": it is how one job records a ruling (`{"quality": {"accepted": ["The cast mixes detailed avatar styles on purpose; that mix is not a consistency defect. A default blocky classic avatar beside detailed ones still is."]}}`) without loosening the rubric for other videos. Prices come from `story.json` `judge.prices`, the table the camera judge uses.

Model calls go through `pipeline.vision.ask` in a thread pool (`workers`, default 8). Spend is one `vision.Budget` shared across threads: every call first reserves `reserve_usd`, so the ceiling holds even with calls in flight, and the budget starts from the total in `quality/ledger.json`, so the ceiling covers every run against the job. Token counts are measured; the dollar figure is as good as the price table.

## Shots, passes and comparison

`--shots` judges the listed shots and merges them into the existing report; the shot after each listed shot gets its continuity judged again, because its neighbour changed. Every judge entry records the video file and modification time it was judged on (`source`). Every verdict also keeps a fingerprint (`render`) of each shot on the sheet it was given on: 4x4 block means of five frames across the shot and the brightness of every frame, measured on the video that judge reads. On every run, `--report-only` included, the fingerprints are measured again; a verdict whose shot now differs by more than encoding the same picture again would cause (two grey levels) is set aside as `stale` with its old answer kept under `judged`, the shot counts as incomplete, and the report lists it. Continuity is set aside when a neighbour on its sheet changed, the whole-video verdict when any shot did. `--changed` judges exactly the missing, unanswered and stale verdicts again, so a report built over several batches holds the latest verdict for every shot as rendered, without judging unchanged shots twice; add `--judges continuity,video` on the final cut to refresh only those. The fingerprint is coarse: a change smaller than a 4x4 block of the 36x64 measuring frame that also leaves each frame's brightness within two levels would not be seen. To compare passes, keep a frozen copy of the earlier report (`cp quality.json quality.pass6.json`) and pass it with `--compare`: QUALITY.md gains a table per judge of shots failing before, failing now, fixed, newly failing and still failing; a shot whose verdict is missing or stale is never counted as fixed.

## Calibration against a person

`--human FILE` takes a person's verdicts, `{"reader": "...", "notes": [...], "verdicts": {"a2": {"posture": "fail", "clipping": "pass"}}}`, and reports how many of them the judges match, which fails the judges passed and which passes they failed. `--baseline OLD.json` measures the same verdicts against an earlier report, which gives agreement before and after a rubric change. The comparison is with what each judge said of the picture (`picture_status`), not with a fail that came from merged geometry, which a reader of frames cannot see. Read the same sheets the judges saw (`quality/sheets/`) and include shots the judges passed.

## Tests

`tests/test_quality.py` fakes the model as `tests/test_vision.py` does and needs no key, network or Studio; one test cuts real sheets and skips without FFmpeg.
