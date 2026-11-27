# social-automation
Pipeline for narrated short-story videos staged in Roblox Studio. Setup and usage instructions arrive with the first pull request.

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
- Audio: narration is loudness-mastered with two-pass loudnorm (measure, then apply the measured values with linear mode) to -14 LUFS integrated and -1 dBTP. With `--music` the bed is looped or trimmed to the video, set 11 LU under the measured voice, faded out over the last 0.3 s, mixed, and the mix is mastered the same way. No ducking, no sound effects.
- `edit_report.json` is written beside the output: durations, fps, measured loudness and true peak (input narration, pre-master, output), caption event count, shots summary, wall-clock seconds.

### How the caption font size was derived

libass scales the ASS `Fontsize` so that the font's ascent plus descent equals the size, not the em. For Montserrat ExtraBold, libass takes the OS/2 Windows metrics (usWinAscent 1109 + usWinDescent 453 = 1562 units per 1000-unit em). An em of 100 px therefore needs a Fontsize of 100 x 1.562, which rounds to 156. The constants live in `pipeline/captions.py`. It was checked on a rendered frame: the cap height of the letter H measured 69 px (cap height is 0.70 em, so 70 px expected), which confirms a 100 px em.

