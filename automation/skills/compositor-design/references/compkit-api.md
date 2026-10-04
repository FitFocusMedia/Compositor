# compkit API reference

`from compkit import Project` (run with `compkit-python`). Colors are `"#RRGGBB"`, `"#RGB"`, `(r, g, b)` in
0–255, or floats 0–1. Positions and sizes are document pixels from the canvas's top-left. Every `add_*` returns
a `Layer` and adds it on top (or inside `parent=`, or just above `above=`).

## Contents
- Project: open, new, find, add layers, arrange, save, render
- Layer: properties, placement, pixels, text, masks, effects, adjustments, clipping
- Adjustment settings
- Effect settings
- Module functions (analysis, crops, cutouts, low level)

## Project

| Call | Notes |
| --- | --- |
| `Project.new(width, height, resolution=72)` | Up to 30,000 px a side. |
| `Project.open(path)` | Edit an existing `.comp`. |
| `p.width`, `p.height`, `p.layers` | `layers` runs bottom to top. |
| `p.layer(name_or_id)` | Topmost layer with that name; raises listing the names if none. |
| `p.find(name)`, `p.children(folder_or_None)` | |
| `p.add_image(source, name=None, *, x=0, y=0, width=None, height=None, fit="cover", focus="auto", max_scale=2.0, opacity=1, blend="Normal", parent=None, above=None)` | `source`: path (JPEG, PNG, HEIC, TIFF, RAW, PSD, SVG…) or PIL image. No box fills the canvas. Give `width` or `height` alone to keep the picture's shape. `fit`: `"cover"` (fill, crop), `"contain"` (fit inside, letterbox), `"stretch"`, or `None` (own pixel size at x, y). `focus="auto"` crops around faces/subject; `(fx, fy)` is the point of the picture (0–1 across, 0–1 down) to center the crop on. Pixels are kept at up to `max_scale` × the box (None: full resolution). Pictures enlarged more than 1.25× are reported on stderr. |
| `p.add_fill(color, name="Color Fill", *, x, y, width, height, opacity, blend, parent, above)` | Solid rectangle; whole canvas by default. |
| `p.add_gradient(stops, name="Gradient", *, angle=90, x, y, width, height, opacity, blend, parent, above)` | `stops`: colors, or `(position, color)` / `(position, color, alpha)`. `angle` as Photoshop: 90 bottom→top, 270 top→bottom, 0 left→right. |
| `p.add_text(content, name=None, *, x=0, y=0, font="Helvetica", size=72, color="#000000", align="Left", tracking=0, leading=0, box=None, anchor="baseline", opacity, blend, parent, above)` | Editable text set by the app's typesetter. `box=(w, h)` makes paragraph text: the text's own area, top-left at `x, y`, wrapping within `w` (the app's 12 px padding is added around it); `h=None` makes it as tall as the text. A warning is printed if the text doesn't fit (it would be cut off). Point text: `anchor="baseline"` puts the first baseline at `y` (x is the left end; for Center/Right alignment the middle/right end), `"top-left"` places the text's top-left. |
| `p.add_adjustment(kind, name=None, *, opacity, blend, parent, above, **settings)` | Affects everything below it within its folder. See Adjustment settings. |
| `p.add_group(name, *, opacity=1, parent=None, above=None)` | A folder; pass it as `parent=` to put layers inside. |
| `p.add_graded_photo(source, preset, name="Photo", *, x, y, width, height, fit="cover", focus="auto", max_scale=2.0, amount=1.0, settings=None, local=True, tune=True, parent, above)` | A Lightroom preset (.xmp) applied as the base grade, kept adjustable: a folder with `<name> · Original` (hidden), `<name> · Base Grade (…)`, the preset's local corrections (`Preset · …`, masked Exposure layers) and neutral `Tune · Exposure/Curves/Hue/Saturation/Color Balance` layers. `amount` 0–2 scales the preset; `settings` overrides Camera Raw fields (`comp-render preset` lists them). |
| `p.grade_layer(layer, preset, source=None, *, focus, amount, settings)` | Turns a plain picture layer into a graded folder in place (from a new `source`, or its own pixels); its mask moves to the folder. |
| `p.remove(layer)` | Removes it (and a folder's contents). |
| `p.move(layer, above=other)` / `below=other` / `to_top=True` / `to_bottom=True` | Above/below another layer joins that layer's folder; top/bottom stay within the layer's own folder. |
| `p.add_guide("horizontal" \| "vertical", position)` | |
| `p.check()` | The format rules, before writing (save calls it). |
| `p.save(path=None, validate=True)` | `.comp` path. Same path as opened: in-place live update. Elsewhere: built aside and moved in. Then loaded by the app's own loader (`comp-render validate`) unless `validate=False`. |
| `p.render(output, *, max_side=None, quality=0.9, background="FFFFFF")` | `.jpg`/`.png` via the app's renderer. Save first. PNG keeps transparency; JPEG sits on `background`. |
| `p.open_in_app()` | `open -a Compositor` on the saved project. |

## Layer

| Member | Notes |
| --- | --- |
| `name`, `visible`, `opacity`, `blend_mode` | Settable. Blend modes: Normal, Darken, Multiply, Color Burn, Linear Burn, Lighten, Screen, Color Dodge, Linear Dodge (Add), Overlay, Soft Light, Hard Light, Vivid Light, Linear Light, Pin Light, Hard Mix, Difference, Exclusion, Subtract, Divide, Hue, Saturation, Color, Luminosity. Folders stay Normal. |
| `id`, `is_group`, `adjustment`, `text`, `transform`, `parent`, `record` | `record` is the raw manifest dict. |
| `place(x=, y=, width=, height=, rotation=, flip_x=, flip_y=, sampling=)` | Moves/scales the layer's box without touching pixels. `rotation` degrees clockwise; `sampling` "High quality", "Smooth" or "Nearest". A text layer's box sits 12 px outside its text: use `place_text`. |
| `place_text(x=, y=)` | Moves a text layer so its text area's top-left is at x, y, as `add_text` places it. |
| `text_area` | The text's own area `(x, y, w, h)` in document pixels: the box inside its padding, or point text's lines. |
| `text_metrics()` | The typesetter's measurements: `width`/`height` (pixels), `padding`, `baseline`, `textWidth`/`textHeight` (the lines), `overflow`. |
| `ink_box()` | `(x, y, w, h)` of the layer's visible pixels in document pixels: the letters themselves, for optical alignment. |
| `place_ink(x=, y=)` | Moves the layer so its visible pixels' top-left lands at x, y. |
| `add_backdrop(color, padding=(24, 12), name=None, opacity=1, use="ink")` | A rectangle directly behind this layer, in its folder, sized to its letters (`"ink"`) or text area (`"area"`) plus padding: badges, pills, plates. |
| `pixels()` | The stored pixels as a PIL image (untransformed). |
| `set_pixels(image)` | Replace pixels; drops text/shape metadata. |
| `replace_image(source, fit="cover", focus="auto", max_scale=2.0, preset=None, amount=1.0)` | New picture inside the layer's current box; placement, mask, effects, opacity, blend stay. The template-placeholder operation. On a graded folder, `preset` grades the new picture and the Tune layers keep their settings. |
| `graded_parts()` | For a graded folder: `{"original", "graded", "local": [...], "tune": [...]}`; else None. |
| `regrade(preset, amount=1.0, settings=None)` | Grade a graded folder again from its kept original; Tune settings survive. |
| `set_text(content=None, shrink_to_fit=False, min_size=None, max_width=None, **style)` | style: `font`, `size`, `color`, `align`, `tracking`, `leading`, `box` (text area, as in add_text). Keeps the top-left corner and scale; the layer resizes with its pixels, never stretching the text. `shrink_to_fit` steps the size down until it fits the box (or, for point text, `max_width`, defaulting to the canvas less a right margin equal to the left one), stopping at `min_size` (40% default) with a warning. |
| `set_mask(mask)` | Grayscale (path, PIL image or numpy array) the size of the layer's pixels; white shows. `None` removes it. |
| `mask_subject(refine=12, contrast=25, shift=0)` | Remove Background as an editable mask. |
| `set_effect(kind, **settings)`, `remove_effect(kind)` | See Effect settings. `color="#…"` accepted. |
| `set_adjustment(**settings)` | Change an adjustment layer. |
| `clip_to(base)` | Clipping mask: show only where `base` has pixels. `None` releases. |

## Adjustment settings

Pass as keyword arguments to `add_adjustment` / `set_adjustment`; nested dicts merge into the defaults
(`comp-render defaults` prints every record in full).

| Kind | Settings |
| --- | --- |
| `Hue/Saturation` | `hue` ±180 (±360 accepted), `saturation` ±100, `lightness` ±100, `colorize` |
| `Levels` | `levels={"rgb": {"black": 10, "gamma": 1.1, "white": 245, "outputBlack": 0, "outputWhite": 255}, "red": {...}}` |
| `Curves` | `curves={"rgb": [(0, 0), (128, 150), (255, 255)], "red": [...], "green": [...], "blue": [...]}` — points 0–255, increasing x |
| `Exposure` | `exposureSettings={"exposure": 0.5, "offset": 0, "gamma": 1}` |
| `Gradient Map` | `gradientMapSettings={"shadows": {"red": 0, "green": 0, "blue": 0.2}, "highlights": {...}, "reversed": False}` (0–1) |
| `Grain` | `grainSettings={"amount": 25, "size": 1.5, "roughness": 50, "seed": 0}` |
| `Black & White` | `blackWhiteSettings={"reds": 40, "yellows": 60, "greens": 40, "cyans": 60, "blues": 20, "magentas": 80, "tint": False, "tintHue": 40, "tintSaturation": 20}` |
| `Color Balance` | `colorBalanceSettings={"shadowCyanRed": 0, "shadowMagentaGreen": 0, "shadowYellowBlue": 0, "midCyanRed": …, "highlightYellowBlue": …, "preserveLuminosity": True}` (−100–100) |
| `Gaussian Blur` | `blurRadius` 0.1–250 |
| `Motion Blur` | `motionAngle` −90–90, `motionDistance` 1–2000 |
| `Add Noise` | `noiseAmount` 0.1–400, `noiseGaussian`, `noiseMonochromatic`, `noiseSeed` |
| `Invert` | — |

Out-of-range values make `save()` fail with the app's reason.

## Effect settings

`set_effect(kind, ...)`; colors as `color="#…"` (or `red`/`green`/`blue` 0–1); `opacity` 0–1.

| Kind | Settings (defaults) |
| --- | --- |
| `stroke` | `size` 4 (0–500), `inside` False, color black, opacity 1 |
| `shadow` (drop shadow) | `angle` 90 (light from above → shadow falls down), `distance` 20, `blur` 20, color black, opacity 0.5 |
| `innerShadow` | `angle` 90, `distance` 10, `blur` 10, black, 0.5 |
| `outerGlow` / `innerGlow` | `size` 20 / 10, white, 0.75 |
| `colorOverlay` | color black, opacity 1 |

## Module functions

| Function | Notes |
| --- | --- |
| `analyze(source)` | Apple Vision via `comp-render subject`: `faces`, `people`, `subject` (with `coverage`), `salient` boxes as 0–1 fractions from the top-left, plus `width`/`height`. Cached per file. |
| `auto_focus(analysis, image_size, box)` | The point `focus="auto"` centers the crop on. |
| `cover_crop(size, box, focus)` | `(left, top, width, height)` of the crop centered on `focus`, kept inside the picture. |
| `fit_image(image, box, fit="cover", focus="auto", max_scale=2.0, analysis=None, label=None)` | Pixels + placement for a box; with a `label`, reports enlargement. |
| `subject_mask(source, refine=12, contrast=25, shift=0, cutout=False)` | Grayscale subject mask, or with `cutout=True` an RGBA cutout. |
| `measure(image, region="frame")` | CIELAB numbers for tuning to guidelines: `lightness` mean and percentiles, `blown`, `channel_clip`, `crushed` (%), `chroma`, `whites` and `cast` (a*/b*); `region="subject"` measures the subject only. CLI: `compkit measure images… [--subject] [--json]`. |
| `read_preset(path)` | A preset's mapping report (`comp-render preset`): `mapped`, `whiteBalance`, `notes`, `skipped`, `localCorrections`, `fields`. |
| `develop(source, preset, output, *, original=None, crop=None, size=None, amount=1.0, as_shot=None, settings=None, seed=0)` | Grade a picture to a PNG with Compositor's Camera Raw engine (`comp-render develop`); returns the report. |
| `load_image(source)` | Upright, sRGB, RGBA PIL image from any supported file. |
| `defaults()` | The app's default records (`comp-render defaults`). |
| `comp_render(*args)` | Run comp-render; raises `CompError` with its message. |
