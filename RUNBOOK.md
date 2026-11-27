# Runbook: one story in, one video out

For an agent making a video on the Mac mini. Read this and CAST.md; read HANDOFF.md and POSES.md when a stop asks
you to author shots, design a set or fix a shot. O = ~/social-automation-output.

One program runs every stage in order, remembers what is done, and stops with a request file where an agent has
to decide something. You run it, do what the request says, and run it again, until it prints the final stage.

## 1. Before you start

1. The Mac has Roblox Studio open on the pipeline's place with the plugin connected. You never touch that window,
   the screen, the Dock or another app, and you never run open, osascript, screencapture or a browser: frames are
   read off the screen. Everything goes through pipeline commands, which take the Studio lock and wait their turn.
2. Run pipeline commands with the run script: it sets the tool paths (FFmpeg with libass, the aligner) and the
   working directory. Give every path as an absolute path, because the script changes directory.

       O/run.sh <module> [args]             the shared copy (branch first-video with make merged)
       O/make-work/run.sh <module> [args]   the make branch copy, until the Studio owner has merged it

3. Model keys (ELEVENLABS_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY) live in the Mac's interactive shell. Start
   the program inside one so the stages can read them: zsh -ic '<command>'. Never print a key or write one to a
   file. The program checks only that each variable is set and names the missing one.
4. Your job folder is yours alone: O/jobs/<slug>/. Never write into another job, O/repo or the cast registry.
5. Know your narrator (a cast id from CAST.md: willow, allan, lawrence, sharath, jesslyn, agni).

## 2. The command

    zsh -ic 'O/run.sh pipeline.make --story /ABS/story.json --out /ABS/O/jobs/<slug> --narrator <id>'

Run the same line again after every stop (--story and --narrator are remembered; --out alone is enough):

    zsh -ic 'O/run.sh pipeline.make --out /ABS/O/jobs/<slug>'

| you want | add |
|---|---|
| see what would run, touching nothing | --dry-run |
| see where a job stands | --status (no other work is done) |
| run one stage and everything after it again | --from STAGE |
| stop after a stage | --to STAGE |
| a different spend ceiling than $8 | --budget USD |
| no keys at all (say voice, synthetic sound, geometry-only cameras, no judges) | --offline |
| a story text or a writer record instead of a finished story.json | --writer DIR, or MAKE_WRITER set (run.sh sets it to O/SOCI-7/repo) |
| close the fix loop with fails you accept | --accept s05,s07 --reason "why"  (or --accept all) |

--story takes a story.json that already has lines (it is copied; shotlist.json, sound.json, poses.json and sets/
beside it are copied too when the job has none), or a story text or record, which the script writer turns into
story.json first. The writer is called from its own checkout with its own spend log (JOB/writer_ledger.jsonl) and
at most $0.50.

Exit code 0: finished, or reached --to. 3: stopped for an agent; the request file is printed and named. 1: a stage
failed; the message says why and JOB/logs/<stage>.log has the output.

## 3. The stages

A stage is skipped when it finished and nothing it reads has changed since, so a rerun costs only what changed.

| stage | who | what it does | writes | it is right when |
|---|---|---|---|---|
| script | code | copies the story, or calls the writer; adds the house settings for the camera judge, the mix and the quality judges when the story has none | story.json | the run line says copied or written; for a written script, script_judge.json says passed |
| cast | code | pipeline.cast: the narrator's voice at the narrating pace, a voice for every speaker, the members' avatars | cast.json, voices in story.json | cast.json names the members you expect; anyone marked auto was chosen for you: listen after the voice stage and override in story.json casting |
| voice | code | pipeline.voice: narration and word timings | narration.wav, words.json, voice_report.json | you listened to narration.wav; final_wpm in voice_report.json is near the target (215 to 235) |
| shots | AGENT | you write the sets and shotlist.json (section 5.1, 5.2); the stage then puts the members' avatars in the cast and builds the timeline | shotlist.json, timeline.json | the stage prints the number of shots and the sets |
| checks | code, AGENT on a fail | pipeline.checks: the defaults (section 6), on the shot list, before Studio | checks.json | 0 failing rule checks; read the warnings |
| sound | code | writes sound.json from the story's sound cues and music line when there is none, then pipeline.sound | sound.json, bed.wav, sound_report.json | you listened to bed.wav; sound_report.json says the score came from generation, not the fallback pad |
| camera | code, AGENT on a fail | pipeline.judge shots: a camera for every shot that is not locked (Studio and a vision key) | judge.json, judge/ | no head or in-world text under the caption (60% to 78% of the height), no variety break |
| posture | code, AGENT on a fail | pipeline.judge posture --no-vision: the geometry check in Studio | posture.json | no failing shot |
| render | code | pipeline.render video: every frame, off the Studio window | video.mp4, shots.json, render_report.json | frames_rendered plus kept shots covers the video |
| edit | code | pipeline.edit: voice, sound bed and captions | final.mp4, edit_report.json | you watched final.mp4 once |
| quality | code | pipeline.quality: eight judges per shot and one for the whole video | quality.json, QUALITY.md, quality/sheets/ | the summary line: pass, fail, incomplete |
| fix | AGENT | you decide a fix for every failing shot (section 5.3); the changed shots go round again | REQUEST-fix-N.md | no shot fails, or the rest are accepted with a reason |
| final | code | the run table | MAKE.md | final.mp4, QUALITY.md and MAKE.md exist |

State is JOB/make.json (per stage: status, inputs, outputs, seconds, spend). MAKE.md is the same as a table.

## 4. When it stops

**A request (exit 3).** Read JOB/REQUEST-<name>.md: what is needed, the findings, the inputs, the commands, and how
to tell it is done. Do it, then run the command line again. A finished request moves to JOB/requests_done/.

**A failure (exit 1).** Read the message, then JOB/logs/<stage>.log.

| message | do this |
|---|---|
| <KEY> is not set in the environment | you did not start it with zsh -ic; or pass --offline |
| the spend ceiling of $N is reached | read JOB/quality/ledger.json to see where it went; raise --budget only with the Captain's go-ahead |
| --story ... is not a story.json ... the writer's checkout is not set | pass --writer DIR or use a run script that sets MAKE_WRITER |
| story.json or shotlist.json: <a validation message> | fix that file; pipeline.story check --job JOB repeats the check |
| voice, sound: a network or quota error | run again; the raw take and every generated sound are cached, so nothing is billed twice |
| camera, posture, render: waiting for Studio | another agent holds the lock; the stage waits up to an hour. O/run.sh pipeline.lock status shows who. Never delete the lock |
| render: the Studio window is not on screen, or a calibration error | tell the Captain; only the Studio owner touches the window |
| a timeline built from an older shot list is refused | run the program again; the shots stage rebuilds the timeline |

To run a stage again on purpose use --from STAGE. --from script copies the story again over JOB/story.json, so
make story edits in the source file or avoid --from script. Spend already made is never refunded by a rerun.

## 5. What an agent does

### 5.1 Author the shots (REQUEST-shots.md)

1. Read story.json (lines and speakers), cast.json (who plays whom and under which actor id) and CAST.md.
2. Open shotlist.draft.json: the script is already cut into shots at sentence and clause ends, each with its exact
   words, its speaker and its length in seconds. The words must stay exactly as they are; you may merge or split
   shots at word boundaries.
3. List what a shot can name: O/run.sh pipeline.catalog --job JOB (sets, anchors, set cameras, moving pieces,
   lights, props, poses, faces, lighting moods). HANDOFF.md, "What a shot can say", has every field.
4. For each shot write: the set; the cast present, each with a place, a pose that is an action, a face, and
   movement where the line has action (to, a pose list, a face list, a reach); props; the light (time) when the
   story says what time it is; a camera with its "type" written out (an untagged camera lets the judge change the
   kind of shot and break the variety rule after it has been paid for). Leave the top-level cast object to the program, except for
   extras with a body.
   What the first videos learned the slow way:
   - A face name must be one the catalog lists for avatars. An unknown name (calm, happy) stops Studio in the
     middle of a preview; pipeline.checks refuses it first.
   - On a fixed camera ({"from": {"pos", "look", "fov"}}), fov is the horizontal angle of the upright frame, not the
     vertical one. A number chosen as a vertical angle gives a picture about 1.8 times wider than planned.
   - The built-in over-the-shoulder camera ("ots") gives no say over who blocks whom and can hide the lead behind
     the other character's back. Preview every ots shot, or use a fixed camera.
   - A free hand reaches belt on a standing body; thigh misses by about 0.6, and ribs collides with an arm that
     holds a prop.
   - Change what a screen shows on a cut, in the next shot. Sliding one panel over another inside a shot garbles
     the text for a few frames and fails the built-twice check.
   - Do not use the sun_patch prop: it reads as translucent paper. Daylight comes from a day lighting mood and
     bright windows in the set.
5. Save it as shotlist.json, then:

       O/run.sh pipeline.story check --job JOB        the words match the script
       O/run.sh pipeline.checks --job JOB             the defaults (section 6); fix until 0 failing

6. Optional look before the long stages (needs the timeline: run the program once first):
   O/run.sh pipeline.preview --job JOB --shots s01,s02 --check --holder <your name>, then read preview/sheet.jpg.
   Preview six to eight shots at a time: the sheet is one image and is unreadable at 58 shots, and a short hold
   lets the other agents in. If the command prints an error, the run died even when its exit code is 0.
7. Run the program again.

### 5.2 Design a set (REQUEST-set-<name>.md)

1. Copy the nearest library set into your job and rename it after the job (grocery_aisle, not store):
   cp O/repo/studio/director/sets/<nearest>.luau JOB/sets/<name>.luau
2. Build the room in that file: floor, walls, furniture; an anchor for every place someone stands, sits or reaches;
   a set camera for every fixed view; named lights; moving pieces (doors, handles). HANDOFF.md, "Adding a set of
   your own", lists every builder call and the rules: nothing may float (a monitor needs its stand, a shelf must
   reach the wall), nothing is built twice, soft parts are named Pillow, Mattress or Cushion.
3. O/run.sh pipeline.catalog --job JOB --set <name> shows the anchors and cameras you declared, without Studio.
4. Preview one shot per set camera with --check and look at the sheet: the room reads at phone size, nothing
   floats, the caption band is clear of what matters.
5. Run the program again. The set lives only in your job folder; other jobs never see it.

### 5.3 Fix failing shots (REQUEST-checks.md, REQUEST-camera.md, REQUEST-posture.md, REQUEST-fix-N.md)

The four requests are the same work at four moments: the earlier a failure is caught, the cheaper it is.

1. Read the findings in the request. For a fix request also open QUALITY.md and the sheet the judge saw
   (quality/sheets/); for a posture request open posture.json.
2. Group the shots that fail for one cause and fix the cause once (a pose that is wrong everywhere, a light that
   hides the subject in a whole set).
3. Choose per shot: restage it in shotlist.json (pose, face, place, props, light, camera); change or add a pose in
   JOB/poses.json (name new poses <slug>_<name>; POSES.md lists the verified ones and what each needs); change the
   set file; or accept it. Never change a shot's id, words or hold.
4. A judge can be wrong. A ruling that holds for the whole video (for example a style choice) goes into story.json
   quality.accepted as a sentence; a single shot is accepted with --accept <id> --reason "why". A default rule is
   passed for one shot with "allow": ["rule"] and "allow_reason": "why" on the shot.
5. Check before paying for a render: O/run.sh pipeline.checks --job JOB, then a preview of the changed shots with
   --check.
6. Run the program again. It rebuilds the timeline, re-judges the cameras and geometry of the changed shots only,
   re-renders them, edits, and judges only what changed. A change to poses.json or a set file touches every shot
   that uses it, so every camera is judged again: prefer shot-level fixes late in a job.
7. The loop ends when no shot fails, after 3 fix requests (--fix-rounds), at the spend ceiling, or when you accept
   the rest. What still fails is listed in MAKE.md and QUALITY.md; say so in your report.

## 6. The defaults, and where each is enforced

Each rule is something the first video failed on after a render. They are checked as early as they can be.

| default | where | when |
|---|---|---|
| a named set, anchor, pose, lighting mood or avatar face exists | shot-list validation: pipeline.checks rule names | before Studio |
| a cast member's entry is the registry's, unchanged | pipeline.checks rule cast | before Studio |
| nobody stands idle: every visible character has an action | pipeline.checks rule no_idle; again by geometry (idle) and by the posture judge | before Studio, in Studio, on the picture |
| every visible avatar states its face in every shot | pipeline.checks rule face | before Studio |
| fear on a scare line, never a smile | pipeline.checks rule fear_face ("scare": true or false on a shot overrides the word list); again by the expression judge | before Studio, on the picture |
| a face is set only on a head that can move | geometry: frozen_face | in Studio |
| a line with action has movement; nobody is frozen for 2 s | pipeline.checks rule motion; again by the motion judge | before Studio, on the picture |
| no pose in more than a quarter of a character's shots, or more than 2 running | pipeline.checks rule pose_variety | before Studio |
| no camera type over a fifth of the time, half the time on a moving camera, neighbours differ | pipeline.checks rule camera_variety, and the camera judge | before Studio, in Studio |
| every camera the judge may replace names its type, so the judge chooses the side and never the kind of shot | pipeline.checks rule camera_tag | before Studio |
| the light agrees with the time of day the script has reached | pipeline.checks rule time_of_day ("daypart" on a shot overrides); again by the story judge | before Studio, on the picture |
| no sun_patch prop | pipeline.checks rule sun_patch | before Studio |
| a hand is sent where it can reach (not thigh when standing, not ribs beside a held prop) | pipeline.checks rule reach (a warning); then geometry: reach_miss, intersect | before Studio, in Studio |
| a pose new in this job has passed geometry | pipeline.checks rule new_pose (a warning), then the posture stage | before Studio, in Studio |
| nothing floats, nothing is built twice, nothing passes through anything, hands reach their targets, props are in the hand | geometry: pipeline.judge posture, preview --check | in Studio |
| no head or in-world text under the caption | the camera judge's geometry drops such candidates; when none is clean pipeline.make stops; again by the framing judge. A locked or fixed camera is not measured before the render: preview it and look at the band on the sheet | in Studio, on the picture |
| the action reads, the face fits, the place and time show, the avatars are consistent | the judges' rubric: pipeline/quality_rubric.json | on the picture |

Tune the first group per job in story.json: "checks": {"pose_share": 0.25, "pose_run": 2, "still_seconds": 2.0,
"off": ["rule"]}. Turning a rule off is a decision to report, not a fix.

## 7. Cast

CAST.md is the page for this. In short: the script names pool characters by id, the narrator is "narrator", and
--narrator says who narrates. The cast stage writes the voices and avatars; do not hand-edit a member's cast
entry. One video's changes (a costume, a role played by a member, an extra) go in story.json "casting".

## 8. Time and spend

Measured on the first video (the-door, 54 shots, 58.8 s, made by hand over several fix rounds):

| stage | time | spend |
|---|---|---|
| voice (ElevenLabs, 216 words) | 20 s | 558 characters billed after caching; 1203 for a first take |
| sound (12 generated sounds and a score, all cached on reruns) | 3 s cached | credits not recorded by the stage |
| camera (anthropic claude-haiku-5-5, 136 calls) | not recorded | $1.02 |
| render | 5.6 frames a second: about 5 minutes for all 1765 frames; 83 s for the 462 frames of a partial rerender | none |
| edit | 20 s | none |
| quality (gemini-3.8-flash) | 266 s for 17 shots | $0.61 for 17 shots, so about $2 for a first pass of 54; $6.96 over all 26 runs of the fix rounds |
| waiting for Studio | 18 s to 175 s per command when others were working | none |

Measured on the sample (examples/found-box, 10 shots, 10.1 s, say voice, old blocky figures) on 27 Nov through
pipeline.make, while other agents were using Studio; the camera times include waiting for the lock:

| stage | first pass | second pass, 2 shots restaged | spend |
|---|---|---|---|
| script, cast, shots, checks | under 0.1 s each | under 0.1 s each | none |
| voice (say) | 7.0 s | skipped | none |
| sound (one 10.5 s score generated) | 4.3 s | skipped | not reported by the sound stage |
| camera (anthropic claude-haiku-5-5) | 115 s for 10 shots, then 139 s for 7 retagged shots | 347 s for 2 shots, nearly all of it waiting for Studio | $0.057, $0.052, $0.009 |
| posture (geometry only) | 5.6 s | 5.0 s | none |
| render | 60 s for 304 frames (5.5 frames a second) | 18 s for 63 frames, 8 shots kept | none |
| edit | 4.0 s | 4.0 s | none |
| quality (gemini-3.8-flash) | 246 s, 104 calls, 10 shots | 99 s, 21 calls, the changed shots | $0.465, $0.122 |

In all: 1054 s of machine time and $0.705, with four stops for an agent (the defaults, the camera variety, two fix
requests). Per shot that is about $0.006 for a camera and $0.047 for a first quality pass.

The ceiling is $8 a video: one ledger, JOB/quality/ledger.json, counts the writer, voice, sound, camera and quality
together, and no paid stage starts once it is reached. Voice and sound are billed in ElevenLabs credits; their
dollars are an estimate at $0.30 per 1000 credits (story.json make.prices). Camera, writer and quality are measured
from tokens. These numbers will be replaced by the five new videos' as they finish.

## 9. What is still done by hand

- Authoring the shot list and any new set (5.1, 5.2), and deciding every fix (5.3).
- Listening: the narration, the voices chosen automatically for extras, the sound bed. No stage hears them.
- Watching final.mp4 once before reporting.
- Anything about the Studio window: only the Studio owner.
