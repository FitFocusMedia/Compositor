---
name: compositor-batch
description: Mass-produce graphics from one Compositor (.comp) design by swapping photos and text per row — weekly PB or results posts, athlete/client spotlights, fixture and score cards, a thumbnail per video, carousel slides, the same post for several people or sizes. Use this whenever the user wants many versions of one design, says "do this for each athlete/client/row/photo", has a spreadsheet/CSV/list of names and photos to turn into graphics, or wants a design turned into a reusable template — even if they don't mention Compositor or templates.
---

# Batch graphics from a Compositor template

A **template** is an ordinary Compositor project whose replaceable layers have stable names (`Photo`,
`Headline`, `Subhead`, `Logo`…). `compkit` fills those layers by name and leaves everything else alone
(placement, masks, effects, grades, fonts), then the app's own renderer flattens each result.

Commands come from the toolkit (if missing, use the compositor-toolkit skill): `compkit`, `compkit-python`,
`comp-render`.

## 1. Get or make the template

- **Existing `.comp`**: run `compkit info template.comp` to see the layer tree and which layers are photos and
  text. Confirm with the person which layers vary; if they can't be asked (an unattended run), infer it from the
  layer names and the data, and say what you assumed in the report.
- **Check the template before filling it.** `compkit info` shows each text layer's box (`box W×H at x,y`, or
  `point text`) and marks text already cut off by its box (`OVERFLOWS ITS BOX`). A box holds about
  `h / (1.2 × size)` lines at full size; a longer value shrinks until it fits, wrapping onto more lines as the
  smaller size allows, so a one-line box turns long values into small type. Point text without a box shrinks
  only to keep within the canvas, with the same margin on the right as on the left.
- **Long values: which fix.** If you're building or allowed to change the template and the layout has room, give
  the box space for two lines (`layer.set_text(box=(w, h))`, saved to the template or a copy). If the template
  is fixed, or a taller box would run into the next element, keep the template and handle it in the data:
  deliver the wording as given plus an `-alt` row with shorter copy (step 3).
- **Hiding parts.** `hide:Tag` hides one layer. If something belongs with it (an accent bar, a plate), add a
  `hide:` column for that layer too; in a template you build, put related layers in one folder so one column
  hides them all.
- **No template yet**: build one with the compositor-design skill, naming the variable layers. These choices
  matter for a template that survives real data:
  - put changing words in **paragraph boxes** (`box=(w, h)`), so long values wrap and shrink inside a known
    area instead of running off the canvas;
  - give photo placeholders the **box they should fill** (`add_image(..., x, y, width, height)`); new photos
    are cropped into that box;
  - for the person's base grade on every photo, make the placeholder a graded photo
    (`add_graded_photo`, see the compositor-grade skill) or pass `--preset` (and `--match` for their learned
    Lightroom look) to the batch.

## 2. Get the data into a CSV

Columns: `name` (each output's file name, no extension) plus one column per change:

| Column | Effect |
| --- | --- |
| `text:LAYER` | New words for a text layer (`\n` for a line break). Shrinks to fit its box. |
| `image:LAYER` | New picture for a layer, cropped into its box around faces and the subject. Relative paths resolve beside the CSV. |
| `focus:LAYER` | Optional `"x,y"`: the point of the photo to center the crop on (0–1 across, 0–1 down), when the automatic crop is wrong. |
| `hide:LAYER` / `show:LAYER` | Any non-empty value except `0`/`no`/`false` hides/shows it (badges, optional logos). |

Data from a spreadsheet, a pasted list or a folder of photos all becomes this CSV; write it next to the outputs
so the run can be repeated. When the data is informal ("220kg squat, week 12"), write it in the template's own
copy style (its sample text shows it: "Squat 220 kg · Week 12"), don't repeat the headline in the subhead, and
list any rewording in your report. Make `name` values filename-safe (`jack-smith-pb`). Photos can be JPEG, HEIC, PNG,
TIFF or RAW, and paths may contain spaces.

## 3. Run it

```bash
compkit batch template.comp rows.csv --out renders/ --keep-comp
```

- Renders come out at the template's canvas size. `--max-side N` only scales them down (it caps the longer
  side, so `--max-side 1080` would turn a 1080×1350 post into 864×1080): use it for previews, not deliverables.
- `--format png` keeps transparency (overlays); `--quality 0.92` for JPEG.
- `--keep-comp` leaves each filled `.comp` beside its render, so one can be touched up in Compositor and
  re-rendered with `comp-render render renders/x.comp renders/x.jpg`. Worth it unless the person only wants
  images.
- A row that fails (missing photo, unknown layer) is reported and the rest carry on; the exit code is 1 if
  any failed. Fix and rerun just those rows with a smaller CSV.
- Watch stderr. `set at NNpx (from MM) to fit` means text shrank: if a headline ends up near or below its
  subhead's size the hierarchy is lost, so suggest shorter copy (or moving detail into the subhead) rather than
  shipping it. If the person can't be asked, deliver their wording as given plus an alternative row (name it
  `…-alt`), and say which you'd pick and why. `enlarged N×` means a photo is smaller than its box: past about 1.5× it looks soft, and past 2×
  ask for a bigger original.
- `--preset base.xmp [--amount 0.8]` grades every photo fill with a Lightroom preset, kept tunable (see the
  compositor-grade skill).
- To redo some rows, run a CSV with just those rows into the same `--out` folder; their renders and `.comp`s
  are replaced and the rest are left alone.
- One-off fill: `compkit fill template.comp out.comp --text Headline="…" --image Photo=shot.jpg --focus
  Photo=0.4,0.5 --render out.jpg`.

## 4. Check every result before handing over

Make a contact sheet and look at it with the Read tool:

```bash
compkit sheet renders/*.jpg -o renders/_contact-sheet.jpg
```

Look for crops that cut heads or miss the subject (or leave it touching the frame's edge), text that shrank too
far or collides with something, elements stranded by a hidden layer, and photos that are too dark or busy behind
the text. Fix crops with a `focus:` value (`compkit analyze photo.jpg
--box WxH --preview check.jpg` shows what the automatic crop saw and where it centered; the box size is the
photo layer's, from `compkit info`), then rerun only those rows. Report where
the renders are, how many, and anything you corrected or that still needs a human eye.

## Several sizes

A different shape (4:5 feed post, 9:16 story, 16:9 thumbnail) needs its own template, since the layout itself
changes; run the batch once per template with the same CSV. If only the photo needs to come in several sizes,
`compkit crop photo.jpg --box 1080x1350 --box 1080x1920 --out crops/` crops around the subject without any
template (see the compositor-photo skill).
