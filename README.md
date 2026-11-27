# social-automation

Pipeline for narrated short-story videos staged in Roblox Studio: a story and a shot list go in, a 1080x1920 captioned video comes out. Python standard library only (3.9+); FFmpeg is the only external tool. Rendering needs a Mac with Roblox Studio open; everything else runs anywhere.

## Stages

### Find a story for a private demo

Run these from the checkout, or install the package first. The source commands use Python 3.9+ and SQLite with JSON functions. They never post to Reddit or send story text to a model. The database defaults to `~/social-automation-output/SOCI-6/reddit.sqlite`; to change it, put `--db PATH` before the command.

```sh
python3 -m pipeline.source fetch --input ~/social-automation-output/reddit/posts.json
python3 -m pipeline.source fetch --input ~/social-automation-output/SOCI-6/no_sleep_posts.csv --subreddits nosleep
python3 -m pipeline.source coverage
python3 -m pipeline.source search ghost --subreddit shortstories --candidates --min-words 250 --max-words 1200
python3 -m pipeline.source show POST_ID
python3 -m pipeline.source script POST_ID
python3 -m pipeline.source index
```

`search` matches title, author and body, sorted by source score (unknown last). The candidate filter flags series, meta posts, mature content, missing maturity flags, and stories outside 100–3,000 words. Add `--include-unknown` to consider stories whose source omitted the maturity flag; this is not a suitability check. The index next to the database contains metadata and original links, not story bodies. The default is HTML for up to 10,000 rows and CSV above that; pass `index FILE.csv` or `index FILE.html` to choose.

`script` writes `drafts/POST_ID/story.json` next to the database. It takes passages in order from the opening, action, turn and ending. This is an extractive first draft, not an original retelling: read through the cuts before recording. It needs at least 200 spoken source words and never pads a short post. Keep the database, index and drafts private; attribution is not permission to publish.

On the Mac, refresh the archive exports together: `python3 -m pipeline.source refresh --input ~/social-automation-output/reddit/posts*.json`. The importer also accepts the published [610-row](https://huggingface.co/datasets/chloeliu/reddit_nosleep_posts) and [99k-row](https://huggingface.co/datasets/ElcaidaLabs/NosleepReddit100K) NoSleep CSV snapshots. The larger CSV supplies only title, body and permalink: missing authors, creation dates and scores stay null. The retrieval field for a cache is its snapshot file timestamp, not proof of when Reddit last updated it.

`refresh --input PATH [PATH ...]` reimports a changed snapshot and regenerates the index. It is safe to schedule; this project does not install a timer. The historical shortstories JSON omits its subreddit; unlabeled JSON rows are assumed to be from r/shortstories. For an approved live feed, use `fetch --route archive --approved` for the Arctic Shift backfill, or `refresh --route reddit --approved` for new posts and current scores by ID. Add subs with `--subreddits shortstories nosleep scarystories shortscarystories`, and bound each run with `--pages N`. Progress commits after each page. Snapshot scores are historical, not current; upstream archives and Reddit's new-post listing have coverage limits.

The live OAuth route requires an approved app, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and a truthful identifying `REDDIT_USER_AGENT`. The client paces requests at one per second and honors rate-limit headers. Reddit [requires explicit approval](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy); eligible free OAuth clients get [100 queries/minute averaged over 10 minutes](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki). A key alone does not establish approval or rights in a story. Neither live feed runs by default. For a separate public-domain source, see [the optional Gutenberg collector](pipeline/source/GUTENBERG.md).

Every stage reads and writes one job folder and can be re-run on its own.

| Stage | Command | Reads | Writes |
|---|---|---|---|
| Story | `python -m pipeline.story check|timeline --job J` | story.json, shotlist.json, words.json | timeline.json |
| Voice | `python -m pipeline.voice --job J [--reuse-raw]` | story.json | narration.wav, words.json, voice_report.json |
| Studio | `python -m pipeline.studio prepare|build-sets|load|info` | timeline.json | sets and cast in the open place |
| Judge | `python -m pipeline.judge shots|video|show|models --job J` | timeline.json, Studio, video.mp4 | judge.json, judge/ |
| Sound | `python -m pipeline.sound --job J [--dry-run]` | sound.json, timeline.json, words.json | bed.wav, sound_report.json, sfx/ |
| Render | `python -m pipeline.render sheet|video --job J` | timeline.json, judge.json, Studio | video.mp4, shots.json, render_report.json |
| Edit | `python -m pipeline.edit --job J --music J/bed.wav` | video.mp4, narration.wav, words.json, shots.json | final.mp4, edit_report.json |

Order for a new video: write story.json and shotlist.json, voice, `story timeline`, `studio build-sets`, `judge shots`, sound, `render video`, edit, `judge video`. `examples/found-box` holds a sample story and shot list. Stories, adapted scripts and renders are job data and never belong in this repository.

API keys are read from the environment at call time and never written anywhere: `ELEVENLABS_API_KEY` (voice, sound), `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` (judge).

## Shot list

`shotlist.json` has a `cast` and a list of `shots`; each shot carries the words it covers (`text`), a `set`, the cast present and a `camera`. `python -m pipeline.studio info` lists the sets, anchors, set cameras and props a shot can name.

- **Cast.** `{"rig": "avatar", "bundle": 722}` builds a real R15 avatar from a catalog bundle with `HumanoidDescription` (optional `wear`, `skin`, scales). `silhouette` and `eyes` turn one into a dark figure with lit eyes. Every script inside anything loaded from the catalog is removed. The older block rig (`studio/library/characters.json`) still works.
- **Acting.** Per actor: `at` / `to` anchors, `facing`, `anim` (a track from `animations.json`, posed frame by frame in edit mode), `pose` (one name or keys over the shot, from `avatar_poses.json`), `face` (from `expressions.json`, driven through FaceControls), `look_at`, `holds` (a prop in a hand).
- **Camera.** Either on a subject, `{"on": "me", "frame": "close", "yaw": 30, "pitch": 4, "move": "push"}`, with frames `extreme|detail|close|medium|full|wide|vast`, `ots`, `pov`, `dutch` and moves `push|pull|pan_*|orbit_*|tilt_*|drift_*|handheld|creep|static`; or fixed, `{"from": "door_wide"}` or `{"from": {"pos": [...], "look": [...]}}`. Yaw 0 is in front of the subject. `face_on` keeps the judge's candidates within 70 degrees of the front, and `intent` is a line the vision judge is told the shot must show.
- **Wording.** Signs and door plates in the sets carry neutral defaults; a top-level `set_text` object in shotlist.json (`{"door_name": "..."}`) replaces them when the sets are built with `studio build-sets --job J`.
- **Light.** `time` picks a preset from `lighting.json`; `lights` switch, flicker or pulse a set's lamps; `set_state` moves set pieces such as a door handle. Dark presets carry a `subject` light: a soft fill that rides beside the camera so a face stays readable, or, for a silhouette, a light behind the figure so its outline stands against the background.

## Judge

`pipeline.judge` picks the camera for every shot that is not fixed, then audits the finished video. Scores, reasons, the chosen camera and the spend are in `<job>/judge.json`; the render stage uses the chosen cameras automatically.

1. **Geometry, inside Studio, before any picture** (`studio/director/judge.luau`). A candidate fails when the subject's head is out of frame or hidden (raycast), the camera is inside a part or off the set, or the view is blocked. Notes that do not fail it: head, face or in-world text in the caption band (60% to 78% of the height), subject too small or large for the frame type, a repeat of the previous framing.
2. **Scout.** When fewer than three proposed cameras survive, the camera is swept around the subject (12 yaws, three pitches, two or three frame sizes) and the survivors nearest the author's camera and most different from each other become candidates. This is what rescues a character standing against a wall.
3. **Vision.** One still per surviving candidate, tiled into a labelled contact sheet with the caption band drawn in, is scored against `studio/library/rubric.json` (readable subject and action, composition, mood, variety against the previous shot, caption-band clearance, defects). Totals are computed here from the rubric weights. Under the threshold (6.5) the shot is restaged, up to `retries` times; the best candidate over all rounds wins.
4. **Whole video.** `judge video` scores one mid-shot frame per shot from video.mp4 against the same rubric plus production quality, next to up to four reference frames (`--references DIR`), and lists the eight weakest shots.

Settings live in story.json under `judge` and can be overridden on the command line: `provider` (`gemini`, `anthropic`, `manual`, `none` for geometry only), `model`, `max_candidates`, `retries`, `still_height`, `ceiling_usd` and `prices` (USD per million input and output tokens by model). Calls stop when the spend reaches the ceiling and the remaining shots keep their best geometric candidate. Token counts are measured; the dollar figure is only as good as the price table. A reply with no usable scores is recorded on the shot and the run continues.

## Sound

`pipeline.sound` builds `bed.wav` from `<job>/sound.json`: a `music` score (ElevenLabs music generation, instrumental, the length of the narration), `ambience` loops and one-shot `cues` (ElevenLabs sound generation), each from a text prompt and placed by seconds, shot or word. The score drops by `duck_db` under every cue and goes silent at each entry in `cuts`; if the music endpoint refuses, the synthetic pad from `pipeline.bed` stands in and the report says so. `"normalise": true` levels every clip before its `gain_db`. Every generated sound is cached under `sfx/`, so re-runs bill nothing. The edit stage sets the whole bed 11 LU under the voice and records its source and gain in `edit_report.json`. `--provider synthetic` runs the stage with no key and no network.

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
- `edit_report.json` is written beside the output: durations, fps, measured loudness and true peak (input narration, pre-master, output), caption event count, shots summary, wall-clock seconds.

### How the caption font size was derived

libass scales the ASS `Fontsize` so that the font's ascent plus descent equals the size, not the em. For Montserrat ExtraBold, libass takes the OS/2 Windows metrics (usWinAscent 1109 + usWinDescent 453 = 1562 units per 1000-unit em). An em of 100 px therefore needs a Fontsize of 100 x 1.562, which rounds to 156. The constants live in `pipeline/captions.py`. It was checked on a rendered frame: the cap height of the letter H measured 69 px (cap height is 0.70 em, so 70 px expected), which confirms a 100 px em.
