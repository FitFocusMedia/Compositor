"""Culling a shoot: from thousands of frames to the ones worth grading and delivering.

Every frame is read from its embedded preview (RAWs aren't decoded) by `comp-render probe` and `comp-render score`,
so a few thousand frames take a minute or two. With a cache folder, finished frames are kept as they're scored, so an
interrupted cull picks up where it stopped and a re-cull of the same files takes seconds. Frames are then:

1. grouped into moments: consecutive frames shot within `gap` seconds of each other that look alike (Vision feature
   prints closer than `distance`), as a burst or a held pose is; `max_moment` splits a run longer than that many
   seconds, for action where one scene runs on for minutes;
2. flagged where something is clearly wrong: nothing anywhere in focus (well under the shoot's typical sharpest
   spot, so a deliberately shallow focus isn't flagged), the main face's eyes shut, the frame far darker or brighter
   than the shoot, a file that can't be read;
3. scored 0–100 within the shoot: subject sharpness, face quality, eyes open, Vision's aesthetic score, exposure;
4. picked (`assign`). Adaptive, for a keep rate or a count: picks are spread over the moments in proportion to each
   moment's size, so a long burst or a held pose gets more frames than a single shot, with the first frame of every
   moment favored so that moments are covered before any gets a third or fourth frame (see `assign` for the rule).
   Or fixed: the best `per_moment` unflagged frames of each moment. Unpicked frames of a moment are "alternate",
   flagged frames "reject".

What it can't judge is taste: which of several good frames of a moment shows the better expression or pose. Tested
against a photographer's own picks, that is no better than chance, so the alternates sit beside each pick on the
contact sheets.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import RAW_SUFFIXES, CompError, Stopped, comp_render
from .runlog import Progress

PICTURE_SUFFIXES = RAW_SUFFIXES | {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png"}
# What a cached frame was measured with; bump when probe or score output changes.
CACHE_VERSION = 1
# The per-moment rule (see `assign`), as calibrated on a posed shoot (429 frames, 170 delivered).
DEFAULT_RULE = {"tau": 0.3, "cover": 1.0, "beta": 0.0}


def gather(inputs) -> list[Path]:
    """Pictures under the given folders (recursively) and files, without Lightroom exports' duplicates of a RAW:
    a JPEG beside a RAW of the same name is skipped. Hidden files (macOS's ._ copies on cards) are skipped."""
    found = []
    for item in inputs:
        item = Path(item).expanduser()
        if item.is_dir():
            found += [p for p in item.rglob("*") if p.suffix.lower() in PICTURE_SUFFIXES and not p.name.startswith(".")]
        else:
            found.append(item)
    raws = {(p.parent, p.stem) for p in found if p.suffix.lower() in RAW_SUFFIXES}
    return sorted({p.resolve() for p in found if p.suffix.lower() in RAW_SUFFIXES or (p.parent, p.stem) not in raws})


def _run_listed(command: str, paths: list[Path], *extra) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
        listing.write("\n".join(str(p) for p in paths))
    try:
        return comp_render(command, "--list", listing.name, *extra)
    finally:
        os.unlink(listing.name)


def _rank(values: list[float | None]) -> list[float | None]:
    """Each value's percentile within the list (0–1), None kept None."""
    known = sorted(v for v in values if v is not None)
    if not known:
        return [None] * len(values)
    return [None if v is None else (np.searchsorted(known, v, side="right") - 0.5) / len(known) for v in values]


# Scanning ---------------------------------------------------------------------------------------------------------

def _key(path: Path) -> list[int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_size, stat.st_mtime_ns]


def _score_batch(batch: list[tuple[Path, list | None]], size: int) -> list[dict]:
    """Probes and scores a batch in one comp-render run. If comp-render fails on the batch, it's split in halves
    until the file it fails on is alone; that file is recorded as unreadable and the rest are scored."""
    paths = [path for path, _ in batch]

    def entry(path, key, probe, score, feature):
        return {"version": CACHE_VERSION, "path": str(path), "key": key, "size": size, "probe": probe, "score": score,
                "print": base64.b64encode(feature.astype(np.float32).tobytes()).decode() if feature is not None else None}

    try:
        with tempfile.TemporaryDirectory() as folder:
            prints_file = Path(folder) / "prints.bin"
            probes = {r["path"]: r for r in json.loads(_run_listed("probe", paths))}
            scores = json.loads(_run_listed("score", paths, "--size", str(size), "--embeddings", prints_file))
            features = _read_prints(prints_file, len(paths))
    except Stopped:
        raise
    except (CompError, ValueError, OSError) as error:
        if len(batch) == 1:
            message = (str(error).strip().splitlines() or ["comp-render failed"])[0]
            return [entry(paths[0], batch[0][1], {}, {"error": f"comp-render failed on it: {message}"}, None)]
        half = len(batch) // 2
        return _score_batch(batch[:half], size) + _score_batch(batch[half:], size)
    return [entry(path, key, probes.get(str(path), {}), score, feature)
            for (path, key), score, feature in zip(batch, scores, features)]


def _read_prints(path: Path, count: int) -> list[np.ndarray | None]:
    """The feature prints `comp-render score --embeddings` wrote: one row per frame, all zeros where there was none."""
    if not path.exists():
        return [None] * count
    raw = path.read_bytes()
    header, _, body = raw.partition(b"\n")
    rows, length = (int(v) for v in header.split())
    if rows != count or length == 0:
        return [None] * count
    matrix = np.frombuffer(body, dtype=np.float32, count=rows * length).reshape(rows, length)
    return [row.copy() if row.any() else None for row in matrix]


def scan(paths: list[Path], cache=None, *, size: int = 1024, chunk: int = 120, progress: Progress | None = None) -> list[dict]:
    """Probes and scores every path (see the module notes), in batches of `chunk`. With a `cache` folder, each
    finished batch is appended to cache/frames.jsonl, and frames already there (same file size and modification
    time) aren't scored again: a cull that was interrupted resumes, and one that finished reruns in seconds. A file
    comp-render can't read is recorded with an error instead of stopping the cull, and tried again on the next run."""
    cached: dict[str, dict] = {}
    cache_file = None
    if cache is not None:
        cache = Path(cache).expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        cache_file = cache / "frames.jsonl"
        if cache_file.exists():
            with open(cache_file) as file:
                for line in file:
                    try:
                        entry = json.loads(line)
                    except ValueError:  # the last line of a cull that was stopped mid-write
                        continue
                    cached[entry["path"]] = entry
    entries, todo = {}, []
    for path in paths:
        key = _key(path)
        entry = cached.get(str(path))
        if entry and entry.get("version") == CACHE_VERSION and entry.get("key") == key and entry.get("size") == size \
                and not entry["score"].get("error"):
            entries[str(path)] = entry
        else:
            todo.append((path, key))
    if progress:
        progress.start(len(paths), done=len(paths) - len(todo))
    out = open(cache_file, "a") if cache_file else None
    try:
        for start in range(0, len(todo), chunk):
            batch = todo[start:start + chunk]
            for entry in _score_batch(batch, size):
                entries[entry["path"]] = entry
                if entry["score"].get("error") and progress:
                    progress.fail(entry["path"], entry["score"]["error"])
                if out:
                    out.write(json.dumps(entry) + "\n")
            if out:
                out.flush()
                os.fsync(out.fileno())
            if progress:
                progress.advance(len(batch))
    finally:
        if out:
            out.close()
    return [entries[str(path)] for path in paths]


def _scene_ev(probe: dict) -> float | None:
    """How bright the scene was, from the exposure the camera used (EV at ISO 100): steady under one lighting,
    a step between a bright stage and a dim corner."""
    shutter, aperture, iso = probe.get("shutter"), probe.get("aperture"), probe.get("iso")
    if not (shutter and aperture and iso):
        return None
    return round(math.log2(aperture * aperture / shutter) - math.log2(iso / 100), 2)


def frames_from(entries: list[dict]) -> list[dict]:
    """One dict per frame in capture order, with its measurements and the feature-print distance to the frame
    before it (Vision's distance is the plain Euclidean one between prints, so it's computed here and order-free)."""
    ordered = sorted(entries, key=lambda e: (e["probe"].get("captured") or "", Path(e["path"]).name))
    frames, previous = [], None
    for entry in ordered:
        probe, score = entry["probe"], entry["score"]
        faces = score.get("faces") or []
        main = faces[0] if faces else None
        captured = probe.get("captured")
        feature = np.frombuffer(base64.b64decode(entry["print"]), dtype=np.float32) if entry.get("print") else None
        distance = float(np.linalg.norm(feature - previous)) if feature is not None and previous is not None \
            and feature.shape == previous.shape else None
        previous = feature
        frames.append({
            "path": entry["path"], "name": Path(entry["path"]).name, "captured": captured,
            "aesthetics": score.get("aesthetics"), "utility": score.get("utility"),
            "sharpness": score.get("sharpness"), "peak_sharpness": score.get("peakSharpness"), "faces": len(faces),
            "face_quality": main.get("quality") if main else None,
            "eyes": min(main["eyes"]) if main and main.get("eyes") else None,
            "face_area": main["box"][2] * main["box"][3] if main else 0.0,
            "lightness": score.get("lightness"), "blown": score.get("blown"),
            "distance": round(distance, 5) if distance is not None else None, "error": score.get("error"),
            "iso": probe.get("iso"), "ev": _scene_ev(probe), "rating": probe.get("rating"),
            "camera": probe.get("camera"), "width": probe.get("width"), "height": probe.get("height"),
        })
    return frames


# Judging ------------------------------------------------------------------------------------------------------------

def _time(frame) -> dt.datetime | None:
    return dt.datetime.strptime(frame["captured"], "%Y-%m-%d %H:%M:%S.%f") if frame.get("captured") else None


def group(frames: list[dict], *, gap: float = 2.0, distance: float = 0.6, max_moment: float | None = None) -> int:
    """Numbers each frame's moment (frames in capture order); returns how many moments there are."""
    moment, first, previous = -1, None, None
    for frame in frames:
        when = _time(frame)
        new = previous is None
        if not new:
            seconds = (when - previous).total_seconds() if when and previous else math.inf
            alike = (frame["distance"] if frame["distance"] is not None else math.inf) <= distance
            too_long = max_moment is not None and when and first and (when - first).total_seconds() > max_moment
            new = not (seconds <= gap and alike) or bool(too_long)
        if new:
            moment += 1
            first = when
        frame["moment"] = moment
        previous = when
    return moment + 1


def judge(frames: list[dict]) -> None:
    """Flags and scores every frame, relative to this shoot (see the module notes)."""
    peaks = [f["peak_sharpness"] for f in frames if f["peak_sharpness"]]
    typical_peak = float(np.median(peaks)) if peaks else None
    light = [f["lightness"] for f in frames if f["lightness"] is not None]
    typical_light = float(np.median(light)) if light else None
    for frame in frames:
        flags = []
        if frame["error"]:
            flags.append("unreadable")
        # Nothing anywhere in focus (missed focus, motion blur); a deliberately shallow focus still has a sharp spot.
        if typical_peak and frame["peak_sharpness"] is not None and frame["peak_sharpness"] < 0.5 * typical_peak:
            flags.append("soft")
        if frame["eyes"] is not None and frame["face_area"] > 0.004 and frame["eyes"] < 0.12:
            flags.append("eyes closed")
        if typical_light is not None and frame["lightness"] is not None and abs(frame["lightness"] - typical_light) > 22:
            flags.append("too dark" if frame["lightness"] < typical_light else "too bright")
        frame["flags"] = flags
    sharp_rank = _rank([f["sharpness"] for f in frames])
    for frame, rank in zip(frames, sharp_rank):
        face = frame["face_quality"] if frame["face_quality"] is not None else 0.5
        eyes = 0.5 if frame["eyes"] is None else min(1.0, max(0.0, (frame["eyes"] - 0.08) / 0.17))
        aesthetics = 0.5 if frame["aesthetics"] is None else (frame["aesthetics"] + 1) / 2
        exposure = 0.5 if frame["lightness"] is None or typical_light is None else \
            1 - min(1.0, abs(frame["lightness"] - typical_light) / 30)
        value = 0.30 * (rank if rank is not None else 0.5) + 0.25 * face + 0.15 * eyes + 0.20 * aesthetics + 0.10 * exposure
        # Vision's "utility" (screenshot, document) is a hint, not a reason to reject: it also fires on a camera
        # photo of someone holding up a phone.
        frame["score"] = round(100 * value - 20 * len(frame["flags"]) - (8 if frame["utility"] else 0), 1)


def moments_of(frames: list[dict]) -> dict[int, list[dict]]:
    moments: dict[int, list[dict]] = {}
    for frame in frames:
        moments.setdefault(frame["moment"], []).append(frame)
    return moments


def effective_size(members: list[dict], tau: float) -> float:
    """How many frames' worth of moment this is: each frame after the first counts fully when it came at least `tau`
    seconds after the one before, and in proportion when sooner, so a 10-frames-a-second burst counts for far less than
    ten frames of a held pose shot a second apart."""
    if not tau:
        return float(len(members))
    size, previous = 1.0, _time(members[0])
    for frame in members[1:]:
        when = _time(frame)
        size += min(1.0, (when - previous).total_seconds() / tau) if when and previous else 1.0
        previous = when or previous
    return size


def parse_rate(text) -> float:
    """A keep rate from "35%", "35" or "0.35"."""
    value = float(str(text).strip().rstrip("%").strip())
    rate = value / 100 if str(text).strip().endswith("%") or value > 1 else value
    if not 0 < rate <= 1:
        raise CompError(f"a keep rate runs from 0 to 100%, not {text!r}")
    return rate


def assign(frames: list[dict], *, rate: float | None = None, count: int | None = None, rule: dict | None = None,
           per_moment: int | None = None, keep: int | None = None) -> dict:
    """Sets each frame's status: pick, alternate, or reject (flagged). Frames must be grouped and judged.

    Adaptive (`count` picks, or `rate` of all frames): each unflagged frame gets a priority and the `count` highest
    become picks. For the k-th best frame (k = 0, 1, …) of a moment of effective size n (see `effective_size`):

        priority = rate × n − k + beta × (its score's z-value) + (cover if k == 0)

    so a moment's frames are taken in proportion to its size (rate × n of them, the more frames shot the more
    picks), `cover` makes the first frame of each moment come before other moments' extra frames, and `beta` lets a
    clearly better frame move ahead of a weaker one. Equal priorities go to the higher score. `rule` holds tau,
    cover and beta (DEFAULT_RULE).

    Fixed (`per_moment`, the default when neither rate nor count is given): the best `per_moment` unflagged frames
    of each moment, and with `keep` only the best-scoring `keep` of those.
    Returns a summary: frames, moments, picks, alternates, rejects and the rule used."""
    moments = moments_of(frames)
    for frame in frames:
        frame.pop("priority", None)
    chosen: set[int] = set()
    if rate is None and count is None:
        per_moment = per_moment or 1
        picks = []
        for members in moments.values():
            good = sorted((f for f in members if not f["flags"]), key=lambda f: (-f["score"], f["name"]))
            picks += good[:per_moment]
        if keep is not None:
            picks = sorted(picks, key=lambda f: (-f["score"], f["name"]))[:keep]
        chosen = {id(f) for f in picks}
        used = {"per_moment": per_moment, **({"keep": keep} if keep is not None else {})}
    else:
        count = count if count is not None else round(rate * len(frames))
        share = count / max(1, len(frames))
        rule = {**DEFAULT_RULE, **(rule or {})}
        scores = np.array([f["score"] for f in frames if not f["flags"]] or [0.0])
        middle, spread = float(scores.mean()), float(scores.std()) or 1.0
        candidates = []
        for members in moments.values():
            size = effective_size(members, rule["tau"])
            good = sorted((f for f in members if not f["flags"]), key=lambda f: (-f["score"], f["name"]))
            for k, frame in enumerate(good):
                frame["priority"] = round(share * size - k + rule["beta"] * (frame["score"] - middle) / spread
                                          + (rule["cover"] if k == 0 else 0), 4)
                candidates.append(frame)
        # Equal priorities (say, every single-frame moment) go to the better-scoring frame, not the earlier one.
        candidates.sort(key=lambda f: (-f["priority"], -f["score"], f["captured"] or "", f["name"]))
        chosen = {id(f) for f in candidates[:count]}
        used = {"count": count, "rate": round(count / max(1, len(frames)), 4), "rule": rule}
    for frame in frames:
        frame["status"] = "pick" if id(frame) in chosen else ("reject" if frame["flags"] else "alternate")
    return {"frames": len(frames), "moments": len(moments), "picks": len(chosen),
            "alternates": sum(f["status"] == "alternate" for f in frames),
            "rejects": sum(f["status"] == "reject" for f in frames), **used}


def cull(inputs, *, gap: float = 2.0, distance: float = 0.6, max_moment: float | None = None,
         per_moment: int | None = None, keep: int | None = None, rate: float | None = None, count: int | None = None,
         rule: dict | None = None, size: int = 1024, cache=None, progress: Progress | None = None) -> list[dict]:
    """Scores, groups, judges and picks every picture under `inputs` (see the module notes and `assign`). With
    `cache` (a folder), scoring resumes and reruns from it. Returns one dict per frame in capture order: path, name,
    captured, moment, score, flags, status (pick / alternate / reject) and the measurements."""
    paths = gather(inputs)
    if not paths:
        raise CompError("no pictures found to cull")
    frames = frames_from(scan(paths, cache, size=size, progress=progress))
    group(frames, gap=gap, distance=distance, max_moment=max_moment)
    judge(frames)
    assign(frames, rate=rate, count=count, rule=rule, per_moment=per_moment, keep=keep)
    return frames


# Writing the results ---------------------------------------------------------------------------------------------

COLUMNS = ["name", "status", "score", "moment", "flags", "captured", "priority", "sharpness", "face_quality", "eyes",
           "aesthetics", "lightness", "blown", "faces", "iso", "ev", "camera", "path"]


def write_results(frames: list[dict], out, *, sheets: bool = True, sheet_moments: int = 24, thumb: int = 220,
                  settings: dict | None = None) -> dict:
    """cull.csv (every frame), picks.txt (paths of picks, for `compkit grade --list`), cull.json (every measurement,
    what `compkit pick` re-dials from) and, with `sheets`, contact sheets of the moments: each moment one row,
    picks outlined green, rejects red with their reasons. `settings` (how the cull was made) go to settings.json."""
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for frame in frames:
        row = {k: (round(frame[k], 3) if isinstance(frame.get(k), float) else frame.get(k)) for k in COLUMNS}
        rows.append({**row, "flags": "; ".join(frame["flags"])})
    _write_atomic(out / "cull.csv", _csv_text(rows))
    picks = [f["path"] for f in frames if f["status"] == "pick"]
    _write_atomic(out / "picks.txt", "\n".join(picks) + ("\n" if picks else ""))
    _write_atomic(out / "cull.json", json.dumps(frames, indent=1))
    if settings is not None:
        _write_atomic(out / "settings.json", json.dumps(settings, indent=2))
    summary = {"frames": len(frames), "moments": len({f["moment"] for f in frames}), "picks": len(picks),
               "rate": round(len(picks) / max(1, len(frames)), 3),
               "alternates": sum(f["status"] == "alternate" for f in frames),
               "rejects": sum(f["status"] == "reject" for f in frames), "out": str(out)}
    if sheets:
        summary["sheets"] = _contact_sheets(frames, out / "sheets", sheet_moments, thumb)
    return summary


def _csv_text(rows: list[dict]) -> str:
    import io
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue()


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def _contact_sheets(frames, folder: Path, per_sheet: int, thumb: int) -> int:
    """Pages of moments. Thumbnails are kept in .previews and only missing ones are made, so a re-dial redraws the
    pages in seconds; pages from an earlier, longer run are removed."""
    folder.mkdir(parents=True, exist_ok=True)
    previews = folder / ".previews"
    missing = [Path(f["path"]) for f in frames if not (previews / f"{Path(f['path']).stem}.jpg").exists()]
    if missing:
        _run_listed("previews", missing, "--out", str(previews), "--size", str(thumb))
    moments = moments_of(frames)
    keys = sorted(moments)
    columns = min(max(len(m) for m in moments.values()), 8)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except OSError:
        font = ImageFont.load_default()

    def page(number: int, chunk: list[int]) -> None:
        rows = sum(math.ceil(len(moments[k]) / columns) for k in chunk)
        cell, label, pad = thumb, 18, 6
        sheet = Image.new("RGB", (100 + columns * (cell + pad) + pad, rows * (cell + label + pad) + pad), "#1A1A1A")
        draw = ImageDraw.Draw(sheet)
        y = pad
        for key in chunk:
            members = moments[key]
            picked = sum(f["status"] == "pick" for f in members)
            draw.text((8, y + 4), f"#{key}", fill="#BBBBBB", font=font)
            draw.text((8, y + 22), f"{picked} of {len(members)}", fill="#3DFF6E" if picked else "#888888", font=font)
            for index, frame in enumerate(members):
                if index and index % columns == 0:
                    y += cell + label + pad
                x = 100 + pad + (index % columns) * (cell + pad)
                path = previews / (Path(frame["path"]).stem + ".jpg")
                if path.exists():
                    with Image.open(path) as image:
                        image.thumbnail((cell, cell))
                        sheet.paste(image.convert("RGB"), (x + (cell - image.width) // 2, y + (cell - image.height) // 2))
                color = {"pick": "#3DFF6E", "reject": "#FF4D4D"}.get(frame["status"])
                if color:
                    draw.rectangle([x - 2, y - 2, x + cell + 1, y + cell + 1], outline=color, width=3)
                note = f"{frame['name'][:14]} {frame['score']:.0f}" + (f" · {', '.join(frame['flags'])}" if frame["flags"] else "")
                draw.text((x, y + cell + 2), note[:34], fill=color or "#BBBBBB", font=font)
            y += cell + label + pad
        temporary = folder / f".moments-{number:03d}.tmp.jpg"
        sheet.save(temporary, quality=82)
        os.replace(temporary, folder / f"moments-{number:03d}.jpg")

    chunks = [keys[start:start + per_sheet] for start in range(0, len(keys), per_sheet)]
    with ThreadPoolExecutor(min(8, os.cpu_count() or 4)) as pool:
        list(pool.map(lambda job: page(*job), enumerate(chunks, start=1)))
    for old in folder.glob("moments-*.jpg"):
        if int(old.stem.split("-")[1]) > len(chunks):
            old.unlink()
    return len(chunks)


def load_results(out) -> tuple[list[dict], dict]:
    """A cull's frames and settings, as write_results left them, for re-dialing without scanning again."""
    out = Path(out).expanduser()
    if not (out / "cull.json").exists():
        raise CompError(f"{out} holds no cull (cull.json); run `compkit cull` first")
    frames = json.loads((out / "cull.json").read_text())
    settings = json.loads((out / "settings.json").read_text()) if (out / "settings.json").exists() else {}
    return frames, settings


def write_ratings(frames: list[dict], *, pick: int = 3, alternate: int = 1) -> dict:
    """Lightroom ratings as XMP sidecars beside each RAW (picks `pick` stars, alternates `alternate`, rejects
    flagged as rejected), so the cull shows up when the folder is imported. A RAW that already has a sidecar is
    left alone, since that sidecar may hold Lightroom edits."""
    written = skipped = 0
    for frame in frames:
        path = Path(frame["path"])
        if path.suffix.lower() not in RAW_SUFFIXES:
            continue
        sidecar = path.with_suffix(".xmp")
        if sidecar.exists():
            skipped += 1
            continue
        rating = {"pick": pick, "alternate": alternate, "reject": -1}[frame["status"]]
        sidecar.write_text(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            f'  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="{rating}"/>\n'
            ' </rdf:RDF>\n</x:xmpmeta>\n')
        written += 1
    return {"written": written, "skipped (sidecar already there)": skipped}
