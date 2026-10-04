"""Culling a shoot: from thousands of frames to the ones worth grading and delivering.

Every frame is read from its embedded preview (RAWs aren't decoded) by `comp-render probe` and `comp-render score`,
so a few thousand frames take a minute or two. Frames are then:

1. grouped into moments: consecutive frames shot within `gap` seconds of each other that look alike (Vision feature
   prints closer than `distance`), as a burst or a held pose is;
2. flagged where something is clearly wrong: nothing anywhere in focus (well under the shoot's typical sharpest
   spot, so a deliberately shallow focus isn't flagged), the main face's eyes shut, the frame far darker or brighter than the shoot, a utility shot;
3. scored 0–100 within the shoot: subject sharpness, face quality, eyes open, Vision's aesthetic score, exposure;
4. picked: the best `per_moment` unflagged frames of each moment ("pick"), the rest of the moment kept as
   "alternate", flagged frames "reject". With `keep`, only the best `keep` picks stay picks.

What it can't judge is taste: which of several good frames of a moment shows the better expression or pose. That is
left to the photographer, with the alternates beside each pick on the contact sheets.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import RAW_SUFFIXES, CompError, comp_render

PICTURE_SUFFIXES = RAW_SUFFIXES | {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png"}


def gather(inputs) -> list[Path]:
    """Pictures under the given folders (recursively) and files, without Lightroom exports' duplicates of a RAW:
    a JPEG beside a RAW of the same name is skipped."""
    found = []
    for item in inputs:
        item = Path(item).expanduser()
        found += [p for p in item.rglob("*") if p.suffix.lower() in PICTURE_SUFFIXES] if item.is_dir() else [item]
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


def cull(inputs, *, gap: float = 2.0, distance: float = 0.6, per_moment: int = 1, keep: int | None = None,
         size: int = 1024) -> list[dict]:
    """Scores, groups and picks every picture under `inputs` (see the module notes). Returns one dict per frame in
    capture order: path, name, captured, moment, score, flags, status (pick / alternate / reject) and the measurements."""
    paths = gather(inputs)
    if not paths:
        raise CompError("no pictures found to cull")
    info = {r["path"]: r for r in json.loads(_run_listed("probe", paths))}
    order = sorted(paths, key=lambda p: (info.get(str(p), {}).get("captured") or "", p.name))
    scores = json.loads(_run_listed("score", order, "--size", str(size)))
    frames = []
    for path, score in zip(order, scores):
        meta = info.get(str(path), {})
        faces = score.get("faces") or []
        main = faces[0] if faces else None
        captured = meta.get("captured")
        frames.append({
            "path": str(path), "name": path.name, "captured": captured,
            "time": dt.datetime.strptime(captured, "%Y-%m-%d %H:%M:%S.%f") if captured else None,
            "aesthetics": score.get("aesthetics"), "utility": score.get("utility"),
            "sharpness": score.get("sharpness"), "peak_sharpness": score.get("peakSharpness"), "faces": len(faces),
            "face_quality": main.get("quality") if main else None,
            "eyes": min(main["eyes"]) if main and main.get("eyes") else None,
            "face_area": main["box"][2] * main["box"][3] if main else 0.0,
            "lightness": score.get("lightness"), "blown": score.get("blown"),
            "distance": score.get("distanceToPrevious"), "error": score.get("error"),
            "iso": meta.get("iso"), "rating": meta.get("rating"),
        })

    # Moments.
    moment = 0
    for index, frame in enumerate(frames):
        if index:
            previous = frames[index - 1]
            seconds = (frame["time"] - previous["time"]).total_seconds() if frame["time"] and previous["time"] else math.inf
            if not (seconds <= gap and (frame["distance"] if frame["distance"] is not None else math.inf) <= distance):
                moment += 1
        frame["moment"] = moment

    # Flags, relative to this shoot.
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

    # Scores, 0–100 within the shoot.
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

    # Picks.
    by_moment: dict[int, list[dict]] = {}
    for frame in frames:
        by_moment.setdefault(frame["moment"], []).append(frame)
    picks = []
    for members in by_moment.values():
        ranked = sorted(members, key=lambda f: -f["score"])
        good = [f for f in ranked if not f["flags"]]
        chosen = good[:per_moment]
        for frame in members:
            frame["status"] = "pick" if frame in chosen else ("reject" if frame["flags"] else "alternate")
        picks += chosen
    if keep is not None and len(picks) > keep:
        for frame in sorted(picks, key=lambda f: -f["score"])[keep:]:
            frame["status"] = "alternate"
    for frame in frames:
        frame.pop("time", None)
    return frames


def write_results(frames: list[dict], out, *, sheets: bool = True, sheet_moments: int = 24, thumb: int = 220) -> dict:
    """cull.csv (every frame), picks.txt (paths of picks, for `compkit grade --list`) and, with `sheets`, contact
    sheets of the moments: each moment one row, picks outlined green, rejects red with their reasons."""
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    columns = ["name", "status", "score", "moment", "flags", "captured", "sharpness", "face_quality", "eyes", "aesthetics",
               "lightness", "blown", "faces", "iso", "path"]
    with open(out / "cull.csv", "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for frame in frames:
            writer.writerow({**frame, "flags": "; ".join(frame["flags"]),
                             **{k: (round(frame[k], 3) if isinstance(frame.get(k), float) else frame.get(k)) for k in columns if k != "flags"}})
    picks = [f["path"] for f in frames if f["status"] == "pick"]
    (out / "picks.txt").write_text("\n".join(picks) + ("\n" if picks else ""))
    (out / "cull.json").write_text(json.dumps(frames, indent=1))
    summary = {"frames": len(frames), "moments": len({f["moment"] for f in frames}), "picks": len(picks),
               "alternates": sum(f["status"] == "alternate" for f in frames),
               "rejects": sum(f["status"] == "reject" for f in frames), "out": str(out)}
    if sheets:
        summary["sheets"] = _contact_sheets(frames, out / "sheets", sheet_moments, thumb)
    return summary


def _contact_sheets(frames, folder: Path, per_sheet: int, thumb: int) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    previews = folder / ".previews"
    _run_listed("previews", [Path(f["path"]) for f in frames], "--out", str(previews), "--size", str(thumb))
    moments: dict[int, list[dict]] = {}
    for frame in frames:
        moments.setdefault(frame["moment"], []).append(frame)
    keys = sorted(moments)
    width = max(len(m) for m in moments.values())
    columns = min(width, 8)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except OSError:
        font = ImageFont.load_default()
    pages = 0
    for start in range(0, len(keys), per_sheet):
        chunk = keys[start:start + per_sheet]
        rows = sum(math.ceil(len(moments[k]) / columns) for k in chunk)
        cell, label, pad = thumb, 18, 6
        page = Image.new("RGB", (90 + columns * (cell + pad) + pad, rows * (cell + label + pad) + pad), "#1A1A1A")
        draw = ImageDraw.Draw(page)
        y = pad
        for key in chunk:
            members = moments[key]
            draw.text((8, y + 4), f"#{key}", fill="#888888", font=font)
            for index, frame in enumerate(members):
                if index and index % columns == 0:
                    y += cell + label + pad
                x = 90 + pad + (index % columns) * (cell + pad)
                path = previews / (Path(frame["path"]).stem + ".jpg")
                if path.exists():
                    with Image.open(path) as image:
                        image.thumbnail((cell, cell))
                        page.paste(image.convert("RGB"), (x + (cell - image.width) // 2, y + (cell - image.height) // 2))
                color = {"pick": "#3DFF6E", "reject": "#FF4D4D"}.get(frame["status"])
                if color:
                    draw.rectangle([x - 2, y - 2, x + cell + 1, y + cell + 1], outline=color, width=3)
                note = f"{frame['name'][:14]} {frame['score']:.0f}" + (f" · {', '.join(frame['flags'])}" if frame["flags"] else "")
                draw.text((x, y + cell + 2), note[:34], fill=color or "#BBBBBB", font=font)
            y += cell + label + pad
        pages += 1
        page.save(folder / f"moments-{pages:03d}.jpg", quality=82)
    return pages


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
