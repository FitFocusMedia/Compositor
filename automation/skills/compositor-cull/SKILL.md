---
name: compositor-cull
description: Cull and process a whole shoot — hundreds or thousands of RAWs from a show, competition, fight night or event — down to the frames worth editing, then grade and export them with the user's Lightroom base grade. Use whenever the user has a folder/card of photos to sort, wants the best shots picked, bursts and duplicates collapsed, blurry or eyes-closed frames thrown out, a keep rate changed ("show me 35%", "give me 1,000"), a show's white balance settled, picks rated for Lightroom, a delivery exported to their spec, the pipeline compared with what they delivered, or a show taken from RAW to delivered images automatically — even if they just say "go through today's shoot" or "pick the best ones".
---

# From a card to a delivered show

A show is a folder that each step fills in turn, all on this Mac. The full walkthrough, with times at 3,000 frames
and the review points, is `SHOW-RUNBOOK.md` in the toolkit folder (`compkit where`). Read it before running a real
show. Commands come from the toolkit (if missing, use the compositor-toolkit skill).

```bash
compkit show new SHOW --card CARD-COPY --profile competition --name "Spring Classic"   # [--naming "…"] [--date …]
compkit show cull SHOW                    # ★ review SHOW/cull/sheets
compkit show pick SHOW --rate 35%         # re-dial in seconds (or --count 1000)
compkit show setup SHOW                   # ★ the person approves from SHOW/setup/setup-sheet.jpg …
compkit show setup SHOW --approve B       # … for the whole show, or --approve 1=B,2=C,… per lighting
compkit show grade SHOW
compkit show export SHOW                  # --size "Web 2048" for just the web set
compkit show compare SHOW --delivered THEIR-EXPORTS   # ★ then --learn to update the profile
compkit show status SHOW                  # what's done, disk, failures, the next command
```

## A hands-off show day

The person starts the day once (dashboard form, or `compkit show start --drive /Volumes/X --name "…" --profile
competition`).

From then on the card watcher (`compkit show watch --install`, a launchd agent) does the rest for every card:
- **Copy:** copies the card's new photos to the show drive and verifies each one.
- **Notify:** says when the card can come out.
- **Run:** runs the show (`compkit show run SHOW`): cull new frames only (earlier decisions never change), setup
  approved with column B on the first card, grade, export, and removal of un-picked deliveries.

The dashboard at http://localhost:8765 (`compkit dashboard`, served from the Mac only, so client photos never
leave it) is where they review:
- statuses (P/A/X/R keys);
- re-dial;
- crops and leveling;
- setup approval;
- the report.

The runner picks up every change. From the command line:
- `compkit show mark SHOW --pick NAME --reject NAME --auto NAME` changes statuses.
- `compkit show crop SHOW NAME --crop x,y,w,h --rotate deg` crops and levels (`--clear` undoes it).
- `compkit show status SHOW` reports where things are.

Installing the watcher creates standing launchd agents: only with the person's go-ahead.

**Automatic crops** (`framing.suggest`: crop in around people at the photo's shape, level a tilted horizon) are
off by default (`auto_crop`, per profile). On the person's workshop they matched their own crops no better than
the whole frame. Vision's horizon misreads stage and room lines indoors (78 of 170 frames "tilted", none really).
Say so if they ask for automatic crops; manual crops in the dashboard are dependable.

## Rules

- **Card and shoot folders are read-only.** Work from a copy of the card, and keep the show folder outside it
  (`show new` refuses one inside). Lightroom ratings (`compkit cull … --ratings`) write sidecars into the shoot
  folder, so only when asked.
- **Settings are the person's call.** Never approve a setup sheet, pick a keep rate, or `--learn` without them.
  Show them the sheet (Read the image) and ask. If they don't say a keep rate, the profile's applies.
- **Everything resumes.** If a step stops (Ctrl-C, crash, sleep), run the same command again. Failed files are in
  `SHOW/logs/failures.csv` and the rest carried on. Report them.

## Profiles and the keep rate

`compkit show profiles` lists them, with what each has learned. They're kept in `~/Documents/Presets/Show
Profiles/profiles.json` with the person's defaults: preset, look and delivery spec.

| Profile | Starting keep rate | Grouping | Notes |
| --- | --- | --- | --- |
| workshop | 40% | frames within 2 s that look alike | calibrated on the person's posing workshop |
| competition | 40% (varies by show) | as workshop | untested: learn from the first delivered show; the person keeps frames a crop or straighten can rescue |
| fight-night | 20% | within 1 s; a run of one scene splits every 4 s | untested: a fast-burst frame counts for less |

- **How picks are spread:** each moment (a burst or a held pose) gets frames in proportion to its size, so long
  bursts and held poses get more. Every moment's first frame comes before any moment's second, and flagged frames
  (missed focus, eyes shut, far off exposure, unreadable) are never picked.
- **Re-dialing:** `compkit show pick SHOW --rate 35%` or `--count 1000` re-picks from the cull's scores and
  redraws the contact sheets in about a second. No frame is read again.

What to say about its judgment, plainly. Against the person's 170 delivered frames from their 429-frame posing
workshop:
- **Before** (one per moment): 165 picks, 124 of 125 moments covered, 1 keeper wrongly rejected, 71 keepers
  picked, picks per moment off by 0.52 on average.
- **After** (calibrated): 170 picks, 123/125 covered, 1 wrongly rejected, 76 picked, off by 0.44.
- **On held-out halves:** the gain was smaller but held.
- **Which frame within a moment:** no better than chance. That's taste (expression, pose), so alternates sit beside
  every pick on the sheets.

## Setup: white balance and exposure

`compkit show setup SHOW` grades 5 representative frames (spread over the show's time and lighting) with candidate
settings, on one labeled sheet:
- **A:** the camera's own white balance.
- **B:** the show base, which is the profile's settings from the last delivered show of that type, else the
  camera's median.
- **C/D:** 300 K warmer or cooler.
- **E/F:** 0.3 EV brighter or darker.
- **More columns:** `--try FIELD=VALUE[,…]` adds one.

Read the sheet and show it to the person. Approving saves the settings in `SHOW/show.json`, and grade, regrade and
later runs use them:
- `--approve B` for the whole show.
- `--approve 1=B,2=B,3=C,…` when the lighting changes, so each frame takes the settings of the nearest row.
- `--set FIELD=VALUE` adds a tweak.

## Delivery

The delivery spec lives in profiles.json (`compkit show defaults` changes it). This person's spec:
- **Sizes:** Full Res and Web 2048, each the highest JPEG quality under 2,000 KB (as Lightroom's "Limit File
  Size").
- **Naming:** their own template with the show, the date (as 11-10-26) and the camera's file number;
  `compkit show profiles` shows it.
- **Folders:** `<show>/<size>/<hour>/`.
- **Per show:** `show new --naming "…"` changes one show's names.

Full-resolution files are graded again from the RAW at 61 MP, which is the slow part.

| On 1,200 picks | Time |
| --- | --- |
| Full Res, 3 jobs | about 2 hours (5.7 s each) |
| Web 2048 alone | 3–4 minutes |
| Grading | 14 minutes |
| Cull | about 1 minute |

Each full-resolution job takes about 6 GB of memory. `--jobs` sets how many run at once: use 2 while the person
works in Lightroom.

About 2% of full-resolution files, the very detailed frames, end up just over the cap even at the lowest quality
(up to 2.2 MB). They're logged, and Lightroom's own limit overshoots the same way.

## Compare and learn

After the person's own Lightroom delivery, `compkit show compare SHOW --delivered EXPORTS` writes
`SHOW/report/compare.md`:
- **Picks:** moment coverage, delivered frames it rejected or picked, and picks per moment by moment size, both as
  run and at their delivered count.
- **Grades:** coarse ΔE of its graded renders against their finals, split into frames that kept the show settings
  and frames they tuned.
- **Settings:** the approved settings beside the median of theirs.

Finals are matched by the RAW name Lightroom embeds, else by capture time, else by file number.

`--learn` updates that profile from it:
- **Keep rate:** the average over the shows learned from.
- **Per-moment rule:** refitted to what was delivered.
- **Setup:** their settings become the next show's column B.

Do it only when the person agrees the show was typical.

## Without a show folder

```bash
compkit cull "/Volumes/Card/DCIM" --out cull/ --profile workshop     # or --rate 40% / --count 900 / --per-moment 2
compkit pick cull/ --rate 35%
compkit grade --list cull/picks.txt --preset ~/Documents/Presets/"God Tones.xmp" \
  --match ~/Documents/Presets/"God Tones (a7R V).cube" --set raw.temperature=4170 --out graded/ --render --jobs 6
```

The cull's output in `--out`:
- `picks.txt`, `cull.csv` (every frame: status, score, moment, flags) and `cull.json` (every measurement).
- `settings.json`.
- `sheets/moments-NNN.jpg`: each moment one row, labeled "2 of 7", picks outlined green, rejects red with the
  reason.

To swap a pick, edit `picks.txt`. Paths are in `cull.csv`.
