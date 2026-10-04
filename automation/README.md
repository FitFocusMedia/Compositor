# Compositor automation

Tools for building, filling, checking and rendering Compositor projects (`.comp`) without touching the app, so
templates designed by hand in Compositor can be turned into finished images by scripts, batch jobs and AI agents.

| Piece | What it is |
| --- | --- |
| `bin/comp-render` | The app's own project loader, typesetter and renderer as a command-line tool. Output matches File › Export exactly. Built from `../Compositor` by `comp-render/build.sh`. |
| `compkit/` | Python library for writing `.comp` packages: image, fill, gradient, text, adjustment and folder layers, masks, effects, clipping, guides; safe live writes; template fill. |
| `compkit` (`python -m compkit`) | Command line: `info`, `fill`, `batch` (CSV → renders), `crop` (subject-aware crops to exact sizes), `analyze` (what the crop sees), `grade` (Lightroom preset as a tunable base grade), `sheet` (contact sheet), `where`. |
| `skills/` | Claude Code skills: `compositor-design`, `compositor-batch`, `compositor-photo`, `compositor-grade`, `compositor-toolkit`. |
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
```

## Lightroom presets

`comp-render develop` reads a Lightroom / Camera Raw `.xmp` and applies every setting with a counterpart in the
app's Camera Raw engine: light, presence, curves (parametric and point), color mixer, color grading, detail,
effects, optics, geometry and calibration. White balance converts from Kelvin: RAW files go from their own
as-shot reading to the preset's Kelvin, and other photos get the shift the preset made on its own photo.
Profiles, LUTs, lens profiles and brush/AI masks have no counterpart and are reported. Radial and linear local
corrections become masked Exposure layers. The arithmetic is Compositor's, so a grade is close to Lightroom's
rather than identical.

```sh
compkit grade shoot/*.CR3 --preset ~/Documents/Presets/"God Tones.xmp" --out graded/ --render
compkit grade post.comp --layer Photo --preset base.xmp            # a project's photo, graded in place
compkit batch template.comp rows.csv --out renders/ --preset base.xmp
```

Each graded photo is a folder holding the untouched original (hidden), the base grade, the preset's local
corrections and neutral `Tune ·` layers (Exposure, Curves, Hue/Saturation, Color Balance), so every image can be
tuned afterwards in Compositor or from code (`layer.regrade(preset, amount=…)` grades again from the original).

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
