---
name: compositor-design
description: Design or edit a graphic as a layered, editable Compositor project (.comp) and render it to JPG/PNG — Instagram posts and carousel slides, YouTube thumbnails, story/reel covers, banners, quote cards, PB or announcement graphics, posters, and transparent overlays/lower-thirds/title cards for video. Use this whenever the user wants a graphic made, laid out, restyled or changed and an editable layered file is useful, when they mention Compositor or a .comp file, or want Claude to design live while they watch the canvas — even if they just say "make me a thumbnail/post/graphic". Not for Canva-template work or video editing itself.
---

# Designing in Compositor

Compositor is a Photoshop-style Mac app. Its project (`.comp`) is a folder of PNG layers plus `manifest.json`, so a
design can be written from Python with **compkit** and checked and flattened with **comp-render**, which is the
app's own loader and renderer compiled as a CLI, so a render matches the app's export exactly. The person gets
real layers they can open and tweak in Compositor, not a flat picture.

Commands (installed by the toolkit; if missing, use the compositor-toolkit skill):
- `compkit-python script.py` runs Python with `compkit` importable
- `comp-render render|validate|info|text|fonts|subject|cutout …`
- `compkit info|analyze|crop|fill|batch|where`

## Workflow

1. **Pick the canvas.** Common sizes: Instagram portrait 1080×1350, square 1080×1080, story/reel 1080×1920,
   YouTube thumbnail 1280×720 (or 1920×1080), 16:9 video overlay 1920×1080 (or 3840×2160). Platform habits worth
   designing for:
   - **YouTube thumbnails:** keep the bottom-right corner quiet (the duration badge sits there), use a few very
     large words, and check a small render too: `p.render("check-320.jpg", max_side=320)`.
   - **Stories and reels:** keep key text out of roughly the top 14% and bottom 20%, where the app's own UI sits.
   - **Before designing around a photo:** see where its subject is with
     `compkit analyze photo.jpg --box WxH --preview check.jpg`. If it finds no faces or subject (an abstract or
     placeholder picture), lay out for the photo you expect instead: its subject will be centered in the photo's
     box, so keep that box clear of the text.
2. **Write a script** (e.g. `make_thumbnail.py` beside the output) rather than one-off commands: the design is then
   repeatable and each tweak is a quick edit and rerun.
3. **Save and render**, then **look at the render** with the Read tool before showing the person. Check what a
   designer would: text cut off or too close to the edge, low contrast, a face cropped badly, things off-center.
   Fix and rerun; two or three passes is normal.
4. **Hand over** the `.comp` and the render. Offer `open -a Compositor path.comp`: once it's open, every later
   save updates the canvas live (about 0.3 s), so in a live session work in small steps and pause briefly between
   saves so each one is visible.

Name layers for what they hold (`Photo`, `Headline`, `Subhead`, `Logo`, `CTA`, `Background`): it makes the
file readable, and it means the design can later be reused as a template by name (compositor-batch skill).
Keep names unique: `p.layer(name)` returns the topmost layer with that name, so a folder called `Photo` holding
a layer called `Photo` is ambiguous (call the folder `Picture`). If
the photo will be swapped later, give it its own box clear of the text, so a new photo's subject (which the
automatic crop centers) lands away from the words. The swap is then
`compkit fill design.comp new.comp --image Photo=new.jpg --render new.jpg`. For the person's Lightroom base
grade, use `p.add_graded_photo(photo, preset, …)` in place of `add_image` (compositor-grade skill).

## compkit in one example

```python
from compkit import Project

p = Project.new(1080, 1350)
p.add_image("shot.heic", name="Photo")                       # fills the canvas; crops around faces/subject
p.add_adjustment("Curves", name="Grade", curves={"rgb": [(0, 12), (128, 134), (255, 248)]})
p.add_gradient([(0, "#000", 0), (0.55, "#000", 0), (1, "#000", 0.85)], name="Fade", angle=270)  # dark at bottom
title = p.add_group("Title")
p.add_fill("#E4FF3A", name="Accent", x=80, y=1028, width=120, height=10, parent=title)
p.add_text("NEW PB", name="Headline", x=80, y=1050, box=(920, 130), font="Helvetica-Bold", size=96,
           color="#FFFFFF", parent=title).set_effect("shadow", opacity=0.4, blur=16, distance=6)
# box=(920, 130) is the text's own area: letters start at x=80, y=1050 and wrap within 920 × 130
p.add_adjustment("Grain", opacity=0.5)
p.save("post.comp")              # checked against the app's own loader; raises if the app would refuse it
p.render("post.jpg")             # flattened by the app's renderer (png keeps transparency)
```

Layers stack bottom to top in the order added; adjustment layers change everything below them (inside their
folder, if they're in one). Full API with every option: [references/compkit-api.md](references/compkit-api.md).

## Things that matter

- **Fonts are PostScript names** (`Helvetica-Bold`, `Futura-CondensedExtraBold`, `Anton-Regular`). What's
  installed differs per Mac, so look before choosing: `comp-render fonts condensed`, `comp-render fonts anton`. A
  wrong name silently falls back to the system font.
- **Type units are pixels.** `tracking` is extra pixels between letters (not Photoshop's 1/1000 em: 4–10 is
  already wide), `leading` is baseline-to-baseline pixels (0 = auto, 120%).
- **Use paragraph boxes for anything whose words may change, and for center/right alignment.** `x, y,
  box=(w, h)` is the text's own area: letters start at `x, y`, wrap within `w` and should fit in `h` (one line
  needs about 1.2 × size; `box=(w, None)` makes it exactly as tall as the text). `add_text` and `set_text`
  warn if the text doesn't fit, since the overflow is cut off. An auto-height box is measured once: after
  changing the size or words with `set_text`, pass `box=(w, None)` again to refit it. Point text grows from its top-left when edited, so centered point
  text drifts; in a box, `set_text(..., shrink_to_fit=True)` steps the size down until the words fit.
- **Point text is placed by its first baseline** at `x, y` (like a Type-tool click); `anchor="top-left"` places
  its top-left instead.
- **Lining things up**: `layer.text_area` is the text's (x, y, w, h); `layer.ink_box()` is where the visible
  letters (or any layer's pixels) actually are; `layer.place_ink(x, y)` moves a layer so its letters' top-left
  lands there (stack lines with `y = above.ink_box()[1] + above.ink_box()[3] + gap`);
  `layer.place_text(x, y)` moves text by its text area; `layer.text_metrics()` gives the typesetter's
  measurements. `layer.place(...)` moves a layer's whole box, which for text sits 12 px outside the letters.
- **Badges and plates**: `text.add_backdrop("#E4FF3A", padding=(24, 12))` puts a rectangle sized to the letters
  directly behind the text, in its folder.
- **Stacking order**: `p.move(layer, above=other)` / `below=other` joins that layer's folder;
  `to_top=True` / `to_bottom=True` move within the layer's own folder. Check the tree with `compkit info`
  afterwards, since a render won't show a layer that escaped its folder.
- **Pictures**: paths or PIL images; HEIC/RAW/PSD work; color profiles become sRGB. `fit="cover"` fills a box and
  by default crops around faces and the subject (`focus="auto"`); `"contain"` letterboxes.
- **Cutouts**: `layer.mask_subject()` hides the background with an editable mask (Remove Background).
- **Opacity/blend**: `opacity` 0–1; blend modes are Photoshop's names, exactly (`"Multiply"`, `"Soft Light"`,
  `"Linear Dodge (Add)"`, …). A folder's opacity multiplies into each layer inside it.
- **Keep a photo's grade with the photo.** An adjustment layer changes everything below it in its folder, so a
  grade meant for the photo also tints a background panel or fill beneath it, which shows as a seam where the
  photo ends. Put the photo, its grade and its fades in their own folder (`parent=`) so the grade touches only
  them.
- **Overlays for video** (lower-thirds, title cards): render to `.png` to keep transparency, and don't add a
  background layer.
- **Editing an existing project**: `Project.open(path)`, `p.layer("Headline").set_text("…")`, then `p.save()`
  writes back in place (images first, manifest swapped in atomically, so an open canvas updates in one step).
  `compkit info path.comp` shows the layer tree first.
- If something won't load in the app, `comp-render validate path.comp` gives the app's own reason (the app
  itself refuses a bad file silently).
