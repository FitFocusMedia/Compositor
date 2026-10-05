# Compositor automation

Tools for building, filling, checking and rendering Compositor projects (`.comp`) without touching the app, so
templates designed by hand in Compositor can be turned into finished images by scripts, batch jobs and AI agents.

| Piece | What it is |
| --- | --- |
| `bin/comp-render` | The app's own project loader, typesetter and renderer as a command-line tool. Output matches File › Export exactly. Built from `../Compositor` by `comp-render/build.sh`. |
| `compkit/` | Python library for writing `.comp` packages: image, fill, gradient, text, adjustment and folder layers, masks, effects, clipping, guides; safe live writes; template fill. |
| `compkit` (`python -m compkit`) | Command line: `show` (a whole show: cull, setup, grade, export, compare), `cull` (thousands of frames → picks), `pick` (re-dial a cull), `grade` (Lightroom preset as a tunable base grade, resumable), `learn-look` (match your Lightroom exports), `info`, `fill`, `batch` (CSV → renders), `crop` (subject-aware crops), `analyze`, `measure`, `sheet`, `where`. |
| `skills/` | Claude Code skills: `compositor-design`, `-batch`, `-photo`, `-grade`, `-cull`, `-toolkit`. |
| `install.sh` | Builds comp-render, sets up `.venv`, puts `comp-render`, `compkit` and `compkit-python` in `~/.local/bin`, and links the skills into `~/.claude/skills`. |
| `examples/` | `build_post_template.py` builds a 1080×1350 post template; `batch/rows.csv` fills it. |
| `tests/` | `.venv/bin/python -m unittest discover -s automation/tests` — every case is checked with the app's own loader and renderer. |

## Setup

```sh
automation/install.sh            # safe to rerun; rebuilds comp-render only when the app's sources changed
```

The build (~2 min) needs only the Swift toolchain (Command Line Tools or Xcode). It compiles every app source
except the SwiftUI entry point, with Sparkle stubbed out, so it tracks the app as it changes. The skills are
linked, not copied, so editing them here takes effect at once.

## comp-render

```sh
comp-render render post.comp post.jpg --quality 0.92 --max-side 1080   # or .png (keeps transparency)
comp-render validate post.comp      # exit 0 if the app would open it; otherwise the app's own reason
comp-render info post.comp          # manifest as JSON, after validating
comp-render text style.json out.png # a text layer's pixels, set exactly as the Type tool sets them
comp-render defaults                # the app's default adjustment / effect / text records and blend modes
comp-render cutout shot.heic cut.png         # Remove Background: transparent PNG (Apple Vision + edge refine)
comp-render cutout shot.heic mask.png --mask # …or just the subject mask, for a layer mask
comp-render subject shot.heic       # Vision's faces, people, subject and salient boxes (0–1, from the top-left)
comp-render fonts futura            # installed fonts' PostScript names (what a text style takes)
comp-render preset base.xmp         # how a Lightroom preset maps onto Compositor's Camera Raw (and what doesn't)
comp-render develop shot.CR3 base.xmp graded.png --original orig.png [--amount 0.8] [--set exposure=0.2]
comp-render render post.comp post.jpg --max-bytes 2048000 --metadata-from shot.ARW   # size-capped, camera metadata
comp-render export job.json         # a graded project to delivery JPEGs, re-developed at full resolution
```

## Shows: from thousands of RAWs to delivered images

A show is a folder that the steps fill in turn. `SHOW-RUNBOOK.md` walks through a real show in order, with times
and the points where you review and approve.

```sh
compkit show new SHOW --card /path/to/card-copy --profile competition --name "Spring Classic"
compkit show cull SHOW                       # picks by the profile's keep rate; contact sheets in SHOW/cull/sheets
compkit show pick SHOW --rate 35%            # re-dial from the same scores, in seconds (or --count 1000)
compkit show setup SHOW                      # white balance / exposure candidates on one sheet …
compkit show setup SHOW --approve B          # … approved for the show (or 1=B,2=C,… per lighting)
compkit show grade SHOW                      # tunable projects + 2048 px JPEGs, with the approved settings
compkit show export SHOW                     # the delivery spec: sizes, names, folders, file-size cap
compkit show compare SHOW --delivered EXPORTS [--learn]   # against what you delivered; --learn updates the profile
compkit show status SHOW
```

**A hands-off show day:**

| Command | What it does |
| --- | --- |
| `compkit show start --drive /Volumes/X --name "…" --profile …` | Sets up the day's show on the show drive. |
| `compkit show watch --install` | Installs two launchd agents: the card watcher and an always-on dashboard. |

From then on every inserted card is copied, verified and run through every step on its own. Picks already made are
never changed by later cards. The dashboard (`compkit dashboard`, http://localhost:8765, served from this Mac
only) shows progress and every frame by moment. It also takes changes, which the runner then grades and delivers:
- picks, alternates and rejects, set with the keyboard;
- re-dials;
- crops and leveling;
- setup approval.

Leveling happens at the RAW stage, before the 8-bit grade. Automatic crop suggestions (`compkit/framing.py`) exist
but are off by default (`compkit show profiles --set NAME auto_crop=true`). On a real shoot they didn't match the
photographer's own crops.

- **Resumable:** every step can be stopped (Ctrl-C, a crash) and run again. Scores are cached, graded projects
  whose settings haven't changed are kept, and exports newer than their project are skipped.
- **Failures:** a file that fails goes to `SHOW/logs/failures.csv` and the step carries on. Each step logs its
  progress to `SHOW/logs/<step>.log`.
- **Read-only cards:** the card copy is only ever read.

**Culling** reads each frame's embedded preview (RAWs aren't decoded):
- `comp-render probe` reads metadata, and `comp-render score` measures on every core: Apple's aesthetic score,
  face capture quality and eye openness, sharpness of the subject in focus, a peak-sharpness check for missed
  focus, exposure, and the Vision feature print.
- Frames group into moments: consecutive, within 2 s, alike.
- Clear failures are rejected: nothing in focus, eyes shut, far off exposure.
- Picks follow a keep rate or count. Each moment gets frames in proportion to its size, so a long burst or a held
  pose gets more than a single shot. Every moment's first frame comes before any moment's second.
- It writes `picks.txt`, `cull.csv`/`cull.json`, contact sheets and, with `--ratings` only, Lightroom star ratings
  as sidecars.

`compkit cull FOLDER --out cull/ [--profile …|--rate 40%|--count N|--per-moment K]` and `compkit pick cull/ --rate
35%` do the same without a show folder.

**Profiles** (`compkit show profiles`) are `workshop`, `competition` and `fight-night`. Each holds a keep rate,
how frames group into moments, the per-moment rule (tau: how much a fast-burst frame counts, cover: the first frame
of each moment first, beta: how far a better score moves a frame ahead), and the setup settings of the last show
learned from. What they learn, your default preset and look, and your delivery spec live in `~/Documents/Presets/Show
Profiles/` (`COMPKIT_PROFILES` overrides that), outside this repository. `compkit show defaults` sets the preset,
look, naming, folders and file-size cap.

Calibrated against a photographer's own 170 delivered frames from a 429-frame posed shoot:

| | One pick per moment | Calibrated rule |
| --- | --- | --- |
| Picks | 165 | 170 |
| Moments covered | 124/125 | 123/125 |
| Keepers wrongly rejected | 1 | 1 |
| Keepers picked | 71 | 76 |
| Picks per moment, mean error | 0.52 | 0.44 |

On held-out halves of the shoot the gain was smaller but held. Which frame within a moment it picks is no better
than chance, so the alternates sit beside every pick.

**Export** grades each full-resolution file again from its RAW: `comp-render export` re-develops the photo at
61 MP and renders the whole project at that size (local corrections and Tune layers included). Each file is
written as the highest JPEG quality under the size cap, as Lightroom's "Limit File Size To" does, with the camera's
metadata. Smaller sizes come from the 2048 px project. On 1,202 picks from a 3,003-frame stand-in show (M2 Max):

| Step | Time |
| --- | --- |
| Cull | about 1 minute |
| Re-dial | 3 s |
| Grade | 14 minutes |
| Web 2048 export | 3.4 minutes |
| Full-resolution export, 3 jobs | about 115 minutes (5.7 s each, about 6 GB of memory per job) |

Files average 2.0 MB at full resolution and 0.8 MB at 2048 px.

The grading engine's pointwise stages run in bands on every core. Their output is byte-identical to the app's
one-core grade (`comp-render develop --serial`), which takes a 61 MP grade from 31 s to 15 s.

## Lightroom presets

`comp-render develop` reads a Lightroom / Camera Raw `.xmp` and applies every setting with a counterpart in the
app's Camera Raw engine: light, presence, curves (parametric and point), color mixer, color grading, detail,
effects, optics, geometry and calibration.

For RAW files, exposure, white balance (the exact Kelvin/tint), highlights, whites and lens corrections are
applied while decoding, in floating point on the full sensor data, before the picture becomes 8-bit. Other photos
get white balance as the shift the preset made on its own photo.

Profiles, LUTs and brush/AI masks have no counterpart and are reported. Radial and linear local corrections
become masked Exposure layers.

Adobe's camera profiles and math aren't available, so `compkit learn-look EXPORTS RAWS -o look.cube` learns the
remaining difference from your own Lightroom exports (which embed their develop settings) as a 3D color table;
`--match look.cube` applies it. On a 429-frame a7R V shoot, frames held out of the learning came within coarse
ΔE 2.7 of the Lightroom exports, against 5.1 for the previous 8-bit pipeline.

```sh
compkit grade shoot/*.CR3 --preset ~/Documents/Presets/"God Tones.xmp" --out graded/ --render
compkit grade post.comp --layer Photo --preset base.xmp            # a project's photo, graded in place
compkit batch template.comp rows.csv --out renders/ --preset base.xmp
```

Each graded photo is a folder holding the untouched original (hidden), the base grade, the preset's local
corrections and neutral `Tune ·` layers (Exposure, Curves, Hue/Saturation, Color Balance), so every image can be
tuned afterwards in Compositor or from code. `layer.regrade(amount=…, settings=…)` grades again from the source
RAW, recorded with its crop and grade in `<name>.sources.json` beside the project.

## compkit

```python
from compkit import Project        # run with compkit-python

p = Project.new(1080, 1350)
p.add_image("shot.heic", name="Photo")   # fills the canvas, cropped around faces and the subject
p.add_adjustment("Curves", name="Grade", curves={"rgb": [(0, 10), (128, 132), (255, 245)]})
p.add_gradient([(0, "#000", 0), (1, "#000", 0.8)], name="Fade", angle=270)
title = p.add_group("Title")
p.add_text("New PB", name="Headline", x=80, y=1200, font="Helvetica-Bold", size=110, color="#FFF", parent=title)
p.layer("Headline").set_effect("shadow", opacity=0.4, blur=16)
p.save("post.comp")          # validated with the app's loader
p.render("post.jpg")
```

- `Project.open(path)` edits an existing project. `layer(name)` finds by name (topmost wins).
- `layer.replace_image(path, fit="cover"|"contain")` drops a new picture into a placeholder's box, keeping its
  placement, mask, effects, opacity and blend mode.
- Cropping is subject-aware by default (`focus="auto"`): Apple Vision finds faces, people, the foreground subject
  and salient regions; the subject is centered when it fits, otherwise faces stay whole (in the top third when
  height is cut) or, without faces, the salient part (heads and eyes, animals included) is kept. `focus=(x, y)`
  centers the crop on that point of the picture (0–1 across and down). `analyze()`, `auto_focus()` and
  `cover_crop()` expose the steps. Pictures enlarged more than 1.25× to fill a box are reported.
- `layer.set_text("…", size=…, color=…)` re-sets a text layer with the app's typesetter, keeping its top-left
  corner and scale; `shrink_to_fit=True` steps the size down until the words fit their box (or, for point text,
  the canvas with matching margins). Text stays editable in Compositor (double-click it).
- `layer.mask_subject()` hides everything but the subject (the app's Remove Background, as an editable layer
  mask); `subject_mask(path, cutout=True)` returns a transparent cutout instead.
- `layer.set_mask(array_or_image)`, `layer.clip_to(base)`, `layer.set_effect("stroke", size=4, color="#FFF")`,
  `layer.set_adjustment(...)`, `project.move(...)`, `project.remove(...)`, `project.add_guide(...)`.
- Pictures go in as sRGB: ICC profiles (Display P3 from iPhones, Adobe RGB) are converted, EXIF rotation is
  applied, and HEIC / RAW / PSD / SVG are read through macOS `sips`.
- Text: `font` is a PostScript name; `tracking` is extra pixels between letters (not Photoshop's 1/1000 em);
  `leading` is baseline-to-baseline pixels (0 = auto). Point text anchors at its first baseline like a Type-tool
  click; `box=(w, h)` makes wrapping paragraph text whose own area is `x, y, w, h` (`h=None`: as tall as the
  text), and text that doesn't fit is reported. `text_area`, `ink_box()`, `text_metrics()` and `place_text()`
  measure and position text by the letters rather than the layer's padded box.
- `save()` over the path the project came from writes images first and swaps `manifest.json` in atomically, so a
  project open in Compositor updates live; saving elsewhere builds the package aside and moves it into place.

## Templates and batches

Design a template in Compositor and give each replaceable layer a stable name (`Photo`, `Headline`, `Logo`…).

```sh
compkit info template.comp
compkit fill template.comp out.comp --image Photo=shot.jpg --text Headline="New PB" --render out.jpg
compkit batch template.comp rows.csv --out renders/ --max-side 1080 [--keep-comp]
compkit crop shots/*.jpg --box 1080x1350 --box 1080x1920 --out crops/      # no template: just the photos
compkit crop wide.jpg --box 1080x1920 --pad blur --out crops/              # whole photo over a blurred fill
compkit analyze shot.jpg --box 1080x1350 --preview check.jpg               # why a crop went where it did
```

CSV columns: `name` (output file name), `text:LAYER`, `image:LAYER` (relative paths resolve beside the CSV),
`focus:LAYER` (optional `"x,y"`: the point to center the crop on, overriding the automatic one), `hide:LAYER`,
`show:LAYER`. Filled text shrinks to fit; `compkit info` flags template text that's already cut off. Renders
come out at the canvas size (`--max-side` only scales down, for previews). `--keep-comp` keeps each filled
project for touch-ups in the app. A failing row is reported and the rest carry on.

## Format rules worth knowing

See `../docs/writing-comp-files.md` and `../docs/project-format.md`. compkit enforces these, but when writing by
hand: IDs are uppercase UUIDs and name their files (`<ID>.png`, `<ID>.mask.png`); images are 8-bit PNG (RGBA
layers, grayscale masks); blend mode names are exact; every non-optional field of a record must be present (Swift
decoding ignores the defaults in the source); and the app refuses a broken file silently, so run
`comp-render validate`.
