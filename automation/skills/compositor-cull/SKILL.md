---
name: compositor-cull
description: Cull and process a whole shoot — hundreds or thousands of RAWs from a show, competition, fight night or event — down to the frames worth editing, then grade and export them with the user's Lightroom base grade. Use whenever the user has a folder/card of photos to sort, wants the best shots picked, bursts and duplicates collapsed, blurry or eyes-closed frames thrown out, picks rated for Lightroom, or a show taken from RAW to delivered images automatically — even if they just say "go through today's shoot" or "pick the best ones".
---

# From a shoot to the frames worth delivering

The pipeline, all on this Mac, from the embedded previews (no RAW is decoded until grading):

1. **Cull** (`compkit cull`): about 15 s per 1,000 frames.
   - Groups frames into **moments**: a burst or a held pose is consecutive frames within 2 s of each other that
     look alike.
   - **Flags** what is clearly wrong: nothing in focus anywhere, the main face's eyes shut, a frame far darker or
     brighter than the rest of the shoot.
   - **Scores** every frame 0–100 within the shoot: sharpness of the subject in focus, face quality, eyes open,
     Apple's aesthetic score, exposure.
   - **Picks** the best unflagged frame per moment. The rest of the moment are alternates; flagged frames are
     rejects.
2. **Grade** the picks (`compkit grade --list picks.txt`, compositor-grade skill): the person's Lightroom preset at
   the RAW stage, an optional learned Lightroom match, editable Tune layers, and a JPEG for each.
3. **Review and deliver.** The person checks the contact sheets, swaps any pick for an alternate, then grades or
   exports.

Commands come from the toolkit (if missing, use the compositor-toolkit skill).

## 1. Cull

```bash
compkit cull "/Volumes/Card/DCIM" --out ~/Shows/2026-10-11/cull            # folders are searched recursively
compkit cull shoot/ --out cull/ --keep 300 --per-moment 2 --ratings
```

| Option | Use |
| --- | --- |
| `--keep N` | Cap the picks at the N best-scoring moments (a delivery count). |
| `--per-moment K` | Keep K frames per moment. Clients often want options from a pose: on the test shoot the photographer kept 1 to 3 from a burst. |
| `--gap S`, `--distance D` | How far apart in time (default 2 s) and how alike (default 0.6) frames of one moment are. Raise the gap for slow, posed sessions; lower it for fast action. |
| `--ratings` | Also write Lightroom star ratings as `.xmp` sidecars beside the RAWs: picks 3★, alternates 1★, rejects flagged rejected. It never overwrites an existing sidecar (that may hold edits). It writes into the person's shoot folder, so only use it when asked. |

Output in `--out`:
- `picks.txt`: the paths of the picks, ready for `compkit grade --list`.
- `cull.csv`: every frame with its status, score, moment and flags.
- `cull.json`: the same with every measurement.
- `sheets/moments-NNN.jpg`: one row per moment, the pick outlined green, rejects red with their reason.

Look at the sheets with the Read tool before grading, and tell the person how many frames, moments, picks and
rejects there were. Point them at the sheets to swap picks. To change a pick, edit `picks.txt` (paths come from
`cull.csv`).

What to say about its judgment, plainly. Tested against a photographer's own picks on a 429-frame shoot:
- **Moments:** it chose a frame in 124 of the 125 moments the photographer used.
- **Wrongly rejected:** 1 of their 170 keepers.
- **Which frame within a burst:** no better than chance. That comes down to expression and pose, so it's the
  person's call, with alternates beside every pick.

A sports or fight-night shoot will have far more real misses (motion blur, missed focus, eyes shut, bad timing),
and that's where rejects save the most time.

## 2. Grade the picks

```bash
compkit grade --list cull/picks.txt --preset ~/Documents/Presets/"God Tones.xmp" \
  --match ~/Documents/Presets/"God Tones (a7R V).cube" \
  --set raw.temperature=4170 --set raw.exposure=-0.14 \
  --out graded/ --render --jobs 6
```

- **Per-show settings** come as `--set raw.*` on top of the preset, as the person does in Lightroom by syncing: the
  venue's white balance (`raw.temperature`, `raw.tint`), a show-wide exposure (`raw.exposure`) and highlights
  (`raw.highlights`).
  - Ask for them, or settle them on one representative frame first: grade it alone, look, adjust, then run the
    batch.
- **`--match`** applies a look learned from the person's own Lightroom exports (`compkit learn-look`, see
  compositor-grade). Use it when one exists for this camera and preset.
- **`--jobs`** runs several photos at once. On the test shoot, 165 picks from a 61 MP a7R V took 147 s with
  `--jobs 6` (about 0.9 s per photo overall) at the default 2048 px project size.
- **Output:** each pick becomes `graded/<name>.comp` (hidden original, base grade, Tune layers, plus a sources
  file so a re-grade goes back to the RAW) and `graded/<name>.jpg`.
- **Check the result:** make a contact sheet with `compkit sheet graded/*.jpg -o graded/_sheet.jpg` and look at
  it. Use `compkit measure` for brightness and color consistency across the set.
- **How close hands-off gets:** on the test shoot, picks graded with one show-wide setting came within coarse
  ΔE 3.4 of the photographer's Lightroom finals where they had used the same setting, and 4.5 where they had
  tuned that frame. Per-photo tuning (Tune layers, `regrade`) is what closes that last gap.

## Scale notes

- **Speed** (measured on a 429-frame a7R V shoot, M2 Max):
  - Cull: 6 s, about 15 s per 1,000 frames.
  - Contact-sheet thumbnails: about 4 s per 1,000 frames.
  - Grading and rendering: 147 s for 165 picks with `--jobs 6`.
  - For 3,000 frames and 400 picks, expect under a minute to cull and about 6 minutes to grade.
- **Disk:** a graded project is about 8–10 MB at 2048 px (1.4 GB for 165). Put `graded/` on a drive with room. The RAWs are only
  read, never moved or changed (except `--ratings` sidecars, when asked).
- **Several cards or cameras:** pass every folder to one `cull`. Frames are ordered by capture time, so the
  cameras' clocks should agree. Check `cull.csv`'s capture times if moments look split or merged.
