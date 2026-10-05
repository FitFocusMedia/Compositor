"""A show, from the card to delivered JPEGs: cull → setup (approve white balance and exposure) → grade → export,
then compare with what the photographer delivered and learn from it.

A show is a folder:

    SHOW/
      show.json      name, date, profile, the card folders, preset, look, approved settings, delivery spec
      cull/          cull.csv, cull.json, picks.txt, settings.json, sheets/ (and .cache/, the scores)
      setup/         setup.json and setup-sheet.jpg: the frames and candidate settings to approve
      graded/        one tunable project, JPEG and sources file per pick
      delivery/      the delivered JPEGs, by the delivery spec (unless it names another folder)
      logs/          <step>.log and failures.csv
      report/        compare.md and compare.json

Every step can be stopped and run again: finished work is kept and skipped (a cull's scores, graded projects whose
settings haven't changed, exports newer than their project). A file that fails is logged in logs/failures.csv and
the step goes on.

Show profiles (competition, fight-night, workshop) set how a show is culled: the keep rate, how frames group into
moments, and the per-moment rule (see cull.assign). Their learned values and the delivery spec live in
~/Documents/Presets/Show Profiles/profiles.json (COMPKIT_PROFILES overrides the folder), outside the toolkit, and
`compare --learn` updates them from what was delivered.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import (COMP_RENDER, CompError, Project, Stopped, _lab, analyze, check_comp_render, comp_render, develop,
               lightroom_crop, lightroom_settings)
from .cull import (DEFAULT_RULE, _run_listed, assign, frames_from, gather, group, judge, load_results, moments_of,
                   scan, write_results)
from .runlog import Progress

# Profiles ----------------------------------------------------------------------------------------------------------

BUILT_IN = {
    "workshop": {
        "description": "posed sessions and classes: held poses, an unhurried pace",
        "rate": 0.40, "gap": 2.0, "distance": 0.6, "max_moment": None, "rule": dict(DEFAULT_RULE),
        "auto_crop": False,
    },
    # Not yet tested against a delivered competition: the workshop's rule (where frames shot faster than 0.3 s apart,
    # a burst through a pose, count for less). Learned from the first delivered show.
    "competition": {
        "description": "physique and fitness stages: posing rounds and held mandatories, frames that a crop can rescue",
        "rate": 0.40, "gap": 2.0, "distance": 0.6, "max_moment": None, "rule": dict(DEFAULT_RULE),
        "auto_crop": False,
    },
    # Not yet tested against a delivered fight night: a burst frame counts for a fraction of a held-pose frame, a run
    # of the same scene splits every 4 s so one round isn't one moment, and a better score counts a little across
    # moments (sharpness and faces matter more in action). Learned from the first delivered show.
    "fight-night": {
        "description": "combat sports: fast action in long bursts, many near-duplicates",
        "rate": 0.20, "gap": 1.0, "distance": 0.6, "max_moment": 4.0, "rule": {"tau": 0.5, "cover": 1.0, "beta": 0.3},
        "auto_crop": False,
    },
}
DELIVERY = {
    "naming": "{show} - {date} - {number}",
    "date_format": "%d-%m-%y",
    "folders": "{size}/{hour}",
    "sizes": [{"name": "Full Res", "max_side": None, "max_kb": 2000},
              {"name": "Web 2048", "max_side": 2048, "max_kb": 2000}],
    "quality": 0.92,
}
SETUP_FIELDS = ("raw.temperature", "raw.tint", "raw.exposure", "raw.highlights")


def profiles_home() -> Path:
    return Path(os.environ.get("COMPKIT_PROFILES") or "~/Documents/Presets/Show Profiles").expanduser()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2, default=str))
    os.replace(temporary, path)


def saved_profiles() -> dict:
    return _read_json(profiles_home() / "profiles.json", {"format": "compkit.profiles", "version": 1, "profiles": {}})


def profile(name: str) -> dict:
    """A profile's settings: the built-in defaults with what was saved or learned on top."""
    saved = saved_profiles().get("profiles", {}).get(name)
    if name not in BUILT_IN and saved is None:
        known = sorted(set(BUILT_IN) | set(saved_profiles().get("profiles", {})))
        raise CompError(f"no show profile {name!r}; known: {', '.join(known)}")
    merged = copy.deepcopy(BUILT_IN.get(name, {"rate": 0.4, "gap": 2.0, "distance": 0.6, "max_moment": None,
                                               "rule": dict(DEFAULT_RULE)}))
    for key, value in (saved or {}).items():
        merged[key] = {**merged.get(key, {}), **value} if key == "rule" else value
    merged["name"] = name
    return merged


def save_profile(name: str, changes: dict) -> None:
    data = saved_profiles()
    data.setdefault("profiles", {}).setdefault(name, {}).update(changes)
    _write_json(profiles_home() / "profiles.json", data)


def defaults() -> dict:
    """The photographer's own defaults kept with the profiles: preset, look and delivery spec."""
    data = saved_profiles()
    return {"preset": data.get("preset"), "match": data.get("match"),
            "delivery": {**DELIVERY, **data.get("delivery", {})}}


# A show -------------------------------------------------------------------------------------------------------------

class Show:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().resolve()
        path = self.folder / "show.json"
        if not path.exists():
            raise CompError(f"{self.folder} isn't a show (no show.json); start one with `compkit show new`")
        self.data = json.loads(path.read_text())

    @classmethod
    def create(cls, folder, *, cards, profile_name: str, name: str, date: str | None = None, preset=None, match=None,
               naming: str | None = None, delivery_folder=None) -> "Show":
        folder = Path(folder).expanduser().resolve()
        if (folder / "show.json").exists():
            raise CompError(f"{folder} is already a show; its steps pick up where they stopped")
        profile(profile_name)  # an unknown profile fails here
        own = defaults()
        preset = preset or own["preset"]
        if not preset:
            raise CompError("give the base grade with --preset (or save one as the default in profiles.json)")
        cards = [str(Path(c).expanduser().resolve()) for c in cards]
        for card in cards:
            if not Path(card).exists():
                raise CompError(f"{card} doesn't exist")
            if folder == Path(card) or Path(card) in folder.parents:
                raise CompError("keep the show folder outside the card folders: the cards are only ever read")
        match = match if match is not None else own["match"]
        delivery = {}
        if naming:
            delivery["naming"] = naming
        if delivery_folder:
            delivery["folder"] = str(Path(delivery_folder).expanduser().resolve())
        data = {"format": "compkit.show", "version": 1, "name": name, "date": date, "profile": profile_name,
                "cards": cards, "preset": str(Path(preset).expanduser().resolve()),
                "match": str(Path(match).expanduser().resolve()) if match else None,
                "settings": None, "delivery": delivery, "created": _now()}
        for path in (data["preset"], data["match"]):
            if path and not Path(path).exists():
                raise CompError(f"{path} doesn't exist")
        folder.mkdir(parents=True, exist_ok=True)
        _write_json(folder / "show.json", data)
        return cls(folder)

    def save(self) -> None:
        _write_json(self.folder / "show.json", self.data)

    @property
    def cull_folder(self) -> Path: return self.folder / "cull"
    @property
    def graded_folder(self) -> Path: return self.folder / "graded"
    @property
    def logs(self) -> Path: return self.folder / "logs"

    @property
    def profile(self) -> dict:
        return profile(self.data["profile"])

    @property
    def delivery(self) -> dict:
        return {**defaults()["delivery"], **self.data.get("delivery", {})}

    @property
    def delivery_root(self) -> Path:
        folder = self.delivery.get("folder")
        return Path(folder) if folder else self.folder / "delivery"

    def frames(self) -> list[dict]:
        return load_results(self.cull_folder)[0]

    def picks(self) -> list[dict]:
        return [f for f in self.frames() if f["status"] == "pick"]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# Cull and re-dial ---------------------------------------------------------------------------------------------------

def cull_show(show: Show, *, rate: float | None = None, count: int | None = None, sheets: bool = True) -> dict:
    """Culls the show's cards with its profile (resumable: scores are cached in cull/.cache). Run again (say, with a
    card added), it keeps the keep rate or count the show was last dialed to unless given another."""
    p = show.profile
    progress = Progress("cull", folder=show.logs, unit="frames")
    last = show.data.get("cull") or {}
    if count is not None:
        dial = {"count": count}
    elif rate is not None:
        dial = {"rate": rate}
    else:
        dial = next(({key: last[key]} for key in ("count", "rate", "per_moment") if last.get(key) is not None),
                    {"rate": p["rate"]})
    paths = gather(show.data["cards"])
    if not paths:
        raise CompError("no pictures on the show's cards yet")
    frames = frames_from(scan(paths, show.cull_folder / ".cache", progress=progress))
    group(frames, gap=p["gap"], distance=p["distance"], max_moment=p.get("max_moment"))
    judge(frames)
    decide(show.cull_folder, frames, rule=p["rule"], dial=dial, again=count is not None or rate is not None)
    progress.finish()
    settings = {"profile": p["name"], "gap": p["gap"], "distance": p["distance"], "max_moment": p.get("max_moment"),
                "rule": p["rule"], **dial}
    summary = write_results(frames, show.cull_folder, sheets=sheets, settings=settings)
    if not show.data.get("date") and frames and frames[0].get("captured"):
        show.data["date"] = frames[0]["captured"][:10]
    show.data["cull"] = {**{k: summary[k] for k in ("frames", "moments", "picks", "rate", "rejects")}, "at": _now(), **dial}
    show.save()
    return summary


def decide(cull_folder, frames: list[dict], *, rule: dict, dial: dict, again: bool = False) -> None:
    """Sets every frame's status, keeping earlier decisions: frames picked or passed over by an earlier run keep
    their status (so a card that arrives later never changes what was already graded and delivered), and only new
    frames are picked, at the dial's rate. `again` (a new rate or count) decides every frame afresh. Statuses set by
    hand (set_status, the dashboard) always win. Kept in cull/decisions.json."""
    path = Path(cull_folder) / "decisions.json"
    decisions = _read_json(path, {"auto": {}, "manual": {}})
    earlier = {} if again else decisions["auto"]
    old = [f for f in frames if f["path"] in earlier]
    new = [f for f in frames if f["path"] not in earlier]
    for frame in old:
        frame["status"] = earlier[frame["path"]]
    if new:
        if old and "per_moment" not in dial:
            rate = dial.get("rate") or sum(f["status"] == "pick" for f in old) / len(old)
            assign(new, rate=rate, rule=rule)
        else:
            assign(new, rule=rule, **dial)
    decisions["auto"] = {f["path"]: f["status"] for f in frames}
    for frame in frames:
        frame["status"] = decisions["manual"].get(frame["path"], frame["status"])
    Path(cull_folder).mkdir(parents=True, exist_ok=True)
    _write_json(path, decisions)


def set_status(cull_folder, changes: dict) -> dict:
    """Statuses set by hand ({path: "pick" | "alternate" | "reject"}; None goes back to the automatic one), applied
    at once to picks.txt, cull.csv and cull.json and kept through later culls and cards."""
    path = Path(cull_folder) / "decisions.json"
    decisions = _read_json(path, {"auto": {}, "manual": {}})
    frames, settings = load_results(cull_folder)
    known = {f["path"] for f in frames}
    for frame_path, status in changes.items():
        if frame_path not in known:
            raise CompError(f"{frame_path} isn't in this cull")
        if status is None:
            decisions["manual"].pop(frame_path, None)
        elif status in ("pick", "alternate", "reject"):
            decisions["manual"][frame_path] = status
        else:
            raise CompError(f"a status is pick, alternate or reject, not {status!r}")
    for frame in frames:
        frame["status"] = decisions["manual"].get(frame["path"], decisions["auto"].get(frame["path"], frame["status"]))
    _write_json(path, decisions)
    return write_results(frames, cull_folder, sheets=False, settings=settings)


def redial(cull_folder, *, rate: float | None = None, count: int | None = None, per_moment: int | None = None,
           rule: dict | None = None, sheets: bool = True) -> dict:
    """Picks again from a finished cull's scores ("show me 35%", "show me 1,000"): no frame is read again, and
    picks.txt, cull.csv and the contact sheets are rewritten. Grouping and flags stay as the cull made them."""
    frames, settings = load_results(cull_folder)
    rule = {**DEFAULT_RULE, **settings.get("rule", {}), **(rule or {})}
    if per_moment is not None:
        dial = {"per_moment": per_moment}
    elif count is not None:
        dial = {"count": count}
    elif rate is not None:
        dial = {"rate": rate}
    else:
        raise CompError("say how many to pick: a rate (35%) or a count (1000)")
    used = assign(frames, rule=rule, **dial)
    decide(cull_folder, frames, rule=rule, dial=dial, again=True)
    for key in ("rate", "count", "per_moment", "keep"):
        settings.pop(key, None)
    settings.update(dial)
    settings["rule"] = rule
    summary = write_results(frames, cull_folder, sheets=sheets, settings=settings)
    return {**summary, "dial": dial, "rule": used.get("rule")}


def pick_show(show: Show, **dial) -> dict:
    summary = redial(show.cull_folder, rule=show.profile["rule"], **dial)
    show.data["cull"] = {**show.data.get("cull", {}), **{k: summary[k] for k in ("frames", "moments", "picks", "rate", "rejects")},
                         "at": _now(), **summary["dial"]}
    for key in ("rate", "count", "per_moment"):
        if key not in summary["dial"]:
            show.data["cull"].pop(key, None)
    show.save()
    return summary


# Setup: white balance and exposure, approved on a contact sheet -----------------------------------------------------

LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _seconds(frame) -> float | None:
    return dt.datetime.strptime(frame["captured"], "%Y-%m-%d %H:%M:%S.%f").timestamp() if frame.get("captured") else None


def representative(frames: list[dict], count: int = 5) -> list[dict]:
    """`count` good frames spread across the show's time and lighting: farthest-point sampling over capture time (as
    a share of the show) and scene brightness (EV, a lighting change of 1.5 stops weighing like the whole show's
    span), among the better-scoring picks."""
    pool = [f for f in frames if f["status"] == "pick" and _seconds(f) is not None] or \
        [f for f in frames if not f["flags"] and _seconds(f) is not None]
    if not pool:
        raise CompError("no readable frames with capture times to set the show up from")
    cutoff = float(np.percentile([f["score"] for f in pool], 40))
    pool = [f for f in pool if f["score"] >= cutoff] or pool
    times = np.array([_seconds(f) for f in pool])
    evs = np.array([f["ev"] if f.get("ev") is not None else np.nan for f in pool])
    evs = np.where(np.isnan(evs), np.nanmedian(evs) if not np.isnan(evs).all() else 0, evs)
    span = max(1.0, times.max() - times.min())
    points = np.stack([(times - times.min()) / span, (evs - np.median(evs)) / 1.5], 1)
    start = int(np.argmin(((points - np.median(points, 0)) ** 2).sum(1)))
    chosen = [start]
    nearest = ((points - points[start]) ** 2).sum(1)
    while len(chosen) < min(count, len(pool)):
        index = int(np.argmax(nearest))
        if nearest[index] <= 0:
            break
        chosen.append(index)
        nearest = np.minimum(nearest, ((points - points[index]) ** 2).sum(1))
    return sorted((pool[i] for i in chosen), key=lambda f: f["captured"])


AS_SHOT = {"raw.temperature": 0}


def _describe(settings: dict) -> str:
    parts = []
    if settings.get("raw.temperature", 1) <= 0:
        parts.append("camera white balance")
    elif "raw.temperature" in settings:
        parts.append(f"{settings['raw.temperature']:.0f} K")
    if "raw.tint" in settings:
        parts.append(f"tint {settings['raw.tint']:+.0f}")
    if "raw.exposure" in settings:
        parts.append(f"{settings['raw.exposure']:+.2f} EV")
    if "raw.highlights" in settings:
        parts.append(f"highlights {settings['raw.highlights']:+.0f}")
    others = [f"{k} {v:+g}" for k, v in settings.items() if k not in SETUP_FIELDS]
    return ", ".join(parts + others) or "the preset as it is"


def setup_show(show: Show, *, frames: int = 5, tries: list[dict] = (), size: int = 640, jobs: int = 6) -> dict:
    """Grades `frames` representative frames with candidate settings and puts them on one labeled sheet,
    setup/setup-sheet.jpg (rows: frames; columns: candidates A, B, …):
      A  as shot: each frame's own camera white balance (raw.temperature=0), no exposure change
      B  the show base: the profile's learned settings if it has any, else the camera's median white balance
      C  warmer (+300 K)  D  cooler (−300 K)  E  brighter (+0.3 EV)  F  darker (−0.3 EV)
      G… each of `tries` (settings on top of B)
    Approve with approve_setup."""
    p = show.profile
    folder = show.folder / "setup"
    cells = folder / "cells"
    if cells.exists():
        shutil.rmtree(cells)
    cells.mkdir(parents=True)
    reps = representative(show.frames(), frames)

    def grade(rep, label, settings):
        long = max(rep.get("width") or 6000, rep.get("height") or 4000)
        w = round((rep.get("width") or 6000) * size / long)
        h = round((rep.get("height") or 4000) * size / long)
        out = cells / f"{Path(rep['path']).stem}-{label}.png"
        report = develop(rep["path"], show.data["preset"], out, size=(w, h), settings=settings or None,
                         match=show.data.get("match"))
        return out, report

    with ThreadPoolExecutor(jobs) as pool:
        first = list(pool.map(lambda rep: grade(rep, "A", AS_SHOT), reps))
    as_shot = [r["asShot"] for _, r in first if r.get("asShot")]
    learned = {k: v for k, v in (p.get("setup") or {}).items() if k in SETUP_FIELDS}
    if learned.get("raw.temperature"):
        base = learned
        origin = f"the {p['name']} profile's learned settings"
    elif as_shot:
        base = {"raw.temperature": round(float(np.median([a[0] for a in as_shot])) / 50) * 50,
                "raw.tint": round(float(np.median([a[1] for a in as_shot]))), **{k: v for k, v in learned.items()}}
        origin = "the camera's median white balance"
    else:
        base, origin = dict(learned), "the preset's own settings"
    candidates = {"A": {"name": "As shot", "settings": dict(AS_SHOT)},
                  "B": {"name": "Show base", "settings": base}}
    if base.get("raw.temperature"):
        candidates["C"] = {"name": "Warmer", "settings": {**base, "raw.temperature": base["raw.temperature"] + 300}}
        candidates["D"] = {"name": "Cooler", "settings": {**base, "raw.temperature": base["raw.temperature"] - 300}}
    exposure = base.get("raw.exposure", 0.0)
    candidates[LABELS[len(candidates)]] = {"name": "Brighter", "settings": {**base, "raw.exposure": round(exposure + 0.3, 2)}}
    candidates[LABELS[len(candidates)]] = {"name": "Darker", "settings": {**base, "raw.exposure": round(exposure - 0.3, 2)}}
    for extra in tries:
        candidates[LABELS[len(candidates)]] = {"name": "Try", "settings": {**base, **extra}}
    jobs_list = [(rep, label, c["settings"]) for rep in reps for label, c in candidates.items() if label != "A"]
    with ThreadPoolExecutor(jobs) as pool:
        rest = list(pool.map(lambda job: grade(*job), jobs_list))
    images = {(Path(rep["path"]).stem, "A"): out for rep, (out, _) in zip(reps, first)}
    images.update({(Path(rep["path"]).stem, label): out for (rep, label, _), (out, _) in zip(jobs_list, rest)})
    for rep, (_, report) in zip(reps, first):
        rep["as_shot"] = report.get("asShot")
    sheet = _setup_sheet(reps, candidates, images, folder / "setup-sheet.jpg", size)
    setup = {"made": _now(), "base_from": origin, "candidates": candidates,
             "frames": [{"row": i + 1, "name": r["name"], "path": r["path"], "captured": r["captured"], "ev": r.get("ev"),
                         "as_shot": r.get("as_shot")} for i, r in enumerate(reps)], "sheet": str(sheet)}
    _write_json(folder / "setup.json", setup)
    return setup


def _setup_sheet(reps, candidates, images, output: Path, size: int) -> Path:
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", max(18, size // 22), index=1)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", max(14, size // 30))
    except OSError:
        font = small = ImageFont.load_default()
    cell, gap, left, top = size, 10, 240, 90
    page = Image.new("RGB", (left + len(candidates) * (cell + gap) + gap, top + len(reps) * (cell + gap) + gap), "#1A1A1A")
    draw = ImageDraw.Draw(page)
    for column, (label, candidate) in enumerate(candidates.items()):
        x = left + gap + column * (cell + gap)
        draw.text((x, 12), f"{label} · {candidate['name']}", fill="#FFFFFF", font=font)
        draw.text((x, 50), _describe(candidate["settings"])[:60], fill="#CCCCCC", font=small)
    for row, rep in enumerate(reps):
        y = top + gap + row * (cell + gap)
        when = rep["captured"][11:16] if rep.get("captured") else "?"
        lines = [f"{row + 1}", when, rep["name"][:22], f"EV {rep['ev']:.1f}" if rep.get("ev") is not None else "",
                 f"camera {rep['as_shot'][0]:.0f} K" if rep.get("as_shot") else ""]
        for i, line in enumerate(lines):
            draw.text((12, y + 8 + 32 * i), line, fill="#FFFFFF" if i == 0 else "#CCCCCC", font=font if i == 0 else small)
        for column, label in enumerate(candidates):
            path = images.get((Path(rep["path"]).stem, label))
            if not path:
                continue
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((cell, cell))
                x = left + gap + column * (cell + gap)
                page.paste(image, (x + (cell - image.width) // 2, y + (cell - image.height) // 2))
    page.save(output, quality=88)
    return output


def approve_setup(show: Show, choice: str, tweaks: dict | None = None) -> dict:
    """Saves the approved settings with the show. `choice` is a candidate letter for the whole show ("B"), or one per
    sheet row ("1=B,2=B,3=C,4=B,5=E") when the lighting changes: each frame then takes the settings of the row frame
    nearest it in time and lighting. `tweaks` ({"raw.exposure": 0.05}) go on top."""
    setup = _read_json(show.folder / "setup" / "setup.json", None)
    if not setup:
        raise CompError("no setup sheet yet: run `compkit show setup` first")
    candidates, rows = setup["candidates"], setup["frames"]
    choice = choice.strip().upper()
    if "=" in choice:
        picked = {}
        for part in choice.split(","):
            row, _, label = part.partition("=")
            picked[int(row)] = label.strip()
        missing = [r["row"] for r in rows if r["row"] not in picked]
        if missing:
            raise CompError(f"give a candidate for every row: rows {missing} are missing")
    else:
        picked = {r["row"]: choice for r in rows}
    for label in picked.values():
        if label not in candidates:
            raise CompError(f"no candidate {label!r} on the sheet (there are {', '.join(candidates)})")
    with_tweaks = lambda label: {**candidates[label]["settings"], **(tweaks or {})}
    labels = set(picked.values())
    if len(labels) == 1:
        label = labels.pop()
        settings = {"default": with_tweaks(label), "by_lighting": None, "approved": label}
    else:
        settings = {"default": with_tweaks(picked[rows[0]["row"]]), "approved": choice,
                    "by_lighting": [{"row": r["row"], "name": r["name"], "captured": r["captured"], "ev": r["ev"],
                                     "settings": with_tweaks(picked[r["row"]])} for r in rows]}
    settings["at"] = _now()
    show.data["settings"] = settings
    show.save()
    return settings


def settings_for(show: Show, frame: dict) -> dict:
    """The approved settings for one frame: the show's, or with per-lighting approval those of the nearest row."""
    approved = show.data.get("settings")
    if not approved:
        raise CompError("approve the show's settings first: `compkit show setup SHOW`, look at the sheet, then "
                        "`compkit show setup SHOW --approve B`")
    groups = approved.get("by_lighting")
    if not groups:
        return dict(approved["default"])
    when = _seconds(frame)
    times = [_seconds(g) for g in groups]
    span = max(1.0, max(t for t in times if t) - min(t for t in times if t)) if any(times) else 1.0

    def distance(g, t):
        dt_ = ((when - t) / span) if when and t else 0.0
        dev = ((frame.get("ev") or 0) - (g.get("ev") or 0)) / 1.5 if frame.get("ev") is not None and g.get("ev") is not None else 0.0
        return dt_ * dt_ + dev * dev

    return dict(min(zip(groups, times), key=lambda gt: distance(*gt))[0]["settings"])


# Grading -------------------------------------------------------------------------------------------------------------

def graded_names(paths) -> dict[str, str]:
    """The project name for each source: its file name without extension, unless two sources share one (two cards,
    two cameras), when each gets a short fingerprint of its folder too, so they never overwrite each other."""
    paths = [str(p) for p in paths]
    stems: dict[str, list[str]] = {}
    for path in paths:
        stems.setdefault(Path(path).stem, []).append(path)
    names = {}
    for stem, members in stems.items():
        for path in members:
            names[path] = stem if len(members) == 1 else f"{stem}-{hashlib.sha1(path.encode()).hexdigest()[:6]}"
    return names


def _same(a: dict, b: dict) -> bool:
    return set(a) == set(b) and all(abs(float(a[k]) - float(b[k])) < 1e-6 for k in a)


def _graded_state(out: Path, name: str, wanted: dict, render: bool) -> str:
    """'done', 'render' (the project is current but its JPEG is missing or older than the project) or 'grade'."""
    project, sources = out / f"{name}.comp", out / f"{name}.sources.json"
    if not (project / "manifest.json").exists() or not sources.exists():
        return "grade"
    try:
        photos = json.loads(sources.read_text())["photos"]
        entry = next(iter(photos.values()))
    except (ValueError, KeyError, StopIteration):
        return "grade"
    if entry.get("source") != wanted["source"] or entry.get("preset") != wanted["preset"] or \
            entry.get("match") != wanted["match"] or abs(entry.get("amount", 1) - wanted["amount"]) > 1e-9 or \
            not _same(entry.get("settings") or {}, wanted["settings"]):
        return "grade"
    crop, rotate = wanted.get("crop"), wanted.get("rotate") or 0.0
    if abs((entry.get("rotate") or 0.0) - rotate) > 1e-6 or bool(entry.get("framed")) != bool(crop) or \
            (crop and any(abs(a - b) > 1e-4 for a, b in zip(entry["crop"], crop))):
        return "grade"
    jpeg = out / f"{name}.jpg"
    if render and (not jpeg.exists() or jpeg.stat().st_mtime < (project / "manifest.json").stat().st_mtime):
        return "render"
    return "done"


def grade_batch(pictures, preset, out, *, settings_for=lambda path: {}, match=None, amount: float = 1.0,
                max_side: int = 2048, render: bool = True, jobs: int = 4, names: dict | None = None,
                progress: Progress | None = None, framing_for=lambda path: {}) -> dict:
    """Grades each picture into out/<name>.comp (+ .jpg with `render`), several at once. Resumable: a project whose
    source, preset, look, strength and settings match what's asked is kept (only its JPEG is made again if it's
    missing or older than the project, as after tuning it in Compositor). A picture that fails is logged and the
    rest carry on. `framing_for(path)` gives a photo's {"crop": (x, y, w, h), "rotate": degrees} (see framing.py);
    without one it's framed whole. Returns the progress summary."""
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    progress = progress or Progress("grade", unit="photos")
    for stale in out.glob(".*.comp"):  # a save that was stopped halfway
        shutil.rmtree(stale, ignore_errors=True)
    pictures = [str(Path(p).expanduser().resolve()) for p in pictures]
    names = names or graded_names(pictures)
    preset = str(Path(preset).expanduser().resolve())
    match = str(Path(match).expanduser().resolve()) if match else None
    plan = []
    for picture in pictures:
        framing = framing_for(picture) or {}
        wanted = {"source": picture, "preset": preset, "match": match, "amount": amount,
                  "settings": {k: float(v) for k, v in settings_for(picture).items()},
                  "crop": [float(v) for v in framing["crop"]] if framing.get("crop") else None,
                  "rotate": float(framing.get("rotate") or 0)}
        plan.append((picture, wanted, _graded_state(out, names[picture], wanted, render)))
    todo = [item for item in plan if item[2] != "done"]
    progress.start(len(plan), done=len(plan) - len(todo))

    def render_atomic(project: Project, name: str) -> None:
        temporary = out / f".{name}.tmp.jpg"
        project.render(temporary, quality=0.92)
        os.replace(temporary, out / f"{name}.jpg")

    def one(item):
        picture, wanted, state = item
        name = names[picture]
        if state == "render":
            render_atomic(Project.open(out / f"{name}.comp"), name)
            return
        found = analyze(picture)
        w, h = found["width"], found["height"]
        if wanted["rotate"]:
            from .framing import inscribed_scale
            keep = inscribed_scale(w, h, wanted["rotate"])
            w, h = w * keep, h * keep
        if wanted["crop"]:
            w, h = w * wanted["crop"][2], h * wanted["crop"][3]
        scale = min(1, max_side / max(w, h))
        project = Project.new(max(1, round(w * scale)), max(1, round(h * scale)))
        project.add_graded_photo(picture, preset, name="Photo", amount=amount, settings=wanted["settings"] or None,
                                 max_scale=1, match=match, crop=wanted["crop"], rotate=wanted["rotate"])
        project.save(out / f"{name}.comp")
        if render:
            render_atomic(project, name)

    pool = ThreadPoolExecutor(max(1, jobs))
    try:
        futures = {pool.submit(one, item): item[0] for item in todo}
        for future in as_completed(futures):
            try:
                future.result()
            except Stopped:
                raise KeyboardInterrupt
            except Exception as error:  # noqa: BLE001 — one bad file must not stop a show
                progress.fail(futures[future], error)
            progress.advance()
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        progress.note("stopped; run it again to carry on from here")
        raise
    pool.shutdown()
    return progress.finish(out=str(out))


def framing(show: Show, paths: list[str], *, jobs: int = 6) -> dict[str, dict]:
    """Each photo's crop and leveling: set by hand (set_framing, the dashboard), else, with the profile's auto_crop
    on, framing.suggest's (worked out once and kept), else none. Kept in SHOW/crops.json."""
    from .framing import suggest
    path = show.folder / "crops.json"
    crops = _read_json(path, {"manual": {}, "auto": {}})
    if show.profile.get("auto_crop"):
        missing = [p for p in paths if p not in crops["manual"] and p not in crops["auto"]]
        if missing:
            with ThreadPoolExecutor(jobs) as pool:
                for p, found in zip(missing, pool.map(lambda p: _analysis_or_none(p), missing)):
                    if found:
                        suggestion = suggest(found)
                        crops["auto"][p] = {"crop": suggestion["crop"], "rotate": suggestion["rotate"], "why": suggestion["why"]}
            _write_json(path, crops)
    result = {}
    for p in paths:
        chosen = crops["manual"].get(p) or (crops["auto"].get(p) if show.profile.get("auto_crop") else None)
        if chosen and (chosen.get("crop") or chosen.get("rotate")):
            result[p] = {"crop": chosen.get("crop"), "rotate": chosen.get("rotate") or 0.0}
    return result


def _analysis_or_none(path):
    try:
        return analyze(path)
    except CompError:
        return None


def set_framing(show: Show, path: str, crop=None, rotate: float = 0.0, clear: bool = False) -> dict:
    """A photo's crop ((x, y, w, h) fractions of the leveled photo) and leveling (degrees counterclockwise) set by
    hand; `clear` goes back to the automatic framing. The next grade and export use it."""
    file = show.folder / "crops.json"
    crops = _read_json(file, {"manual": {}, "auto": {}})
    if clear:
        crops["manual"].pop(path, None)
    else:
        if crop is not None:
            x, y, w, h = (float(v) for v in crop)
            if not (0 <= x and 0 <= y and 0 < w and 0 < h and x + w <= 1.0001 and y + h <= 1.0001):
                raise CompError("a crop is x, y, width, height as fractions of the photo, inside it")
            crop = [round(x, 5), round(y, 5), round(w, 5), round(h, 5)]
        if not -45 <= float(rotate) <= 45:
            raise CompError("leveling is -45 to 45 degrees")
        crops["manual"][path] = {"crop": crop, "rotate": float(rotate)}
    _write_json(file, crops)
    return crops["manual"].get(path) or {}


def grade_show(show: Show, *, jobs: int = 6) -> dict:
    frames = show.frames()
    picks = [f for f in frames if f["status"] == "pick"]
    by_path = {f["path"]: f for f in picks}
    if not show.data.get("settings"):
        settings_for(show, {})  # explains what to do
    names = graded_names([f["path"] for f in frames])
    framings = framing(show, [f["path"] for f in picks], jobs=jobs)
    summary = grade_batch([f["path"] for f in picks], show.data["preset"], show.graded_folder,
                          settings_for=lambda path: settings_for(show, by_path[path]), match=show.data.get("match"),
                          jobs=jobs, names={f["path"]: names[f["path"]] for f in picks},
                          progress=Progress("grade", folder=show.logs, unit="photos"), framing_for=framings.get)
    current = {names[f["path"]] for f in picks}
    summary["graded, no longer picked"] = sum(1 for p in show.graded_folder.glob("*.comp") if p.stem not in current)
    show.data["grade"] = {"picks": len(picks), "at": _now(), "failed": summary["failed"]}
    show.save()
    return summary


# Export -------------------------------------------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Safe in a file or folder name on macOS, Windows and cloud drives."""
    return re.sub(r"\s+", " ", re.sub(r'[/\\:*?"<>|]', "-", str(text))).strip()


def delivery_plan(show: Show, picks: list[dict], sizes: list[str] | None = None) -> list[dict]:
    """Where each pick's delivered files go: one entry per pick with its graded project and, per size, the file.
    Names follow the spec's `naming` ({show}, {date}, {number}: the camera's file number, {stem}, {seq}: its place
    in capture order) and `folders` ({size}, {hour}: the hour it was shot, as 19.00, with the date in front when a
    show runs past midnight). Two picks that would share a name get -2, -3 … in capture order."""
    spec = show.delivery
    wanted = [s for s in spec["sizes"] if not sizes or s["name"] in sizes]
    if sizes and len(wanted) != len(sizes):
        raise CompError(f"the delivery spec's sizes are {', '.join(s['name'] for s in spec['sizes'])}")
    picks = sorted(picks, key=lambda f: (f.get("captured") or "", f["name"]))
    dates = {f["captured"][:10] for f in picks if f.get("captured")}
    show_date = show.data.get("date") or (min(dates) if dates else "")
    try:
        date_text = dt.date.fromisoformat(show_date).strftime(spec.get("date_format", "%d-%m-%y")) if show_date else ""
    except ValueError:
        date_text = show_date
    names = graded_names(list(dict.fromkeys([f["path"] for f in show.frames()] + [f["path"] for f in picks])))
    plan, used = [], set()
    for seq, frame in enumerate(picks, start=1):
        stem = Path(frame["path"]).stem
        digits = re.search(r"(\d+)$", stem)
        if frame.get("captured"):
            hour = f"{frame['captured'][11:13]}.00"
            if len(dates) > 1:
                hour = f"{frame['captured'][:10]} {hour}"
        else:
            hour = "Unknown time"
        fields = {"show": _clean(show.data["name"]), "date": _clean(date_text), "number": digits.group(1) if digits else stem,
                  "stem": stem, "seq": f"{seq:04d}", "hour": hour}
        base = _clean(spec["naming"].format(**fields))
        entry = {"frame": frame, "project": show.graded_folder / f"{names[frame['path']]}.comp", "outputs": []}
        for size in wanted:
            folder = show.delivery_root / _clean(show.data["name"]) / Path(*[_clean(part) for part in
                                                                            spec["folders"].format(size=size["name"], **fields).split("/")])
            name, n = base, 1
            while (folder / f"{name}.jpg") in used:
                n += 1
                name = f"{base}-{n}"
            used.add(folder / f"{name}.jpg")
            entry["outputs"].append({"path": folder / f"{name}.jpg", "size": size})
        plan.append(entry)
    return plan


def _export_job(entry: dict, outputs: list[dict], quality: float) -> dict:
    project = entry["project"]
    sources = json.loads(project.with_suffix(".sources.json").read_text())["photos"]
    group_id, source = next(iter(sources.items()))
    manifest = json.loads((project / "manifest.json").read_text())
    base = next(r for r in manifest["layers"] if r.get("parentID") == group_id and " · Base Grade" in r["name"])
    long_side = max(manifest["width"], manifest["height"])
    job = {"project": str(project), "metadata": source["source"],
           "outputs": [{"path": str(o["path"]), "maxSide": o["size"].get("max_side"),
                        "maxBytes": o["size"]["max_kb"] * 1024 if o["size"].get("max_kb") else None,
                        "quality": o["size"].get("quality", quality)} for o in outputs]}
    if any(not o["size"].get("max_side") or o["size"]["max_side"] > long_side for o in outputs):
        crop = source.get("crop")
        # A project's own crop of the whole frame differs from it by its rounding: deliver the whole frame.
        if crop and not source.get("framed") and crop[0] < 0.004 and crop[1] < 0.004 and crop[2] > 0.992 and crop[3] > 0.992:
            crop = None
        job["develop"] = {"layer": base["id"], "source": source["source"], "preset": source["preset"], "crop": crop,
                          "amount": source.get("amount", 1), "settings": source.get("settings") or {},
                          "match": source.get("match"), "rotate": source.get("rotate") or 0.0}
    return job


def export_show(show: Show, *, jobs: int = 3, sizes: list[str] | None = None, prune: bool = False) -> dict:
    """Writes the delivery: every graded pick at every size of the spec, under delivery_root/<show name>/. Resumable:
    a file newer than its project is kept. Full-resolution files are graded again from the RAW (several seconds
    each); smaller sizes come from the project, or from the full-resolution render when both are due."""
    picks = show.picks()
    plan = delivery_plan(show, picks, sizes)
    progress = Progress("export", folder=show.logs, unit="photos")
    todo = []
    for entry in plan:
        project = entry["project"]
        if not (project / "manifest.json").exists():
            todo.append((entry, None))
            continue
        newest = max((project / "manifest.json").stat().st_mtime, project.with_suffix(".sources.json").stat().st_mtime)
        missing = [o for o in entry["outputs"] if not o["path"].exists() or o["path"].stat().st_mtime < newest]
        if missing:
            todo.append((entry, missing))
    full = sum(1 for _, outputs in todo for o in (outputs or []) if not o["size"].get("max_side"))
    need = sum(o["size"].get("max_kb", 20_000) * 1024 if not o["size"].get("max_side") else 1_200_000
               for _, outputs in todo for o in (outputs or []))
    show.delivery_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(show.delivery_root).free
    if need * 1.2 + 2e9 > free:
        raise CompError(f"the delivery needs about {need / 1e9:.1f} GB and {show.delivery_root} has {free / 1e9:.1f} GB free")
    progress.start(len(plan), done=len(plan) - len(todo))
    if full:
        progress.note(f"{full:,} full-resolution files to make: each is graded again from its RAW")
    quality = show.delivery.get("quality", 0.92)

    def one(item):
        entry, outputs = item
        if outputs is None:
            raise CompError(f"not graded yet ({entry['project'].name}): run `compkit show grade` first")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
            json.dump(_export_job(entry, outputs, quality), file)
        try:
            result = subprocess.run([str(COMP_RENDER), "export", file.name], capture_output=True, text=True)
        finally:
            os.unlink(file.name)
        check_comp_render(result, "comp-render export")
        return [{**w, "size": o["size"]["name"]} for o, w in zip(outputs, json.loads(result.stdout))]

    written = []
    pool = ThreadPoolExecutor(max(1, jobs))
    try:
        futures = {pool.submit(one, item): item[0]["frame"]["path"] for item in todo}
        for future in as_completed(futures):
            try:
                result = future.result()
                written += result
                for w in result:
                    if not w.get("fits", True):
                        progress.note(f"over the size cap even at the lowest quality: {Path(w['path']).name} "
                                      f"({w['bytes'] / 1e6:.2f} MB)")
            except Stopped:
                raise KeyboardInterrupt
            except Exception as error:  # noqa: BLE001 — one bad file must not stop a show
                progress.fail(futures[future], error)
            progress.advance()
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        progress.note("stopped; run it again to carry on from here")
        raise
    pool.shutdown()
    expected = {o["path"] for entry in plan for o in entry["outputs"]}
    root = show.delivery_root / _clean(show.data["name"])
    extra = [p for p in root.rglob("*.jpg") if p not in expected] if root.exists() else []
    if prune:
        for path in extra:
            path.unlink()
    summary = progress.finish(delivery=str(root), written=len(written),
                              **{"over the size cap": sum(not w.get("fits", True) for w in written)},
                              **{"delivered files that aren't picks": len(extra), "removed": len(extra) if prune else 0})
    for size in show.delivery["sizes"]:
        made = [w["bytes"] for w in written if w["size"] == size["name"]]
        if made:
            summary[f"{size['name']}: average MB"] = round(float(np.mean(made)) / 1e6, 2)
    show.data["export"] = {"at": _now(), "failed": summary["failed"], "folder": str(root)}
    show.save()
    return summary


# Comparing with what was delivered, and learning from it ------------------------------------------------------------

def match_delivered(frames: list[dict], delivered) -> tuple[dict[str, Path], list[Path], dict[str, dict]]:
    """Which frame each delivered JPEG is: by the RAW name Lightroom embeds with develop settings, else by capture
    time, else by the camera's file number when only one frame has it. Returns {frame name: file}, the files that
    matched nothing, and {frame name: the embedded Lightroom settings} where there were some."""
    files = sorted(p for p in Path(delivered).expanduser().rglob("*")
                   if p.suffix.lower() in (".jpg", ".jpeg") and not p.name.startswith("."))
    by_name = {}
    for f in frames:
        by_name.setdefault(f["name"], []).append(f)
    by_time = {f["captured"]: f for f in frames if f.get("captured")}
    by_number = {}
    for f in frames:
        digits = re.search(r"(\d+)$", Path(f["name"]).stem)
        if digits:
            by_number.setdefault(digits.group(1), []).append(f)
    matched, unmatched, settings = {}, [], {}
    leftovers = []
    for path in files:
        found = lightroom_settings(path)
        if found and found[1].get("RawFileName") in by_name:
            frame = by_name[found[1]["RawFileName"]][0]
            matched[frame["name"]] = path
            settings[frame["name"]] = found[1]
        else:
            leftovers.append(path)
    if leftovers:
        probes = json.loads(_run_listed("probe", leftovers))
        for path, probe in zip(leftovers, probes):
            frame = by_time.get(probe.get("captured"))
            if not frame:
                digits = re.search(r"(\d+)(?:-\d+)?$", path.stem)
                candidates = by_number.get(digits.group(1), []) if digits else []
                frame = candidates[0] if len(candidates) == 1 else None
            if frame:
                matched[frame["name"]] = path
            else:
                unmatched.append(path)
    return matched, unmatched, settings


def pick_metrics(frames: list[dict], kept: set[str]) -> dict:
    """How a cull's picks compare with the frames that were delivered: moment coverage (of the moments with a
    delivered frame, how many have a pick), delivered frames it rejected, delivered frames it picked, and picks per
    moment beside delivered frames per moment, by moment size."""
    moments = moments_of(frames)
    used = {m for m, members in moments.items() if any(f["name"] in kept for f in members)}
    covered = sum(any(f["status"] == "pick" for f in moments[m]) for m in used)
    rejected = [f["name"] for f in frames if f["name"] in kept and f["status"] == "reject"]
    picked = sum(1 for f in frames if f["name"] in kept and f["status"] == "pick")
    rows, errors = {}, []
    for members in moments.values():
        p = sum(f["status"] == "pick" for f in members)
        k = sum(f["name"] in kept for f in members)
        errors.append(abs(p - k))
        n = len(members)
        bucket = str(n) if n < 5 else ("5–7" if n < 8 else "8+")
        row = rows.setdefault(bucket, {"moments": 0, "picks": 0, "delivered": 0})
        row["moments"] += 1; row["picks"] += p; row["delivered"] += k
    order = ["1", "2", "3", "4", "5–7", "8+"]
    table = [{"size": b, **rows[b], "picks per moment": round(rows[b]["picks"] / rows[b]["moments"], 2),
              "delivered per moment": round(rows[b]["delivered"] / rows[b]["moments"], 2)} for b in order if b in rows]
    picks = sum(f["status"] == "pick" for f in frames)
    return {"frames": len(frames), "picks": picks, "delivered": len(kept & {f["name"] for f in frames}),
            "moments": len(moments), "moments delivered from": len(used), "moments covered": covered,
            "coverage": round(covered / max(1, len(used)), 3), "delivered but rejected": rejected,
            "delivered and picked": picked, "recall": round(picked / max(1, len(kept)), 3),
            "precision": round(picked / max(1, picks), 3),
            "moments picked from but not delivered from": sum(1 for m, members in moments.items()
                                                               if m not in used and any(f["status"] == "pick" for f in members)),
            "picks per moment error": round(float(np.mean(errors)), 3), "by moment size": table}


def _coarse_delta_e(mine: Image.Image, theirs: Image.Image, preview: int = 360) -> float:
    """learn-look's measure: both at a small size, softened, mean CIELAB ΔE76 per pixel."""
    theirs = theirs.convert("RGB")
    theirs.thumbnail((preview, preview), Image.LANCZOS)
    mine = mine.convert("RGB").resize(theirs.size, Image.LANCZOS)
    shrink = lambda image: np.asarray(image.resize((max(1, image.width // 3), max(1, image.height // 3)), Image.BOX))
    return float(np.sqrt(((_lab(shrink(mine)) - _lab(shrink(theirs))) ** 2).sum(-1)).mean())


def grade_metrics(show: Show, matched: dict[str, Path], lightroom: dict[str, dict], frames: list[dict]) -> dict:
    """Coarse ΔE between the pipeline's graded renders and the delivered finals, for frames both have. A final's
    Lightroom crop is applied to the render first; finals that are straightened (rotated crops) are skipped. Split by
    whether the final kept the show's approved settings or was tuned away from them."""
    names = graded_names([f["path"] for f in frames])
    by_name = {f["name"]: f for f in frames}
    approved = (show.data.get("settings") or {}).get("default") or {}
    rows, skipped = [], {"not graded": 0, "straightened or cropped differently": 0}
    for name, final in matched.items():
        frame = by_name[name]
        render = show.graded_folder / f"{names[frame['path']]}.jpg"
        if not render.exists():
            skipped["not graded"] += 1
            continue
        attributes = lightroom.get(name, {})
        mine = Image.open(render)
        if attributes.get("HasCrop") == "True":
            orientation = json.loads(comp_render("probe", frame["path"]))[0].get("orientation", 1)
            crop = lightroom_crop(attributes, orientation)
            if crop is None:
                skipped["straightened or cropped differently"] += 1
                continue
            w, h = mine.size
            mine = mine.crop((round(crop[0] * w), round(crop[1] * h), round((crop[0] + crop[2]) * w), round((crop[1] + crop[3]) * h)))
        theirs = Image.open(final)
        if abs(mine.width / mine.height - theirs.width / theirs.height) > 0.02:
            skipped["straightened or cropped differently"] += 1
            continue
        tuned = None
        if attributes and approved:
            # Only what was approved can be compared: a show-wide Kelvin (not the camera's own, raw.temperature=0)
            # and an exposure override (without one, the preset's own exposure applied).
            kelvin, exposure = approved.get("raw.temperature", 0), approved.get("raw.exposure")
            same = [kelvin <= 0 or abs(float(attributes.get("Temperature", kelvin)) - kelvin) <= 50,
                    exposure is None or abs(float(attributes.get("Exposure2012", exposure)) - exposure) <= 0.05]
            tuned = not all(same)
        rows.append({"frame": name, "delta_e": round(_coarse_delta_e(mine, theirs), 2), "tuned in Lightroom": tuned})
    values = [r["delta_e"] for r in rows]
    summary = {"compared": len(rows), "skipped": skipped}
    if values:
        summary.update({"delta_e median": round(float(np.median(values)), 2), "delta_e mean": round(float(np.mean(values)), 2),
                        "delta_e p90": round(float(np.percentile(values, 90)), 2)})
        for label, flag in (("kept the show settings", False), ("tuned in Lightroom", True)):
            part = [r["delta_e"] for r in rows if r["tuned in Lightroom"] is flag]
            if part:
                summary[f"delta_e mean, {label}"] = round(float(np.mean(part)), 2)
                summary[f"frames that {label}"] = len(part)
    summary["worst"] = sorted(rows, key=lambda r: -r["delta_e"])[:5]
    return summary


def delivered_settings(lightroom: dict[str, dict]) -> dict:
    """The show-wide settings a photographer used in Lightroom: the median of each across the delivered frames."""
    keys = {"raw.temperature": "Temperature", "raw.tint": "Tint", "raw.exposure": "Exposure2012", "raw.highlights": "Highlights2012"}
    result = {}
    for field, key in keys.items():
        values = []
        for attributes in lightroom.values():
            try:
                values.append(float(attributes[key]))
            except (KeyError, ValueError):
                pass
        if values:
            result[field] = round(float(np.median(values)), 2)
    return result


RULE_GRID = {"tau": [0, 0.15, 0.3, 0.5, 1.0], "cover": [0, 0.25, 0.5, 1.0, 2.0], "beta": [0, 0.3, 0.6, 1.0]}


def fit_rule(shows: list[dict], current: dict | None = None) -> tuple[dict, float]:
    """The per-moment rule that best matches what was delivered across `shows` (each {"frames": [...], "kept":
    [...]}, frames grouped and judged): each candidate picks as many frames as were delivered and is scored on moment
    coverage plus the share of delivered frames it picked, averaged over the shows. The current rule is kept unless
    another beats it by more than 0.005."""
    def value(rule):
        total = 0.0
        for item in shows:
            frames, kept = item["frames"], set(item["kept"])
            assign(frames, count=len(kept), rule=rule)
            m = pick_metrics(frames, kept)
            total += m["coverage"] + m["recall"]
        return total / max(1, len(shows))
    best, best_value = None, -1.0
    for tau, cover, beta in itertools.product(RULE_GRID["tau"], RULE_GRID["cover"], RULE_GRID["beta"]):
        rule = {"tau": tau, "cover": cover, "beta": beta}
        score = value(rule)
        if score > best_value + 1e-9:
            best, best_value = rule, score
    if current:
        current_value = value(current)
        if current_value >= best_value - 0.005:
            return dict(current), current_value
    return best, best_value


def compare_show(show: Show, delivered, *, learn: bool = False) -> dict:
    """Compares the pipeline's picks and grades with what the photographer delivered (their Lightroom exports), writes
    report/compare.md and compare.json, and with `learn` updates the show's profile: its keep rate (the average over
    the shows learned from), its per-moment rule (fit_rule over those shows) and its setup settings (the medians of
    the delivered frames' Lightroom settings)."""
    frames = show.frames()
    matched, unmatched, lightroom = match_delivered(frames, delivered)
    kept = set(matched)
    as_run = pick_metrics(frames, kept)
    at_count = copy.deepcopy(frames)
    assign(at_count, count=len(kept), rule=show.profile["rule"])
    report = {"show": show.data["name"], "profile": show.data["profile"], "delivered folder": str(delivered),
              "delivered files": len(matched) + len(unmatched), "matched": len(matched),
              "unmatched": [p.name for p in unmatched][:20], "unmatched count": len(unmatched),
              "delivered rate": round(len(kept) / max(1, len(frames)), 3),
              "picks as run": as_run, "picks at the delivered count": pick_metrics(at_count, kept),
              "grades": grade_metrics(show, matched, lightroom, frames),
              "approved settings": (show.data.get("settings") or {}).get("default"),
              "delivered settings (median)": delivered_settings(lightroom)}
    if learn:
        report["learned"] = learn_profile(show, frames, kept, lightroom)
    folder = show.folder / "report"
    _write_json(folder / "compare.json", report)
    (folder / "compare.md").write_text(compare_markdown(report))
    return report


def learn_profile(show: Show, frames: list[dict], kept: set[str], lightroom: dict[str, dict]) -> dict:
    name = show.data["profile"]
    before = profile(name)
    history = profiles_home() / "history" / name
    record = {"show": show.data["name"], "date": show.data.get("date"), "folder": str(show.folder), "learned": _now(),
              "kept": sorted(kept), "settings": delivered_settings(lightroom),
              "frames": [{k: f.get(k) for k in ("name", "moment", "captured", "score", "flags")} for f in frames]}
    slug = re.sub(r"[^A-Za-z0-9]+", "-", f"{show.data.get('date') or ''}-{show.data['name']}").strip("-")
    _write_json(history / f"{slug}.json", record)
    shows = []
    for path in sorted(history.glob("*.json")):
        item = json.loads(path.read_text())
        shows.append({"frames": item["frames"], "kept": item["kept"], "rate": len(item["kept"]) / max(1, len(item["frames"])),
                      "settings": item.get("settings", {}), "date": item.get("date") or ""})
    rule, value = fit_rule([{"frames": copy.deepcopy(s["frames"]), "kept": s["kept"]} for s in shows], before["rule"])
    rate = round(float(np.mean([s["rate"] for s in shows])), 3)
    latest = max(shows, key=lambda s: s["date"])
    setup = latest["settings"] or before.get("setup")
    changes = {"rate": rate, "rule": rule, "shows learned from": len(shows)}
    if setup:
        changes["setup"] = setup
    save_profile(name, changes)
    return {"profile": name, "before": {k: before.get(k) for k in ("rate", "rule", "setup")},
            "after": {k: changes.get(k) for k in ("rate", "rule", "setup")}, "shows": len(shows),
            "fit (coverage + recall)": round(value, 3)}


def compare_markdown(report: dict) -> str:
    run, at = report["picks as run"], report["picks at the delivered count"]
    lines = [f"# {report['show']}: pipeline vs delivered", "",
             f"Profile **{report['profile']}**. {report['matched']:,} of {report['delivered files']:,} delivered files "
             f"matched to frames ({report['delivered rate']:.0%} of {run['frames']:,} frames delivered).", "",
             "## Picks", "",
             "| | as run | at your delivered count |", "| --- | ---: | ---: |",
             f"| picks | {run['picks']:,} | {at['picks']:,} |",
             f"| moments covered | {run['moments covered']}/{run['moments delivered from']} | {at['moments covered']}/{at['moments delivered from']} |",
             f"| delivered frames rejected | {len(run['delivered but rejected'])} | {len(at['delivered but rejected'])} |",
             f"| delivered frames picked | {run['delivered and picked']} | {at['delivered and picked']} |",
             f"| picks per moment, mean error | {run['picks per moment error']} | {at['picks per moment error']} |", "",
             "| moment size | moments | picks per moment | delivered per moment |", "| ---: | ---: | ---: | ---: |"]
    lines += [f"| {r['size']} | {r['moments']} | {r['picks per moment']} | {r['delivered per moment']} |" for r in run["by moment size"]]
    if run["delivered but rejected"]:
        lines += ["", "Delivered but rejected: " + ", ".join(run["delivered but rejected"][:30])]
    g = report["grades"]
    lines += ["", "## Grades", ""]
    if g.get("compared"):
        lines += [f"Coarse ΔE (as learn-look measures it) on {g['compared']} frames: median {g['delta_e median']}, "
                  f"mean {g['delta_e mean']}, 90th percentile {g['delta_e p90']}."]
        for label in ("kept the show settings", "tuned in Lightroom"):
            if f"delta_e mean, {label}" in g:
                lines.append(f"- {g[f'frames that {label}']} frames that {label}: mean {g[f'delta_e mean, {label}']}")
    else:
        lines.append("No graded frames to compare.")
    lines += ["", f"Approved settings: {report['approved settings']}", f"Your delivered settings (median): {report['delivered settings (median)']}"]
    if report.get("learned"):
        l = report["learned"]
        lines += ["", "## Profile learned", "", f"From {l['shows']} show(s): rate {l['before']['rate']} → {l['after']['rate']}, "
                  f"rule {l['before']['rule']} → {l['after']['rule']}, setup {l['before'].get('setup')} → {l['after'].get('setup')}."]
    return "\n".join(lines) + "\n"


# Status -------------------------------------------------------------------------------------------------------------

def _size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def status(show: Show) -> dict:
    """What's done and what's next."""
    result = {"show": show.data["name"], "folder": str(show.folder), "profile": show.data["profile"]}
    if not (show.cull_folder / "cull.json").exists():
        result["next"] = f"compkit show cull {show.folder}"
        return result
    frames = show.frames()
    picks = [f for f in frames if f["status"] == "pick"]
    result["cull"] = {"frames": len(frames), "picks": len(picks), "rate": round(len(picks) / max(1, len(frames)), 3),
                      "rejects": sum(f["status"] == "reject" for f in frames), "sheets": str(show.cull_folder / "sheets")}
    approved = show.data.get("settings")
    result["setup"] = {"approved": approved.get("approved"), "settings": approved.get("default"),
                       "per lighting": bool(approved.get("by_lighting"))} if approved else "not approved"
    names = graded_names([f["path"] for f in frames])
    graded = sum(1 for f in picks if (show.graded_folder / f"{names[f['path']]}.jpg").exists())
    result["grade"] = {"graded": graded, "of": len(picks)}
    if graded:
        plan = delivery_plan(show, picks)
        result["export"] = {size["name"]: sum(1 for e in plan for o in e["outputs"] if o["size"]["name"] == size["name"] and o["path"].exists())
                            for size in show.delivery["sizes"]}
    result["disk"] = {name: f"{_size(show.folder / name) / 1e9:.2f} GB" for name in ("cull", "setup", "graded", "delivery")}
    failures = show.logs / "failures.csv"
    result["failures logged"] = max(0, len(failures.read_text().splitlines()) - 1) if failures.exists() else 0
    if not approved:
        result["next"] = f"compkit show setup {show.folder}   (then look at setup/setup-sheet.jpg and approve)"
    elif graded < len(picks):
        result["next"] = f"compkit show grade {show.folder}"
    elif any(v < len(picks) for v in result.get("export", {}).values()):
        result["next"] = f"compkit show export {show.folder}"
    else:
        result["next"] = f"compkit show compare {show.folder} --delivered <your Lightroom exports>"
    return result
