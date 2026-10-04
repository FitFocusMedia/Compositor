# Compositor automation

Tools for building, filling, checking and rendering Compositor projects (`.comp`) without touching the app, so
templates designed by hand in Compositor can be turned into finished images by scripts, batch jobs and AI agents.

| Piece | What it is |
| --- | --- |
| `bin/comp-render` | The app's own project loader, typesetter and renderer as a command-line tool. Output matches File › Export exactly. Built from `../Compositor` by `comp-render/build.sh`. |
| `compkit/` | Python library for writing `.comp` packages: image, fill, gradient, text, adjustment and folder layers, masks, effects, clipping, guides; safe live writes; template fill. |
| `python -m compkit` | Command line: `info`, `fill`, `batch` (CSV → renders). |
| `examples/` | `build_post_template.py` builds a 1080×1350 post template; `batch/rows.csv` fills it. |
| `tests/` | `.venv/bin/python -m unittest discover -s automation/tests` — every case is checked with the app's own loader and renderer. |

## Setup

```sh
automation/comp-render/build.sh                     # ~2 min; rebuild after pulling app changes
python3 -m venv automation/.venv && automation/.venv/bin/pip install pillow numpy
```

The build needs only the Swift toolchain (Command Line Tools or Xcode). It compiles every app source except the
SwiftUI entry point, with Sparkle stubbed out, so it tracks the app as it changes.

## comp-render

```sh
comp-render render post.comp post.jpg --quality 0.92 --max-side 1080   # or .png (keeps transparency)
comp-render validate post.comp      # exit 0 if the app would open it; otherwise the app's own reason
comp-render info post.comp          # manifest as JSON, after validating
comp-render text style.json out.png # a text layer's pixels, set exactly as the Type tool sets them
comp-render defaults                # the app's default adjustment / effect / text records and blend modes
comp-render cutout shot.heic cut.png         # Remove Background: transparent PNG (Apple Vision + edge refine)
comp-render cutout shot.heic mask.png --mask # …or just the subject mask, for a layer mask
```

## compkit

```python
import sys; sys.path.insert(0, "automation")
from compkit import Project

p = Project.new(1080, 1350)
p.add_image("shot.heic", name="Photo", fit="cover", focus=(0.5, 0.35))
p.add_adjustment("Curves", name="Grade", curves={"rgb": [(0, 10), (128, 132), (255, 245)]})
p.add_gradient([(0, "#000", 0), (1, "#000", 0.8)], name="Fade", angle=270)
title = p.add_group("Title")
p.add_text("New PB", name="Headline", x=80, y=1200, font="Helvetica-Bold", size=110, color="#FFF", parent=title)
p.layer("Headline").set_effect("shadow", opacity=0.4, blur=16)
p.save("post.comp")          # validated with the app's loader
p.render("post.jpg")
```

- `Project.open(path)` edits an existing project. `layer(name)` finds by name (topmost wins).
- `layer.replace_image(path, fit="cover"|"contain", focus=(x, y))` drops a new picture into a placeholder's box,
  keeping its placement, mask, effects, opacity and blend mode.
- `layer.set_text("…", size=…, color=…)` re-sets a text layer with the app's typesetter, keeping its top-left
  corner and scale. Text stays editable in Compositor (double-click it).
- `layer.mask_subject()` hides everything but the subject (the app's Remove Background, as an editable layer
  mask); `subject_mask(path, cutout=True)` returns a transparent cutout instead.
- `layer.set_mask(array_or_image)`, `layer.clip_to(base)`, `layer.set_effect("stroke", size=4, color="#FFF")`,
  `layer.set_adjustment(...)`, `project.move(...)`, `project.remove(...)`, `project.add_guide(...)`.
- Pictures go in as sRGB: ICC profiles (Display P3 from iPhones, Adobe RGB) are converted, EXIF rotation is
  applied, and HEIC / RAW / PSD / SVG are read through macOS `sips`.
- Text: `font` is a PostScript name; `tracking` is extra pixels between letters (not Photoshop's 1/1000 em);
  `leading` is baseline-to-baseline pixels (0 = auto). Point text anchors at its first baseline like a Type-tool
  click; pass `box=(w, h)` for wrapping paragraph text whose top-left is `x, y`.
- `save()` over the path the project came from writes images first and swaps `manifest.json` in atomically, so a
  project open in Compositor updates live; saving elsewhere builds the package aside and moves it into place.

## Templates and batches

Design a template in Compositor and give each replaceable layer a stable name (`Photo`, `Headline`, `Logo`…).

```sh
python -m compkit info template.comp
python -m compkit fill template.comp out.comp --image Photo=shot.jpg --text Headline="New PB" --render out.jpg
python -m compkit batch template.comp rows.csv --out renders/ --max-side 1080 [--keep-comp]
```

CSV columns: `name` (output file name), `text:LAYER`, `image:LAYER` (relative paths resolve beside the CSV),
`focus:LAYER` (`"x,y"` 0–1), `hide:LAYER`, `show:LAYER`. `--keep-comp` keeps each filled project for touch-ups
in the app. A failing row is reported and the rest carry on.

## Format rules worth knowing

See `../docs/writing-comp-files.md` and `../docs/project-format.md`. compkit enforces these, but when writing by
hand: IDs are uppercase UUIDs and name their files (`<ID>.png`, `<ID>.mask.png`); images are 8-bit PNG (RGBA
layers, grayscale masks); blend mode names are exact; every non-optional field of a record must be present (Swift
decoding ignores the defaults in the source); and the app refuses a broken file silently, so run
`comp-render validate`.
