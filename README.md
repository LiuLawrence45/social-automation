# social-automation

Pipeline for narrated short-story videos staged in Roblox Studio: a story and a shot list go in, a 1080x1920 captioned video comes out. Python standard library only (3.9+); FFmpeg is the only external tool. Rendering needs a Mac with Roblox Studio open; everything else runs anywhere.

## Stages

Every stage reads and writes one job folder and can be re-run on its own.

| Stage | Command | Reads | Writes |
|---|---|---|---|
| Writer | `python -m pipeline.writer write --story S --out J` | a story as text or JSON | story.json, script.md, source.json, script_judge.json |
| Story | `python -m pipeline.story check|timeline --job J` | story.json, shotlist.json, words.json | timeline.json |
| Voice | `python -m pipeline.voice --job J [--reuse-raw]` | story.json | narration.wav, words.json, voice_report.json |
| Studio | `python -m pipeline.studio prepare|build-sets|load|info` | timeline.json | sets and cast in the open place |
| Judge | `python -m pipeline.judge shots|video|show|models --job J` | timeline.json, Studio, video.mp4 | judge.json, judge/ |
| Sound | `python -m pipeline.sound --job J [--dry-run]` | sound.json, timeline.json, words.json | bed.wav, sound_report.json, sfx/ |
| Render | `python -m pipeline.render sheet|video --job J` | timeline.json, judge.json, Studio | video.mp4, shots.json, render_report.json |
| Edit | `python -m pipeline.edit --job J --music J/bed.wav` | video.mp4, narration.wav, words.json, shots.json | final.mp4, edit_report.json |

Order for a new video: write story.json and shotlist.json, voice, `story timeline`, `studio build-sets`, `judge shots`, sound, `render video`, edit, `judge video`. `examples/found-box` holds a sample story and shot list. Stories, adapted scripts and renders are job data and never belong in this repository.

API keys are read from the environment at call time and never written anywhere: `ELEVENLABS_API_KEY` (voice, sound), `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` (judge).

## Writer

`python -m pipeline.writer` is the stage before the story stage. It keeps a backlog of Roblox story shorts with their transcripts and view counts, measures how they are written, and turns a story into the script half of `story.json` in that style. Model keys are read from the environment at call time (`ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`); set `WRITER_LEDGER=path/spend.jsonl` and every model call is appended to that file with its tokens and cost. Prices in `pipeline/writer/llm.py` are estimates to edit when they change.

### One command: story in, script out

```
python -m pipeline.writer write --story STORY --out JOB [--id ID] [--names Willow,Allan] [--seconds 55] [--wpm 250]
                                [--genre auto|scary|rule_world|wholesome|comedy|drama|mystery] [--retries 2] [--ceiling 0.5]
                                [--no-judge] [--brief FILE] [--provider P --model M --judge-model M --effort medium]
```

`STORY` is a plain text file (a short first line followed by a blank line is taken as the title) or a JSON file with `text` and optionally `title`, `author`, `url`; a JSON list of such records works with `--id`. Whatever provenance the record carries (`url`, `author`, `subreddit`, `retrieved`, `retrieved_via`, `license`) is copied to `story.json`'s `source`. The command drafts a script, judges it, rewrites it against the judge's findings until it passes, the retries run out or `--ceiling` dollars are spent, and keeps the best draft. It writes into `JOB`:

| File | What it holds |
|---|---|
| `story.json` | `title`, `slug`, `source`, `names`, `lines` with a speaker on every line, `script`, `voice.target_wpm`, and the writer's extra keys `cast` (a voice description for every speaker: age, gender, tone, processing), `sound_cues`, `music`, `writer` |
| `script.md` | the same script to read, with cues, voices and the judge table |
| `source.json` | the story as given |
| `script_judge.json` | every draft with its checks, rubric scores and fixes, and why the loop stopped |

An existing `story.json` in the job keeps its production fields (`voice`, `display_overrides`); only the script half is replaced. `--names` lists names that must appear: up to three go to speaking characters and the rest are seen or called out. The story stage ignores the extra keys; `voice.speakers` is left for the voice stage to fill.

### Script judge

```
python -m pipeline.writer judge --job JOB [--checks-only] [--rewrite] [--retries 2] [--ceiling 0.5] [--seconds 55] [--wpm 250] [--names A,B]
```

Eighteen measurable checks (length for the target seconds, hook length, sentence length, share and count of spoken lines, first spoken line, speakers, narrator quoting speech, speech tags, last line, filler and gore words, required names, reading grade, a cast entry per speaker) plus a model rubric of seven items scored 1 to 5 (premise clear in five seconds, filmable, dialogue sounds spoken, escalation, ending lands, tone safe, faithful to the source). A script passes when every check is ok, the rubric mean is at least `rubric_pass` and no item is under `rubric_floor`. `--checks-only` calls no model; `--rewrite` runs the same loop as `write` on a script that already exists. Exit status is 0 on a pass, 1 on a fail, 2 on an error.

The targets and rules live in `pipeline/writer/style.json`, which both the writer and the judge load (`--style FILE` swaps it). Its numbers come from the corpus study; `pipeline/writer/style_stats.json` holds the measured values behind them.

### Shorts backlog

```
python -m pipeline.writer corpus update     --corpus DIR [--route yt-dlp] [--no-discover] [--pause 1.0]
python -m pipeline.writer corpus discover   --corpus DIR [--sources FILE] [--per-channel 45] [--per-tag 40]
python -m pipeline.writer corpus fetch      --corpus DIR [--workers 4] [--pause 0] [--limit N]
python -m pipeline.writer corpus refresh    --corpus DIR [--workers 4]
python -m pipeline.writer corpus rank       --corpus DIR
python -m pipeline.writer corpus transcribe --corpus DIR [--model large-v3-turbo] [--workers 2] [--threads 4]
python -m pipeline.writer corpus annotate   --corpus DIR [--ceiling 1.0] [--workers 4]
python -m pipeline.writer corpus voices     --corpus DIR [--ceiling 1.0] [--workers 4]
python -m pipeline.writer corpus index      --corpus DIR
python -m pipeline.writer corpus search     --corpus DIR [WORDS] [--channel C] [--genre scary] [--stories] [--min-views N] [--sort views_over_median] [--text]
python -m pipeline.writer corpus export     --corpus DIR --out FILE.jsonl|FILE.csv [--stories] [--no-transcripts]
python -m pipeline.writer analyze           --corpus DIR [--out stats.json] [--features features.jsonl] [--examples examples.md]
```

`DIR` is job data outside the repository. `update` is the standing run: it lists the channels, hashtags and searches in `pipeline/writer/sources.json`, fetches audio and counts for new shorts, re-reads every short's counts into a new dated snapshot, ranks each short within its channel, and rebuilds `DIR/corpus.sqlite`. After new shorts arrive, `transcribe`, `annotate`, `voices` and `index` fill in the rest; each skips what is already done. `annotate` sends the transcript to a cheap text model for genre, hook, beats and who speaks which words; `voices` sends the audio to a cheap Gemini model that counts distinct voices.

`corpus.sqlite` has one row per short in `shorts` (counts, `views_per_day`, `views_per_follower`, `views_over_median`, `channel_pct`, genre, hook, ending, word and dialogue measures), one row per short per collection date in `snapshots`, the timed text in `transcripts`, the raw model passes in `annotations` and `voices`, and a full-text index. `analyze` writes derived statistics only (no transcript text) and compares genres and each channel's top quarter against the rest.

Collection goes through a `Collector` in `pipeline/writer/collectors.py`; `yt-dlp` is the one route today and a second is a subclass registered in `ROUTES` and picked with `--route`. YouTube refuses audio downloads from data-centre addresses after a few hundred requests, so fetch from a home connection with `--workers 2 --pause 1.0`; `fetch` stops after eight refusals in a row.

The writer and the judge need only the standard library. The backlog needs `yt-dlp`, `ffmpeg` and, for `transcribe`, `faster-whisper`: `python -m pip install -e '.[writer]'`. Transcripts, collected stories and adapted scripts are job data and never belong in this repository.

## Voice

`story.json` may carry `"lines": [{"speaker": "mara", "text": "..."}]` in reading order; the script is the lines' texts joined with single spaces, so everything downstream (shots, captions, word counts) treats it as plain text. A story without `lines` is one line spoken by `narrator`. `voice` keeps its meaning for the narrator; `voice.speakers` maps a speaker to its own `voice_id`, `voice_name`, `settings` (ElevenLabs voice settings) and `process`, and a speaker not listed uses the narrator voice. `process` post-processes that speaker's audio with FFmpeg: `pitch` (frequency ratio, duration unchanged), `tempo` (speed ratio, duration changes and the alignment is scaled with it), `filter` (an FFmpeg audio chain appended as given) and `gain_db`. `voice.segment_gap` (default 0.12 s) is the silence between segments.

Consecutive lines by one speaker are one segment. Each segment is generated on its own, with the neighbouring segments sent as `previous_text` and `next_text`, and cached under `voice_work/segments/` by a hash of voice, model, settings, text and neighbours, so a re-run bills only changed segments; `process` is applied after the cache, so tuning it bills nothing. The segments are joined into one take that goes through the usual tighten, pace, level and timing steps. Every word in `words.json` carries `"speaker"`, and `voice_report.json` has a `speakers` object (voice, segment count, characters sent and billed, cached segments, process). The `say` fallback uses its one voice for every speaker; processing still applies.

## Shot list

`shotlist.json` has a `cast` and a list of `shots`; each shot carries the words it covers (`text`), a `set`, the cast present and a `camera`. `python -m pipeline.studio info` lists the sets, anchors, set cameras and props a shot can name.

- **Cast.** `{"rig": "avatar", "bundle": 722}` builds a real R15 avatar from a catalog bundle with `HumanoidDescription` (optional `wear`, `skin`, scales). `silhouette` and `eyes` turn one into a dark figure with lit eyes. Every script inside anything loaded from the catalog is removed. The older block rig (`studio/library/characters.json`) still works.
- **Acting.** Per actor: `at` / `to` anchors, `facing`, `anim` (a track from `animations.json`, posed frame by frame in edit mode), `pose` (one name or keys over the shot, from `avatar_poses.json`), `face` (from `expressions.json`, driven through FaceControls), `look_at`, `holds` (a prop in a hand).
- **Camera.** Either on a subject, `{"on": "me", "frame": "close", "yaw": 30, "pitch": 4, "move": "push"}`, with frames `extreme|detail|close|medium|full|wide|vast`, `ots`, `pov`, `dutch` and moves `push|pull|pan_*|orbit_*|tilt_*|drift_*|handheld|creep|static`; or fixed, `{"from": "door_wide"}` or `{"from": {"pos": [...], "look": [...]}}`. Yaw 0 is in front of the subject. `face_on` keeps the judge's candidates within 70 degrees of the front, and `intent` is a line the vision judge is told the shot must show.
- **Shot types.** `"type"` on a camera tags the shot as one of `wide`, `medium`, `close`, `extreme_close`, `over_shoulder`, `low_angle`, `high_angle`, `tilted`, `pov` or `insert`. On a subject camera the tag has to agree with the camera's own numbers (frame size, pitch at or below -12 or at or above 20, a dutch of 6 degrees or more, `ots`, `pov`); a fixed camera has no numbers to read, so its tag is taken as written and an untagged one counts as an insert. Close framings (`close`, `detail`, `extreme`) are shot with shallow focus; `"focus": "shallow"` or `"deep"` overrides that.
- **Silent shots.** A shot with `"hold": seconds` instead of `text` carries no words. Silent shots go after the last word, so a video can stay on its ending: the timeline, `shots.json`, the sound bed and the edit all run to the end of the last one, and the captions stay empty over them.
- **Wording.** Signs and door plates in the sets carry neutral defaults; a top-level `set_text` object in shotlist.json (`{"door_name": "..."}`) replaces them when the sets are built with `studio build-sets --job J`.
- **Light.** `time` picks a preset from `lighting.json`; `lights` switch, flicker or pulse a set's lamps; `set_state` moves set pieces such as a door handle (a piece can ride on another, like a handle on a door that swings). Dark presets carry a `subject` light: a soft fill that rides beside the camera so a face stays readable, or, for a silhouette, a light behind the figure so its outline stands against the background.

## Judge

`pipeline.judge` picks the camera for every shot that is not fixed, then audits the finished video. Scores, reasons, the chosen camera and the spend are in `<job>/judge.json`; the render stage uses the chosen cameras automatically.

1. **Geometry, inside Studio, before any picture** (`studio/director/judge.luau`). A candidate fails when the subject's head is out of frame or hidden (raycast), the camera is inside a part or off the set, or the view is blocked. Notes that do not fail it: head, face or in-world text in the caption band (60% to 78% of the height), subject too small or large for the frame type, a repeat of the previous framing.
2. **Scout.** When fewer than three proposed cameras survive, the camera is swept around the subject (12 yaws, three pitches, two or three frame sizes) and the survivors nearest the author's camera and most different from each other become candidates. This is what rescues a character standing against a wall.
3. **Vision.** One still per surviving candidate, tiled into a labelled contact sheet with the caption band drawn in, is scored against `studio/library/rubric.json` (readable subject and action, whether the frame would hold up as a still from a film in the genre, variety against the previous shot, composition, mood, caption-band clearance, defects; variety and the film-still question weigh 0.18 each). Totals are computed here from the rubric weights. Under the threshold (6.5) the shot is restaged, up to `retries` times; the best candidate over all rounds wins.
4. **Variety, as a rule and not a score.** Every camera has a type (above), a side of the subject (front, left, right, back, or the fixed position it shoots from) and a move (`static`, `push_in`, `pull_out`, `pan`, `track`, `handheld`, `orbit`). A candidate with both the type and the side of the previous shot's camera is dropped before any picture is taken, and a tagged camera only gets candidates of its own type, so the judge chooses the side and never the kind of shot. With `"variety": {"cap": 0.2, "slack": 0.02, "min_moving": 0.5}` under `judge` in story.json, a shot list is refused before any camera is tried when a type takes more than the cap of the screen time, less than `min_moving` of it is on a moving camera, a tag contradicts its camera, or two neighbouring fixed cameras share type and position. The distribution of the chosen cameras, with any broken rule, is written to `variety` in judge.json; `python -m pipeline.judge variety --job J [--authored]` prints it without Studio.
5. **Whole video.** `judge video` scores one mid-shot frame per shot from video.mp4 against the same rubric plus production quality, next to up to four reference frames (`--references DIR`), and lists the eight weakest shots.

Settings live in story.json under `judge` and can be overridden on the command line: `provider` (`gemini`, `anthropic`, `manual`, `none` for geometry only), `model`, `max_candidates`, `retries`, `still_height`, `ceiling_usd` and `prices` (USD per million input and output tokens by model). Calls stop when the spend reaches the ceiling and the remaining shots keep their best geometric candidate. Token counts are measured; the dollar figure is only as good as the price table. A reply with no usable scores is recorded on the shot and the run continues.

## Sound

`pipeline.sound` builds `bed.wav` from `<job>/sound.json`: a `music` score (ElevenLabs music generation, instrumental, the length of the narration), `ambience` loops and one-shot `cues` (ElevenLabs sound generation), each from a text prompt and placed by seconds, shot or word. The score drops by `duck_db` under every cue and goes silent at each entry in `cuts`; a `drone` (`{"root_hz": 55, "gain_db": -5}`) is a synthesised sustained layer that runs from the first sample to the last and is never ducked or cut, so a cut drops to the drone alone; if the music endpoint refuses, the synthetic pad from `pipeline.bed` stands in and the report says so. `"normalise": true` levels every clip before its `gain_db`. Every generated sound is cached under `sfx/`, so re-runs bill nothing. The bed is as long as the picture, silent shots included, and `sound_report.json` gives its overall level and its quietest 0.4 s window. The edit stage sets the whole bed under the voice (11 LU unless `mix.music_offset_lu` in story.json or `--music-offset` says otherwise) and records its source, gain and measured levels in `edit_report.json`. `--provider synthetic` runs the stage with no key and no network.

## Rendering on the Mac

Studio does not let a script capture the viewport, so each frame is posed through the Studio MCP server and the Studio window is captured with `screencapture`; a barcode drawn in a corner proves the picture shows the frame that was asked for. Consequences: the Studio window must be on screen and uncovered, the Mac must be unlocked and awake, and nothing else may take the screen during a render. A 54 second video (1,614 frames) captures in about four minutes. Shots are kept as `render/<id>.h264` and only changed shots are rendered again (`--reuse` ignores director source changes, `--force` renders all).

## Tests

```
python -m pip install -e ".[dev]" && python -m pytest     # tests skip when FFmpeg is missing
```

Nothing in the suite needs Studio, a key or the network: the judge, sound and vision stages are tested against fakes.

## Edit stage

Renders `final.mp4` from a job folder: the picture untouched, one-word captions burned in with FFmpeg + libass, narration loudness-mastered. Python standard library only (3.9+); FFmpeg and ffprobe must be on PATH (or set `FFMPEG` / `FFPROBE`) and FFmpeg must be built with libass. The stage fails with a clear message when it is not.

```
python -m pipeline.edit --job <folder> [--out final.mp4] [--music bed.wav] [--tail 0.1]
python -m pip install -e ".[dev]" && python -m pytest     # tests skip when FFmpeg is missing
```

### Inputs (job folder)

- `video.mp4`: 1080x1920, constant frame rate, no audio, no captions.
- `narration.wav`: 48 kHz mono voice.
- `words.json` ([schema](schemas/words.schema.json)): `{"version":1,"audio":"narration.wav","duration":52.31,"words":[{"i":0,"token":"Nine","display":"9","start":0.12,"end":0.38,"sentence":0}]}`. Times are seconds on narration.wav. `display` is rendered verbatim; `pipeline.captions.display_text(token, names)` is the helper for the upstream stage that produces it.
- `shots.json` ([schema](schemas/shots.schema.json)): fps, size, `content_start`/`content_end` (times in video.mp4; outside them is handle and is trimmed so narration time 0 is `content_start`) and the shot list. Shots are checked (ordered, non-overlapping, inside the video) and summarised in the report; picture is not cut.

Both files are validated before any rendering (`pipeline/handoff.py`).

### Guarantees

- Picture is re-encoded only to burn captions: 1080x1920, same constant frame rate as the master, H.264 High about 8 Mbit/s, yuv420p, BT.709 tags, AAC 192 kbit/s 48 kHz stereo, faststart. No end card.
- Output duration is the narration length plus `--tail` seconds, rounded to whole frames. A shorter picture holds its last frame; a longer one is trimmed.
- Captions: one word at a time, hard cut, no animation. A word stays up until the next one starts, with at least 120 ms on screen (a shorter word borrows from the following gap by delaying the next word; nothing overlaps). The first word is on frame 0 and the last holds to the end. All times are snapped to the frame grid, and ASS times are floored to centiseconds so no cut lands a frame late.
- Look: Montserrat ExtraBold (SIL OFL 1.1, in `assets/fonts/`, loaded through the ass filter's `fontsdir`), white fill, 8 px black stroke, a second blurred semi-transparent black layer 6 px lower as a soft shadow, centred horizontally with the cap-height centre at 69% of the frame height. No background box.
- Audio: narration is loudness-mastered with two-pass loudnorm (measure, then apply the measured values with linear mode) to -14 LUFS integrated and -1 dBTP. With `--music` the bed is looped or trimmed to the video, set 11 LU under the measured voice, faded out over the last 0.3 s, mixed, and the mix is mastered the same way. The edit stage itself adds no ducking and no effects; a score, ambience and cues come ready-mixed in the bed from the sound stage.
- Music level is a setting: `story.json` `"mix": {"music_offset_lu": 11.0}` or `--music-offset` (the flag wins; default 11). The value used is `music.offset_below_voice_lu` in the report. The report also measures the bed against the voice under `music.levels` (RMS dBFS of the pre-master mix parts, so the differences survive mastering): the bed overall, the bed inside the gaps between sentences (`gap_seconds`, `gap_count`), the voice while words are spoken, and `bed_minus_voice_db` and `bed_gap_minus_voice_db`. The CLI prints one summary line.
- Tail: without `--tail` the output lasts the longer of the narration plus 0.1 s and the content length in shots.json, so a timeline that ends on silent shots is held; `--tail` sets narration plus that many seconds as before. The report has `tail` (seconds, source `timeline` or `flag`). With a tail of 0.6 s or more the last caption ends 0.4 s after its word and the rest of the video has none; a shorter tail holds the last word to the end. The bed covers the whole video and fades out over its last 0.3 s.
- `edit_report.json` is written beside the output: durations, fps, measured loudness and true peak (input narration, pre-master, output), caption event count, shots summary, wall-clock seconds.

### How the caption font size was derived

libass scales the ASS `Fontsize` so that the font's ascent plus descent equals the size, not the em. For Montserrat ExtraBold, libass takes the OS/2 Windows metrics (usWinAscent 1109 + usWinDescent 453 = 1562 units per 1000-unit em). An em of 100 px therefore needs a Fontsize of 100 x 1.562, which rounds to 156. The constants live in `pipeline/captions.py`. It was checked on a rendered frame: the cap height of the letter H measured 69 px (cap height is 0.70 em, so 70 px expected), which confirms a 100 px em.

