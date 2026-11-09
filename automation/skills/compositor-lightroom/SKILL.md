---
name: compositor-lightroom
description: Edit a whole show in Lightroom Classic by writing settings into the RAWs' XMP sidecars — cull ratings, base white balance, per-division looks, blue/coloured stage-light correction, crops with the person's framing, levelling from the camera's tilt sensor — then have the person read them in, and sort the export into division folders. Use whenever the person is editing a show in Lightroom Classic and wants edits applied in bulk, edits copied from what they've already done, ratings or settings pushed into Lightroom, crops/straightening automated, white balance fixed across a show, a Read Metadata / Save Metadata step planned, exported photos renamed or sorted into folders, or blank/white corners found — even if they just say "apply this to the rest" or "can you speed up the editing".
---

# Editing a show in Lightroom Classic through XMP sidecars

Lightroom has no scripting from outside. The only safe channel is the **XMP sidecar** next to each RAW: write
settings into it, then the person runs **Metadata › Read Metadata from Files** on exactly those photos. Built and
measured on ICN Nationals 2026 (13,688 Canon frames, 6,236 delivered). The code is `compkit.lightroom`
(`automation/compkit/lightroom.py`, run with `compkit-python`): catalogue copy and readers, sidecar check and write,
rule crops and levelling, blue-light white balance, the corner scan and the delivery organiser; its module notes list
each function. That show's one-off scripts stay for reference in
`/Volumes/8TB Part 1/ICN Nationals/Durran - Compositor/pipeline-scripts-20261021/`. Culling itself is the
compositor-cull skill.

## Ground rules (each one cost the person hours when broken)

- **The catalogue is the truth.** Read it only from a copy: `cp` the `.lrcat` and `.lrcat-wal` to the scratchpad
  and open the copy (`sqlite3 -readonly` or `?mode=ro`). Never touch the live catalogue.
- **Never ask for a catalogue-wide Read Metadata** unless you have just checked every sidecar it will read against
  a fresh catalogue copy (see the routine). Stale sidecars silently undo the person's edits.
- **A write isn't done until it's read.** After the person says "done", check for `From Metadata` history steps
  newer than the write and that each photo now holds the planned values. Once the read never happened and the
  person spent an evening redoing it by hand.
- **Never overwrite the person's edits.** Skip any photo with a hand-made history step after the state you planned
  from; say how many you skipped.
- **Ask before standing changes** (Upright off for thousands of photos, ratings, labels). Show a sample sheet first.

## Reading the catalogue

- Photo → file: `Adobe_images.rootFile → AgLibraryFile (baseName, extension, folder) → AgLibraryFolder
  (pathFromRoot) → AgLibraryRootFolder (absolutePath, name)`. `pathFromRoot` is empty when the root folder *is* the
  division folder.
- Settings: `Adobe_imageDevelopSettings.text`, a Lua table. Parse **top-level keys only**. Nested `Preset = {…}`
  and `Look` blocks repeat `Exposure2012`, `Temperature`, `PerspectiveUpright` with the preset's values; a flat
  regex reads those and gives false "changes".
- History: `Adobe_libraryImageDevelopHistoryStep (image, dateCreated, name, text)`. `dateCreated` is seconds since
  2001-01-01 (add 978307200). `text` is a 4-byte length + zlib; snapshots also carry `agHistoryOrientation` (ignore).
  Hand edits = any step not `Import/…`, `From Metadata`, `Synchronize Settings`, `Export…`.
- Virtual copies: `Adobe_images.masterImage` is set. They share the master's sidecar: hide them (filter Kind:
  Master Photos) before any read, export them separately.
- Orientation: `Adobe_images.orientation` `AB` = normal, `DA` = EXIF 8 (portrait, most stage frames), `BC` = 6.
- `Adobe_AdditionalMetadata.externalXmpIsDirty` and `lastSynchronizedTimestamp` vs the sidecar's mtime
  approximate Lightroom's Metadata Status.

## Writing sidecars

- Edit only the **top-level `rdf:Description` tag's attributes** (`crs:…`, `xmp:Label`, `xmp:Rating`). Nested
  `crs:Preset` / `crs:Look` blocks repeat keys; never touch them.
- Before writing a photo: its sidecar must match the catalogue on every numeric top-level field (about 50). If not,
  skip it and say so. Old leftovers from your own earlier writes can be rebuilt from their backup zip if that copy
  matches the catalogue.
- Back up every file you change (zip, paths absolute minus the leading `/`), write, re-read and verify only the
  planned keys changed, then delete only the `._` AppleDouble files you created (exFAT drives: 1 MB each).
- Missing attributes (an untouched sidecar has no `crs:Crop*`) go in after `crs:HasSettings="True"`.
- Before import, `compkit.cull.write_ratings` writes minimal rating-only sidecars, and only where none exists.

## The person's side, in Lightroom (give these steps literally)

1. Sync first if anything is stale: Library Filter › Metadata, change a column to **Metadata Status**, select
   **Changed on disk** + **Conflict detected**, ⌘A, Metadata › **Save Metadata to Files**, **Overwrite**. A plain
   ⌘S does not overwrite files Lightroom thinks changed externally.
2. You write and verify every sidecar against a fresh copy.
3. Read: **All Photographs** (or the folders), ★3+ filter, **Master Photos** (the first of the three Kind icons at
   the right of the Attribute row), check the count you give them, ⌘A, Metadata › **Read Metadata from Files**.
   Lightroom notices changed files lazily, so the Changed on disk filter undercounts; read by folder/all once
   you've verified everything else matches.
4. To re-export only what changed, put `xmp:Label="Red"` on those photos in the same write; they filter Red.

## What's automated, and how well (ICN Nationals, Canon R5 + R5 C)

| Step | How | Measured |
| --- | --- | --- |
| Cull → ratings | `compkit show cull`, then `write_ratings` (★3 pick, ★1 alternate, −1 reject) before import | ~95% of picks kept by the person |
| Base white balance | Person's preference: 3,800–4,000 K, tint +30 (they went cooler than their first 4,425 K) | — |
| Coloured stage light (PRO show, harsh blue) | Frame blueness = mean Lab b* of the camera's embedded preview (camera WB was fixed at 4,500 K, so the cast shows). Fit their own hand-set temperatures: T = −40.2·b* + 3,742 (Day 2 −444); only frames with b* < −40; clamp 4,300–6,800 | 377 K typical error vs 436 K for one fixed value; the level shift matters, per-frame variation is mostly unpredictable. Auto WB was rejected ("kills things") |
| Crops | Vision people boxes (`comp-render previews` 1024 px + `comp-render subject`), largest person = athlete; square in frame fractions (keeps 3:2), top margin 0.17× athlete height, bottom 0.07×; portrait frames only; skip if the crop would keep ≥97% | Person approved on a sheet; exact match only ~⅓ of the time, size is the main difference |
| Straightening | Canon `RollAngle` (ExifTool, 0.1°): tilt = Roll+90 (orient 8) or Roll (orient 1). `PerspectiveRotate` = 0.654 × tilt (their own ratio; +ve = clockwise), `PerspectiveUpright=0`, crop shrunk until all four corners sit inside the rotated frame | corr 0.94 with their manual levelling. **Sony a7R V records no tilt** |
| Exposure, dehaze, blacks | Not automatable: per-frame exposure ±0.27 stops at best | Leave to their eye |

Upright Auto is unreliable on stage photos: >1.5° off the camera's tilt on 15%, and it is computed only when a photo
is rendered, sometimes first at export (one turned 54°). Never leave Upright Auto on uncropped photos; don't batch
it with Sync. A Sync with White Balance ticked by accident once spread 3,841 K over 5,470 photos.

## Corner check before upload

Scan the exported JPEGs (~1,200 px): a real blank corner is a pure-white (≥252) run along *both* edges from the
corner joined by a straight edge (white just inside, image just outside). Bright LED screens and logos produce
hundreds of false hits with simpler "white corner" tests; zoom into anything small before calling it real.

## Sorting the export into the delivery

Lightroom can't route photos to per-division folders in one export. Have them export flat with original names, then
match each JPEG to its RAW by **camera model + capture time to 1/100 s** (file numbers repeat between cameras and
days; Lightroom adds "-2"), move into `<gallery>/<Division>/` and rename
`<Show> - <Division> - <dd-mm-yy> - Fit Focus Media<number>.jpg` (same number twice in a division → ` (2)`), strip
characters Windows rejects (`:`), write a manifest and an undo script. Replacing re-exports later: keep the old file
in a backup folder outside the gallery, verify the new file's embedded `crs:ColorTemperature` / settings first.
