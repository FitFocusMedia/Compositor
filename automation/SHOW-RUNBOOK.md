# Show runbook: from the card to the delivery

The first real use of the pipeline: you do the show your normal way in Lightroom, while the pipeline runs on a
**copy** of the card. Nothing it does touches your card, your Lightroom catalog or your shoot folders. Afterwards it
compares its picks and grades with what you delivered, and learns from the difference.

Steps in order, with the time each took on a 3,003-frame stand-in show (1,202 picks at 40%, on this M2 Max), and the
points where you review and approve (**★**).

## A hands-off show day

Set it up once:

```bash
compkit show watch --install      # the card watcher, and the dashboard at http://localhost:8765
```

The first time a card is copied, macOS may ask to let Python read files on a removable volume: allow it (or in
System Settings › Privacy & Security › Files and Folders). `compkit show watch --uninstall` turns both off.

**In the morning**, connect the show drive, open http://localhost:8765 and fill in "Start today's show" (drive,
show name, type, date, and file names if this show's differ), or:

```bash
compkit show start --drive /Volumes/SHOWS --name "Spring Classic" --profile competition
```

**Through the show**, insert cards as they fill. For each card:
1. **Copy:** every new photo is copied to the show drive (`<drive>/Shows/<date> <name>/card/`) and read back from
   it to check it. A notification says when the card can come out. Your Lightroom import from the same card isn't
   affected; the card is only read. Copying runs at the card reader's speed, about 4 minutes per 64 GB at 250 MB/s.
2. **Run:** the show runs on its own. The new frames are culled; frames already decided never change when a later
   card arrives. On the first card the setup is approved with column B, the show base. Then grading, cropping
   (when it's on for the show type), and export of Full Res and Web 2048.

A card that arrives while it's busy is taken in at once and processed when the current pass ends.

**★ The dashboard** (http://localhost:8765) is where you check and change things, at any time:

| Tab | What it's for |
| --- | --- |
| **Cull** | Every frame by moment, picks outlined green and rejects red. Click a frame, then **P** pick, **A** alternate, **X** reject, **R** back to automatic. **Enter** shows it large; the arrow keys move between frames (←→) and moments (↑↓). "Re-dial" sets a new keep rate. |
| **Graded & crops** | The delivered look of every pick. Click one to crop (the frame keeps the photo's shape) or level it. |
| **Setup** | The candidate sheet and what's approved. Approve another column, for the show or per row. |
| **Report** | The comparison with your Lightroom delivery, and learning from it. |
| **Cards & log** | Cards taken in, failures, and the next step. |

Every change is saved at once:
- A frame you pick is graded and delivered.
- A frame you un-pick has its delivered files removed.
- A new crop or setting is graded and delivered again.

**At the end**, click "Finish the day" (or run `compkit show finish`), so cards inserted afterwards aren't taken
in.

**Afterwards**, when your Lightroom delivery is done, use the Report tab or step 7 below to compare, then learn.

**Automatic crops are off** for every show type. On the posing workshop, cropping in around the people matched your
own crops no better than leaving the whole frame, and it cropped between 9 and 47 frames you left alone, depending
on how cautious it was set.
Vision's horizon misread stage and room lines as tilts on 78 of 170 frames. Crop by hand in the dashboard. To try
automatic crops for a show type anyway:

```bash
compkit show profiles --set competition auto_crop=true
```

## Step by step

These are the same steps the automation runs, for running a show by hand or redoing one step.

## Before the show (once)

```bash
~/Documents/"Claude Projects"/Compositor/automation/install.sh
compkit show profiles
```

`compkit show profiles` should list `competition`, `fight-night` and `workshop` and your defaults: God Tones, the
a7R V look, and your delivery spec (Full Res and Web 2048 capped at 2,000 KB, your file naming such as
"{show} - {date} - {number}", `{size}/{hour}` folders).

Room on the drive the show folder is on: about 200 GB for a 3,000-frame card copy (66 MB per a7R V RAW), plus about
14 GB for the pipeline's own files at 1,200 picks (see the table at the end).

## After the show

### 1. Copy the card

Import into Lightroom as you always do. Separately, copy the card for the pipeline. Pick a show folder outside
your Lightroom folders:

```bash
mkdir -p ~/Shows/"2026-10-11 Spring Classic"
ditto /Volumes/Untitled/DCIM ~/Shows/"2026-10-11 Spring Classic"/card
```

With two cards or two cameras, copy each into its own subfolder of `card/`. Their clocks should agree.

### 2. Start the show

```bash
compkit show new ~/Shows/"2026-10-11 Spring Classic" --card ~/Shows/"2026-10-11 Spring Classic"/card \
  --profile competition --name "Spring Classic"
```

- `--profile` is `competition`, `fight-night` or `workshop`. It sets the keep rate (40%, 20% and 40% to start) and
  how frames are grouped and picked.
- `--naming "…"` changes this show's file names only, e.g. `--naming "{show} {seq}"`. The fields are `show`,
  `date`, `number` (the camera's file number), `stem`, `seq` (capture order) and `hour`.
- `--date 2026-10-11` sets the date in the file names. By default it comes from the first frame.

From here on, `SHOW` stands for the show folder.

### 3. Cull: about 1 minute

```bash
compkit show cull SHOW
```

**★ Review 1: the picks.** Open `SHOW/cull/sheets/` (`open SHOW/cull/sheets`).
- Each row is one moment (a burst or a held pose), labeled "2 of 7" for picks out of frames.
- Picks are outlined green, and rejects red with their reason.

To see more or fewer, re-dial from the same scores (3 seconds, contact sheets included; no frame is read again):

```bash
compkit show pick SHOW --rate 35%
compkit show pick SHOW --count 1000
```

Bigger moments get more frames, and every moment gets one before any gets a second. Rejected frames (missed
focus, eyes shut, far off exposure) are never picked.

### 4. Setup: white balance and exposure, about 10 seconds

```bash
compkit show setup SHOW
open SHOW/setup/setup-sheet.jpg
```

**★ Review 2: approve the settings.** The sheet has 5 frames spread over the show's time and lighting (rows), each
graded with candidate settings (columns):

| Column | Settings |
| --- | --- |
| A | The camera's own white balance |
| B | The show base: the profile's settings from your last delivered show of this type, else the camera's median white balance |
| C, D | B made 300 K warmer or cooler |
| E, F | B made 0.3 EV brighter or darker |

Approve one column for the whole show:

```bash
compkit show setup SHOW --approve B
compkit show setup SHOW --approve B --set raw.exposure=-0.05      # B with a tweak
```

Or, when the lighting changes during the show, approve one column per row. Each frame then takes the settings of the
row nearest it in time and lighting:

```bash
compkit show setup SHOW --approve 1=B,2=B,3=C,4=B,5=C
```

To see other settings first, add columns and look again:

```bash
compkit show setup SHOW --try raw.temperature=4400 --try raw.temperature=4400,raw.exposure=0.1
```

The approved settings are saved in `SHOW/show.json`. Grading, re-grading and any later run use them.

### 5. Grade: about 15 minutes for 1,200 picks

```bash
compkit show grade SHOW
```

Each pick becomes `SHOW/graded/<name>.comp` (tunable in Compositor) plus a 2048 px JPEG.

**★ Review 3 (optional): the graded set.** Run `compkit sheet SHOW/graded/*.jpg -o SHOW/graded/_sheet.jpg`. Tune
any frame in Compositor (its Tune layers), and export picks up the change.

If you change the approved settings later, run `grade` again. Only the photos whose settings changed are graded
again.

### 6. Export the delivery

```bash
compkit show export SHOW --size "Web 2048"     # about 3–4 minutes for 1,200 picks
compkit show export SHOW                       # adds Full Res: about 2 hours for 1,200 picks
```

- **Output:** files go to `SHOW/delivery/Spring Classic/Full Res/19.00/Spring Classic - 11-10-26 - 07763.jpg`
  (named by your own template). Web 2048 files go in the matching place, and a show running past midnight gets dated hour
  folders.
- **Size cap:** each file is the highest quality under 2,000 KB, as Lightroom's "Limit File Size" does, and keeps
  the camera's metadata.
- **Full res:** each full-resolution file is graded again from its RAW at 61 MP, which is the slow part.
  `--jobs 3` is a little faster if nothing else heavy is open. Each job uses about 6 GB.
- **Picks that change:** after a re-dial, `--prune` removes delivered files whose frames are no longer picks.

### 7. Compare with your delivery

Once your Lightroom delivery is done, run this against the folder of your exported finals:

```bash
compkit show compare SHOW --delivered "/path/to/your/Lightroom exports"
```

It writes `SHOW/report/compare.md` with these sections:
- **Picks:** moments covered, your delivered frames it rejected, your delivered frames it picked, and picks per
  moment beside yours by moment size, both as run and at your delivered count.
- **Grades:** coarse ΔE between its graded renders and your finals, split by frames where you kept the show
  settings and frames you tuned. Your crops are applied to its renders first, and straightened frames are left
  out.
- **Settings:** the approved settings beside the median of yours.

Delivered files are matched by the RAW name Lightroom embeds, else by capture time, else by file number.

**★ Review 4: learn from it.** Read the report. If the show is typical of its type, update the profile:

```bash
compkit show compare SHOW --delivered "/path/to/your/Lightroom exports" --learn
```

That sets the profile's keep rate to the average over the shows it has learned from, refits its per-moment rule to
match what you delivered, and makes your settings the next show's column B. `compkit show profiles` shows the
result.

## Anytime

- `compkit show status SHOW`: what's done, disk used, failures logged, and the next command.
- **Stopping:** Ctrl-C (or a crash, or the Mac sleeping) stops a step safely. Run the same command again and it
  carries on, skipping finished work.
- **Failures:** a file that fails is logged in `SHOW/logs/failures.csv` and the step carries on. Each step's log is
  `SHOW/logs/<step>.log`.
- **Read-only cards:** no step writes into the card copy. Lightroom star ratings are written only by `compkit cull
  … --ratings`, and only when asked.

## Times and disk at 3,000 frames

Measured on 3,003 frames, 7 renumbered copies of a 429-frame a7R V shoot moved 2 hours apart (plus two damaged
files), with 1,202 picks at 40%:

| Step | Time | Peak memory | Disk |
| --- | --- | --- | --- |
| cull | 1 min (stopped with Ctrl-C at 20 s, then resumed) | 0.8 GB | 87 MB |
| pick (re-dial) | 3 s | 0.5 GB | |
| setup sheet | 7 s | 1.3 GB | 17 MB |
| grade | 14 min (0.71 s per pick; stopped at 5 min, then resumed) | 2.5 GB | 10 GB (8.5 MB per pick) |
| export Web 2048 | 3.4 min | 0.7 GB | 0.95 GB (0.79 MB per file) |
| export Full Res, 3 jobs | about 115 min (5.7 s per pick; killed outright at 10 min, then resumed) | 12.6 GB | 2.4 GB (2.0 MB per file) |
| **total** | **about 2 h 15 min** | | **about 14 GB**, plus the card copy |

- **The full-resolution row** was measured over 645 of the 1,202 picks: 10 minutes before the kill and 52 minutes
  after the resume, at the same rate throughout. The total is extrapolated from that.
- **Jobs:** 2 jobs ran at 7.6 s per pick (about 2.5 h for 1,200), and 4 jobs at 5.5 s with only 5 GB left free.
- **Over the cap:** 2% of full-resolution files (very detailed frames) came out at 2.08–2.18 MB even at the lowest
  JPEG quality. The export logs them, and Lightroom's own 2,000 KB limit let workshop finals reach 2.26 MB.
- **The grade at 2048 px** is what you review and tune. Full-resolution projects would take about 200 MB each (240
  GB for a show), which is why the full-resolution files are made from the RAW at export instead.
- **Damaged files:** an empty RAW was rejected and logged without stopping the cull. A RAW truncated by a bad copy
  still has its preview, so the cull can't tell. If such a frame is picked, grading it fails and is logged.
