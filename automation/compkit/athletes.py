"""Who is in each frame at a competition, so that every athlete gets enough photos of their own.

The cull picks by moment, not by athlete: it can't tell who is who, so along a lineup some athletes get many frames
and some few. Here a frame where one person stands alone (`comp-render hips`: one Vision people box, no one else at
half their height) is matched to an athlete by the number on their hip, read by Apple Vision, and named from the
show's run sheet. A read carries to the frames just before and after it from the same camera, up to 0.6 s apart with
the person in the same place and at the same size: one athlete's burst. It carries no further, because along a
lineup the photographer moves to the next athlete about every second (carried up to 1 s, 88% were right; up to
0.6 s, 98.7%). A number read nowhere else within five minutes names no one, since most such reads are a misread
digit (it still counts as read, for the list of numbers never read).

`balance` then promotes the best-scoring solo alternates of any athlete short of the target, one per moment, and
athletes.csv lists every athlete's solo picks. Only frames whose number was read count (at ICN Nationals 2026, 41%
of solo frames, 99.4% of them right), so an athlete's real coverage is usually well above what the table says.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import COMP_RENDER, CompError, Stopped, comp_render
from .cull import _csv_text, _key, _run_listed, _time, _write_atomic
from .runlog import Progress

# What a cached read was made with; bump when `comp-render hips` output changes.
HIPS_VERSION = 1
# shortcut: one-digit numbers are never read (too easily a fragment of a longer one); allow them if a show uses them.
NUMBER = re.compile(r"\d{2,3}")
CARRY = 0.6  # seconds
CONFIRM = 300  # seconds


def runsheet(path) -> dict[int, dict]:
    """{number: {"name", "divisions"}} from a CSV with number, name and (optionally) divisions columns, or JSON: a list
    of objects with those keys. A number listed in several divisions under the same name is one entry."""
    path = Path(path).expanduser()
    try:
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text())
        else:
            with open(path, newline="", encoding="utf-8-sig") as file:
                rows = list(csv.DictReader(file))
    except (OSError, ValueError) as error:
        raise CompError(f"can't read the run sheet {path}: {error}")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise CompError(f"{path}: a run sheet is a list of competitors (number, name, divisions)")
    sheet: dict[int, dict] = {}
    for row in rows:
        row = {str(k).strip().lower(): v for k, v in row.items()}
        try:
            number, name = int(str(row["number"]).strip()), " ".join(str(row["name"]).split())
        except (KeyError, ValueError):
            raise CompError(f"{path}: every competitor needs a number and a name, not {row}")
        divisions = row.get("divisions") or []
        divisions = [d.strip() for d in (divisions if isinstance(divisions, list) else str(divisions).split(";")) if d.strip()]
        entry = sheet.setdefault(number, {"name": name, "divisions": []})
        if entry["name"].casefold() != name.casefold():
            raise CompError(f"{path}: number {number} is both {entry['name']} and {name}")
        entry["divisions"] += [d for d in divisions if d not in entry["divisions"]]
    if not sheet:
        raise CompError(f"{path}: the run sheet lists no one")
    return sheet


def _hips_batch(batch: list[Path]) -> list[dict]:
    """`comp-render hips` on a batch; if it fails, halves until the file it fails on is alone and marks that one."""
    try:
        reads = json.loads(_run_listed("hips", batch))
        if len(reads) == len(batch):
            return reads
        raise ValueError("comp-render hips returned the wrong number of frames")
    except Stopped:
        raise
    except (CompError, ValueError) as error:
        if len(batch) == 1:
            return [{"error": (str(error).strip().splitlines() or ["comp-render failed"])[0]}]
        half = len(batch) // 2
        return _hips_batch(batch[:half]) + _hips_batch(batch[half:])


def read_hips(paths: list[Path], cache=None, *, chunk: int = 120, progress: Progress | None = None) -> dict[str, dict]:
    """{path: `comp-render hips` result} for every path, kept in cache/hips.jsonl as the cull keeps its scores: a frame
    already read (same size and modification time) isn't read again. A frame that fails is retried on the next run."""
    cache_file = Path(cache).expanduser() / "hips.jsonl" if cache is not None else None
    cached: dict[str, dict] = {}
    if cache_file and cache_file.exists():
        with open(cache_file) as file:
            for line in file:
                try:
                    entry = json.loads(line)
                except ValueError:  # the last line of a run that was stopped mid-write
                    continue
                cached[entry["path"]] = entry
    found, todo = {}, []
    for path in paths:
        entry = cached.get(str(path))
        if entry and entry.get("version") == HIPS_VERSION and entry.get("key") == _key(path):
            found[str(path)] = entry
        else:
            todo.append(path)
    if todo and "comp-render hips" not in comp_render("help"):  # else every frame would fail, one by one
        raise CompError(f"{COMP_RENDER} can't read hip numbers yet: run automation/comp-render/build.sh")
    if progress:
        progress.start(len(paths), done=len(paths) - len(todo))
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
    out = open(cache_file, "a") if cache_file else None
    try:
        for start in range(0, len(todo), chunk):
            batch = todo[start:start + chunk]
            for path, read in zip(batch, _hips_batch(batch)):
                entry = {"version": HIPS_VERSION, "path": str(path), "key": _key(path), "people": read.get("people") or [],
                         "solo": bool(read.get("solo")), "text": read.get("text") or []}
                found[str(path)] = entry
                if read.get("error"):
                    if progress:
                        progress.fail(path, read["error"])
                elif out:
                    out.write(json.dumps(entry) + "\n")
            if out:
                out.flush()
            if progress:
                progress.advance(len(batch))
    finally:
        if out:
            out.close()
    return found


def identify(frames: list[dict], hips: dict[str, dict], sheet: dict[int, dict]) -> None:
    """Sets each frame's `solo`, `hip` (the run-sheet number read on it, if one was), `athlete` (a run-sheet number,
    or None) and `athlete_from` ("hip" where its read was confirmed, "burst" where it was carried from a neighbouring
    frame). Frames must be in capture order."""
    mains, numbers = [], []
    for frame in frames:
        read = hips.get(frame["path"]) or {}
        frame["solo"] = bool(read.get("solo"))
        mains.append((read.get("people") or [None])[0])
        found = {int(t["text"].strip()) for t in read.get("text") or []
                 if t["confidence"] >= 0.5 and NUMBER.fullmatch(t["text"].strip())} & sheet.keys()
        numbers.append(found)
        frame["hip"] = frame["athlete"] = next(iter(found)) if frame["solo"] and len(found) == 1 else None
        frame["athlete_from"] = "hip" if frame["athlete"] is not None else None
    times = [_time(frame) for frame in frames]
    # A number read nowhere else within five minutes is most likely a misread digit (at ICN Nationals 32 of 49 wrong
    # reads were, and 113 of 3,114 right ones).
    seen: dict[int, list] = {}
    for frame, when in zip(frames, times):
        if frame["athlete"] is not None and when:
            seen.setdefault(frame["athlete"], []).append(when)
    for frame, when in zip(frames, times):
        if frame["athlete"] is not None and (
                not when or sum(abs((other - when).total_seconds()) <= CONFIRM for other in seen[frame["athlete"]]) < 2):
            frame["athlete"] = frame["athlete_from"] = None

    def one_burst(a: int, b: int) -> bool:  # both alone, close in time, the person in the same place at the same size
        if not (frames[a]["solo"] and frames[b]["solo"] and times[a] and times[b] and mains[a] and mains[b]):
            return False
        (ax, _, aw, ah, _), (bx, _, bw, bh, _) = mains[a], mains[b]
        return abs((times[a] - times[b]).total_seconds()) <= CARRY and abs((ax + aw / 2) - (bx + bw / 2)) <= 0.05 \
            and abs(ah - bh) <= 0.1 * ah

    cameras: dict[str | None, list[int]] = {}
    for index, frame in enumerate(frames):
        # shortcut: two bodies of the same model share a timeline here; tell them apart by serial if a show has two.
        cameras.setdefault(frame.get("camera"), []).append(index)
    for line in cameras.values():
        for order in (line, line[::-1]):
            for a, b in zip(order, order[1:]):
                if frames[b]["athlete"] is None and frames[a]["athlete"] is not None and not numbers[b] and one_burst(a, b):
                    frames[b]["athlete"], frames[b]["athlete_from"] = frames[a]["athlete"], "burst"


def _people(sheet: dict[int, dict]) -> dict[str, list[int]]:
    """Each athlete's numbers: someone entered in several divisions can wear a different number in each."""
    people: dict[str, list[int]] = {}
    for number in sorted(sheet):
        people.setdefault(sheet[number]["name"].casefold(), []).append(number)
    return people


def _solo_frames(frames: list[dict], sheet: dict[int, dict]) -> dict[str, list[dict]]:
    owner = {number: person for person, numbers in _people(sheet).items() for number in numbers}
    own: dict[str, list[dict]] = {person: [] for person in _people(sheet)}
    for frame in frames:
        if frame.get("solo") and frame.get("athlete") in owner:
            own[owner[frame["athlete"]]].append(frame)
    return own


def balance(frames: list[dict], sheet: dict[int, dict], target: int, *, fixed=()) -> list[dict]:
    """Picks the best-scoring solo alternates of every athlete with fewer than `target` solo picks, at most one per
    moment, until they have `target` or run out. Frames whose path is in `fixed` (set by hand) are left alone.
    Returns the frames promoted."""
    picked = {frame["moment"] for frame in frames if frame["status"] == "pick"}
    promoted = []
    for own in _solo_frames(frames, sheet).values():
        short = target - sum(frame["status"] == "pick" for frame in own)
        # Moments with no pick yet first: another frame of a moment already picked is nearly the same photo.
        spare = sorted((f for f in own if f["status"] == "alternate" and f["path"] not in fixed),
                       key=lambda f: (f["moment"] in picked, -f["score"], f["name"]))
        used = set()
        for frame in spare:
            if short <= 0:
                break
            if frame["moment"] not in used:
                used.add(frame["moment"])
                frame["status"] = "pick"
                promoted.append(frame)
                short -= 1
    return promoted


def write_table(frames: list[dict], sheet: dict[int, dict], out, target: int | None = None) -> dict:
    """athletes.csv in `out`: every athlete on the run sheet with their solo picks, solo alternates and how many picks
    `balance` promoted. Returns who is still under `target` and the numbers never read (most likely scratched)."""
    out = Path(out).expanduser()
    own, people = _solo_frames(frames, sheet), _people(sheet)
    read = {frame.get("hip") for frame in frames}
    rows, under = [], []
    for person, numbers in people.items():
        picks = sum(frame["status"] == "pick" for frame in own[person])
        rows.append({"number": " ".join(map(str, numbers)), "name": sheet[numbers[0]]["name"],
                     "divisions": "; ".join(d for n in numbers for d in sheet[n]["divisions"]), "solo picks": picks,
                     "solo alternates": sum(frame["status"] == "alternate" for frame in own[person]),
                     "promoted": sum(frame["status"] == "pick" and bool(frame.get("promoted")) for frame in own[person])})
        if target and picks < target and read & set(numbers):
            under.append(f"{rows[-1]['number']} {rows[-1]['name']}: {picks}")
    columns = ["number", "name", "divisions", "solo picks", "solo alternates", "promoted"]
    _write_atomic(out / "athletes.csv", _csv_text(rows, columns))
    solo = [frame for frame in frames if frame.get("solo")]
    return {"athletes": len(people), "solo frames": len(solo),
            "identified": sum(frame.get("athlete") is not None for frame in solo),
            "promoted": sum(row["promoted"] for row in rows),
            **({f"under {target} solo picks": under} if target else {}),
            "never read (scratched?)": sorted(set(sheet) - read), "table": str(out / "athletes.csv")}
