---
name: compositor-grade
description: Apply the user's Lightroom / Camera Raw preset (.xmp) — their base grade, kept in ~/Documents/Presets (e.g. "God Tones") — to photos with Compositor's own Camera Raw engine, keeping the untouched original and adding editable tune layers so each image can then be adjusted to meet brand or client guidelines. Use whenever the user wants their preset, base grade, look or edit applied to photos; a set of photos graded consistently; a Lightroom preset used outside Lightroom; graded photos tuned, re-graded or made stronger/weaker; or a template's photos graded during a batch — even if they don't say XMP or Compositor.
---

# Base grades from Lightroom presets

`comp-render develop` reads a Lightroom / Camera Raw preset and applies every setting that has a counterpart in
Compositor's Camera Raw engine. compkit wraps the result in a **graded photo**: a folder whose layers are, bottom
to top:

| Layer | What it's for |
| --- | --- |
| `Photo · Original` (hidden) | The photo as decoded and cropped, ungraded: kept so it can be graded again |
| `Photo · Base Grade (Preset)` | The preset applied (by Compositor's engine, baked into pixels) |
| `Preset · Mask 2 (+0.09 EV)` | The preset's radial/linear local corrections, as masked Exposure layers |
| `Tune · Exposure`, `Tune · Curves`, `Tune · Hue/Saturation`, `Tune · Color Balance` | Neutral adjustment layers for per-photo tuning |

The folder keeps the grade and the tuning on this photo only. In Compositor the person double-clicks a Tune layer
to adjust it, or toggles the folder's layers to compare. Commands come from the toolkit (if missing, use the
compositor-toolkit skill).

## 1. Know what the preset will and won't do

The person's presets live in `~/Documents/Presets` (`ls ~/Documents/Presets`). Before the first use of a preset,
read its mapping:

```bash
comp-render preset ~/Documents/Presets/"God Tones.xmp"
```

That lists `mapped` settings (Lightroom name → Compositor field and value), the `whiteBalance` conversion,
`notes` about approximations, what was `skipped` (no counterpart) and its `localCorrections`. Tell the person
once, briefly, what doesn't carry over. Typical gaps are the camera profile (e.g. Adobe Color), LUTs, brush/AI
masks and Upright.

**RAW files** (the person shoots Sony a7R V .ARW) get the part of the preset that Lightroom applies to sensor data
done while decoding, in floating point on all 14 bits, before anything becomes 8-bit:
- exposure;
- white balance at the preset's exact Kelvin/tint;
- highlights and whites, rolled off from the RAW's headroom;
- lens corrections, from the camera's own correction data.

The rest of the preset (shadows, texture, clarity, curves, calibration, color, detail) runs on the result. JPEG
and HEIC photos get the white balance as the shift the preset made on its own photo, the nearest equivalent.

**Matching Lightroom's rendering.** Adobe's camera profiles and math aren't available, so on its own the grade is
close to Lightroom's in character but visibly different: on the person's workshop shoot, about ΔE 5 at a coarse
scale, darker midtones, less saturated skin and teal coming out blue. The fix is a look learned from their own
Lightroom exports, which embed each frame's exact develop settings:

```bash
compkit learn-look "<shoot>/Photo Edits" "<shoot>/RAW" -o ~/Documents/Presets/"God Tones (a7R V).cube"
```

It develops each RAW with its export's settings, compares the two, and fits a 3D color table from the
differences, scored on frames held out of the fitting.

On the workshop (170 a7R V exports), frames held out of training went from coarse ΔE 5.1 with the old pipeline
to 2.7, about the smallest difference most people notice, with no brightness or color bias left. The RAW stage
itself doesn't change color match; it keeps highlights (stage lights hold their shape and color instead of
clipping white) and precision. The look is what matches Adobe's rendering. The look is saved at
`~/Documents/Presets/God Tones (a7R V).cube`.

Pass it as `--match` (or `match=`) whenever grading that camera with that preset. A look is specific to its
camera and preset, so learn another for a different body or preset. Exports without embedded settings (no
"include develop settings" on export) can't be used.

## 2. Grade

Single photos or a folder of them, each into its own tunable project (plus a JPEG with `--render`):

```bash
compkit grade shoot/*.CR3 --preset ~/Documents/Presets/"God Tones.xmp" --out graded/ --render
```

`--amount 0.7` applies the preset at 70% (0–2). `--set exposure=0.2 --set clarity=10` overrides Camera Raw
fields after the preset; `comp-render preset` lists the field names under `fields`. For RAW files the per-show
settings the person syncs in Lightroom go to the RAW stage: `--set raw.temperature=4170 --set raw.tint=30
--set raw.exposure=-0.14 --set raw.highlights=-11`. `--match look.cube` adds the learned look, `--list
picks.txt` grades a list (a cull's picks, see compositor-cull) and `--jobs 6` grades several at once. Point curves aren't
overridable this way: use the Tune · Curves layer. `--max-side` caps the project size (default 2048) and only
ever shrinks, so small photos stay at their own size.

In a design or template (Python), use `p.add_graded_photo(photo, preset, name="Photo", x=…, y=…, width=…,
height=…)` where you'd use `add_image`; it crops around faces and the subject the same way. To grade an
existing project's photo layer in place: `compkit grade project.comp --layer Photo --preset … [--out new.comp]`.
In a template batch, `compkit batch template.comp rows.csv --out renders/ --preset … [--amount …]` grades every
`image:` fill. A plain photo layer becomes a graded folder, and an existing graded folder keeps its Tune settings
across rows.

## 3. Tune to the guidelines

Guidelines differ per brand or client (brightness, contrast, skin tone, warmth, saturation, consistency across a
set). If you don't know them, ask. If you can't, start from these and say that you assumed them:

| Aim | Starting target (`compkit measure`) |
| --- | --- |
| Bright enough for social feeds | subject median L* about 55–70 (`--subject`), frame p25 not below ~20 unless the backdrop is meant to be dark |
| Highlights keep detail | `blown` ≤ 1% (pure white); `channel_clip` matters only if a color is meant to stay subtle |
| Shadows keep detail | `crushed` ≤ 1–2% unless the look is deliberately inky |
| A set looks consistent | whites' L* within ~2 and their a*/b* within ~1.5 across the set; medians within ~6 |

Measure every render, tune, and measure again:

```bash
compkit measure graded/*.jpg                 # whole frame
compkit measure graded/*.jpg --subject       # just the subject (what Remove Background keeps)
```

The percentiles (p25 / median / p75) are taken over the same pixels every pass. That makes them steady to compare
before and after, unlike an average over "mid-tone" pixels, which counts different pixels as the image
brightens. Then adjust per photo, preferring the Tune layers so everything stays editable:

```python
from compkit import Project
p = Project.open("graded/IMG_0412.comp")
photo = p.layer("Photo")
tune = {layer.name: layer for layer in photo.graded_parts()["tune"]}
tune["Tune · Exposure"].set_adjustment(exposureSettings={"exposure": 0.15})
tune["Tune · Hue/Saturation"].set_adjustment(saturation=-8)
tune["Tune · Curves"].set_adjustment(curves={"rgb": [(0, 8), (128, 132), (255, 250)]})
tune["Tune · Color Balance"].set_adjustment(colorBalanceSettings={"midYellowBlue": -6})   # negative = warmer
p.save(); p.render("graded/IMG_0412.jpg")
```

What the Tune controls do:

| Layer | Settings |
| --- | --- |
| `Tune · Exposure` | `exposureSettings`: `exposure` in stops (+0.1 is a gentle lift), `offset` (−0.5–0.5, lifts or deepens shadows), `gamma` (1 neutral, above 1 brightens midtones) |
| `Tune · Curves` | `curves={"rgb": [(0, 0), (128, 140), (255, 255)], "red": …}`: points 0–255, rising x |
| `Tune · Hue/Saturation` | `hue` ±180°, `saturation` and `lightness` ±100 (saturation is roughly a percentage change) |
| `Tune · Color Balance` | `colorBalanceSettings`: `{shadow,mid,highlight}{CyanRed,MagentaGreen,YellowBlue}`, each −100–100, where positive moves toward the second color (red, green, blue), so warmer is +CyanRed / −YellowBlue and more magenta is −MagentaGreen; `preserveLuminosity` |

The base grade is an 8-bit image: highlights the preset blows out or shadows it crushes can't be brought back by
Tune layers. Re-grade instead (below), for example with `settings={"highlights": -20}` or `{"blacks": 15}`.

Measure, don't eyeball alone. For example, render and compute the mean luminance, the share of clipped
highlights and shadows, and the color cast of near-neutral areas with PIL/numpy (`compkit-python`). Compare the
numbers against the guideline and across the set, then look at the result too (a contact sheet:
`compkit sheet graded/*.jpg -o graded/_sheet.jpg`).

To change the base grade itself rather than tune on top of it, re-grade:
`photo.regrade(amount=0.8, settings={"raw.exposure": 0.1})`. Every project remembers its source file, crop,
preset, strength, overrides and look in `<name>.sources.json` beside the `.comp`, so a re-grade decodes the RAW
again with all its latitude. Arguments left out keep what the photo was graded with. If the RAW has moved, it
falls back to the hidden 8-bit original and says so. Keep the sources file with the project when moving it. Tune layer settings survive, the preset's local
corrections are rebuilt, and the strength and overrides are written into the Base Grade layer's name
(`Photo · Base Grade (God Tones at 80%, shadows -5)`) so the person can see them in the app.

## Check before handing over

Show the original and the graded version side by side for at least one photo, using the hidden Original layer's
pixels (`photo.graded_parts()["original"].pixels()`). Report the folder of projects and renders, what the preset
couldn't carry over, and any photo that needed tuning beyond the rest.
