---
name: compositor-photo
description: Prepare photos with Apple Vision through the Compositor toolkit — crop or resize photos to exact sizes (4:5 feed, 9:16 story, 16:9 thumbnail, any WxH) without cutting off heads by keeping faces and the subject in frame, remove backgrounds / cut out an athlete, person, product or animal to a transparent PNG, make subject masks, and check what an automatic crop will keep. Use whenever the user wants photos reframed, cropped for social, batch-resized, cut out or background-removed — even if they don't mention Compositor — and when a template batch crops someone badly.
---

# Photo prep: subject-aware crops and cutouts

These run Apple's Vision models on the Mac through `comp-render` (the Compositor app's own code), so there's no
upload and no cost. Commands come from the toolkit (if missing, use the compositor-toolkit skill).

## Crop to exact sizes, keeping what matters

```bash
compkit crop shots/*.jpg --box 1080x1350 --box 1080x1920 --box 1920x1080 --out crops/
```

Each photo comes out once per size as `<photo>-<W>x<H>.jpg` (`--format png` to keep transparency,
`--quality 92`). HEIC, RAW, PSD and TIFF are fine; color profiles become sRGB and phone rotation is applied.

Photos smaller than the size get enlarged, and the tool reports by how much. Up to about 1.5× is fine; past
that it looks soft, and past 2× tell the person and ask for a bigger original. If they need something now,
deliver the crops anyway and say in your message which are placeholders (keep the tool's file names, so a rerun
with the bigger original replaces them). Padding (`--pad`) enlarges too, so it doesn't fix a small source.

Check a set of crops on one page with `compkit sheet crops/*.jpg -o crops/_sheet.jpg` and look at it.

How the crop is chosen (`auto_focus`), so its results can be predicted and explained:
1. The subject (what Remove Background would keep; else the people; else where the eye goes) is centered if
   it fits the crop.
2. If it doesn't fit, the crop anchors on human faces: all of them are kept whole, and when height is being
   cut they sit in the top third, with no empty space above the subject.
3. Without faces (animals, products, faces turned away) it centers Vision's attention saliency, which tends to
   land on heads and eyes; failing that, the top of the subject is kept when cutting height, the middle when
   cutting width.
4. Nothing found: a plain center crop.

**Check before handing over.** Look at the crops (the Read tool can open them), or see why a crop went where it
did:

```bash
compkit analyze photo.jpg --box 1080x1350 --preview check.jpg
```

That prints what Vision found (faces, people, subject with its coverage, salient regions, as fractions from the
top-left), the `focus` it chose (the point the crop centers on) and the crop, and draws them: faces green,
subject yellow, people magenta, salient blue, the crop white.

To override, give the point of the photo to center on: `--focus x,y`, 0–1 across and 0–1 down (`0.66,0.5` is
two-thirds of the way across; the crop stays inside the photo). It applies to every photo and size in that
call, so fix one photo by rerunning just that photo, at just the size that needs it, with its own `--focus`;
the new crop replaces the old one:

```bash
compkit crop Parrot.heic --box 1080x1920 --focus 0.6,0.5 --out crops/      # replaces crops/Parrot-1080x1920.jpg
```

Preview a hand-set crop first with `compkit analyze Parrot.heic --box 1080x1920 --focus 0.6,0.5 --preview check.jpg`.
In a template batch use a `focus:LAYER` column instead.

To find where a feature is (an eye, a beak, a ball), check the `salient` box first, which often marks the head.
For more precision, `comp-render cutout photo.jpg mask.png --mask` gives the subject's exact outline to measure
with numpy. After any crop, check the edges: a subject within a few pixels of the frame looks cut even when it
technically isn't, so nudge the focus.

**When the subject is wider (or taller) than the shape allows**, like a head that fills a square photo going
into a 9:16 story, something has to go. Either center on what identifies the subject (the eyes plus the mouth,
beak or ball) with `--focus`, or keep the whole photo with `--pad blur` (the photo fitted inside the frame over a
blurred, darkened copy of itself) or `--pad '#111111'` for a solid color, and offer the person the choice.
Padded versions are saved as `<photo>-<W>x<H>-pad.jpg`, beside the regular crop rather than over it.

Limits to say out loud when they matter: Vision's face detector finds human faces only; group shots wider than
the crop keep the faces that fit; heavy motion blur or very busy backgrounds can confuse the subject mask.

## Cut out a subject (background removal)

```bash
comp-render cutout athlete.jpg athlete-cutout.png            # transparent PNG, full resolution
comp-render cutout athlete.jpg athlete-mask.png --mask       # white-on-black mask instead
```

Edge refinement matches Compositor's Advanced mode: `--refine 0–40` (default 12, recovers hair and fur edges),
`--contrast 0–100` (default 25, clears haze), `--shift -10–10` (negative pulls the edge in to drop a background
fringe), `--basic` for Vision's raw mask.

A halo of background color around the subject usually goes with `--shift -2` to `-4`. A negative shift also
eats thin parts (beak tips, fingers, hair ends, racquet strings), so check those afterwards; if they broke, keep
the shift and lower `--refine` to about 4, or try `--basic`. Compare two or three settings side by side when
edges matter.

Inside a design, prefer the non-destructive version: `layer.mask_subject()` in compkit hides the background with
a layer mask the person can still paint on in Compositor (see compositor-design).

Check a cutout on two backgrounds before handing it over: one opposite in color to the photo's original
background, where a fringe of it shows up, and black or white. Edges that look clean on one can fringe on the
other:

```bash
compkit-python - <<'PY'
from PIL import Image
cut = Image.open("athlete-cutout.png")
for name, color in (("magenta", "#FF00FF"), ("black", "#000000")):
    back = Image.new("RGBA", cut.size, color)
    back.alpha_composite(cut)
    back.convert("RGB").save(f"cutout-on-{name}.jpg")
PY
```

## From Python

```python
from compkit import analyze, auto_focus, cover_crop, subject_mask
found = analyze("shot.jpg")                                  # dict of boxes (cached per file)
focus = auto_focus(found, (found["width"], found["height"]), (1080, 1350))
left, top, w, h = cover_crop((found["width"], found["height"]), (1080, 1350), focus)
cutout = subject_mask("shot.jpg", cutout=True)               # PIL RGBA
```
