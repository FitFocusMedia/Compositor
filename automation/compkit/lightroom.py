"""Editing a show in Lightroom Classic through the RAWs' XMP sidecars, the routine of the compositor-lightroom skill
(which has the person's side of it and what each step measured on ICN Nationals 2026: 13,688 Canon frames, 6,236
delivered). Lightroom can't be driven from outside, so settings go into the top-level attributes of each RAW's
sidecar and the person reads them in with Metadata › Read Metadata from Files. The catalogue is the truth: it is
read only from a copy, and a sidecar is written only while it matches the catalogue, so reading it in changes
nothing but the planned settings.

- Catalogue: `copy_catalogue`, `photos`, `history`, `hand_made`, `step_settings`, `top`.
- Sidecars: `attrs`, `set_attrs`, `mismatches`, `apply` (back up, write, read back), `read_in` (after their read).
- Framing: `rule_crop`, `camera_tilts`, `level`, `fit`, `crop_settings`.
- Blue stage light: `blueness`, `fit_temperature`, `stage_temperature`.
- Before upload: `blank_corners` (Upright's blank wedges in the exported JPEGs).
- Delivery: `match_exports`, `delivery_plan`, `organise`, `embedded`, `replace`.
"""

from __future__ import annotations

import csv
import io
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from . import CompError
from .show import _clean

# Catalogue ----------------------------------------------------------------------------------------------------------

EPOCH_2001 = 978307200  # history steps count seconds from 2001-01-01
# Adobe_images.orientation as EXIF orientation: AB upright, DA portrait (EXIF 8, most stage frames).
ORIENTATION = {"AB": 1, "BC": 6, "CD": 3, "DA": 8}


def copy_catalogue(lrcat, folder) -> sqlite3.Connection:
    """A read-only connection to a fresh copy of the catalogue `lrcat` in `folder`, with its -wal file, which holds
    the latest edits while Lightroom is open. The live catalogue is never opened."""
    lrcat, folder = Path(lrcat).expanduser().resolve(), Path(folder).expanduser().resolve()
    copy = folder / lrcat.name
    if copy == lrcat:
        raise CompError("copy the catalogue into another folder than its own")
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("-shm", "-wal"):
        Path(f"{copy}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(lrcat, copy)
    if Path(f"{lrcat}-wal").exists():
        shutil.copyfile(f"{lrcat}-wal", f"{copy}-wal")
    return sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)


_PHOTOS = """
select i.id_local, rf.absolutePath || coalesce(fo.pathFromRoot, '') || f.baseName, f.extension, coalesce(i.rating, 0),
       i.masterImage, i.orientation, i.captureTime, s.text
from Adobe_images i
join AgLibraryFile f on f.id_local = i.rootFile
join AgLibraryFolder fo on fo.id_local = f.folder
join AgLibraryRootFolder rf on rf.id_local = fo.rootFolder
left join Adobe_imageDevelopSettings s on s.image = i.id_local"""


def photos(db) -> dict[int, dict]:
    """Every photo in the catalogue by id: its RAW `path`, `xmp` sidecar, `rating`, `master` (set on a virtual copy,
    which shares its master's sidecar), EXIF `orientation`, `captured` and top-level develop `settings`. A photo's
    division is its folder's name (`pathFromRoot` is empty when the root folder is the division)."""
    return {i: {"id": i, "path": f"{stem}.{extension}", "xmp": f"{stem}.xmp", "rating": rating, "master": master,
                "orientation": ORIENTATION.get(orientation), "captured": captured, "settings": top(text)}
            for i, stem, extension, rating, master, orientation, captured, text in db.execute(_PHOTOS)}


def history(db) -> dict[int, list[tuple[float, str]]]:
    """Each photo's develop history, oldest first, as (unix time, step name)."""
    steps = defaultdict(list)
    for image, when, name in db.execute("select image, dateCreated, name from Adobe_libraryImageDevelopHistoryStep "
                                        "order by dateCreated, id_local"):
        steps[image].append((when + EPOCH_2001, name))
    return steps


def hand_made(name: str) -> bool:
    """Whether a history step is the person's own edit: not an import, a read from the sidecar, a sync or an export."""
    return not (name.startswith(("Import", "Export")) or name in ("From Metadata", "Synchronize Settings"))


def step_settings(db, image: int) -> list[tuple[float, str, dict]]:
    """One photo's history with the top-level settings after each step: (unix time, name, settings). Lightroom keeps
    a step's settings as a 4-byte length and zlib text (snapshots add agHistoryOrientation)."""
    unpack = lambda t: zlib.decompress(t[4:]).decode("utf-8", "replace") if isinstance(t, bytes) else t or ""
    return [(when + EPOCH_2001, name, top(unpack(text))) for when, name, text in db.execute(
        "select dateCreated, name, text from Adobe_libraryImageDevelopHistoryStep where image = ? "
        "order by dateCreated, id_local", (image,))]


def top(text) -> dict[str, str]:
    """The top-level keys of Lightroom's develop settings (`s = { Exposure2012 = 0.25, ... }`), values as text.
    Only the top level: nested Preset and Look blocks repeat Exposure2012, Temperature, PerspectiveUpright and
    more with the preset's values, so a flat search reads those and finds changes that aren't there."""
    t = (text or "").strip()
    t = t[5:] if t.startswith("s = {") else t
    t = t[:-1] if t.endswith("}") else t
    out, depth, quoted, start = {}, 0, False, 0
    for m in re.finditer(r'\\.|["{},]', t):
        ch = m.group()
        if quoted:
            quoted = ch != '"'  # an escape is two characters, so never the closing quote
        elif ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            _put(out, t[start:m.start()])
            start = m.end()
    _put(out, t[start:])
    return out


def _put(out: dict, item: str) -> None:
    key, _, value = item.partition("=")
    if key.strip():
        out[key.strip()] = value.strip()


# Sidecars -----------------------------------------------------------------------------------------------------------

# The photo's own settings are the attributes of the first rdf:Description tag (quotes respected: a value may hold >).
_DESCRIPTION = re.compile(r'<rdf:Description(?:\s+[^\s=>/]+\s*=\s*"[^"]*")*\s*/?>')
# Fields a sidecar and the catalogue may disagree on without changing the photo: names, digests, versions, bookkeeping.
IGNORED = re.compile(r"^(Upright\w*|RawFileName|Version|CompatibleVersion|ProcessVersion|HasSettings|AlreadyApplied|"
                     r"CameraProfileDigest|LookTable|CameraProfile|Look\w*|.*Digest|ToneCurveName\w*|"
                     r"OverrideLookVignette|HDREditMode|SupportsAmount\w*|PresetType|UUID|Cluster|Copyright|Group|"
                     r"Name|ShortName|SortName|ContactInfo)$")
# Compared even where one side leaves them out (an untouched sidecar has no crop), at Lightroom's defaults.
DEFAULTS = {"Temperature": 0, "Tint": 0, "Exposure2012": 0, "Dehaze": 0, "PerspectiveUpright": 0,
            "PerspectiveRotate": 0, "CropLeft": 0, "CropTop": 0, "CropRight": 1, "CropBottom": 1, "CropAngle": 0}


def _head(sidecar: str) -> tuple[int, int]:
    m = _DESCRIPTION.search(sidecar)
    if not m:
        raise ValueError("no rdf:Description tag")
    return m.start(), m.end() - 1


def attrs(sidecar: str, ns: str = "crs") -> dict[str, str]:
    """The photo's own `ns` attributes in a sidecar's text (the first rdf:Description tag only)."""
    i, j = _head(sidecar)
    return dict(re.findall(rf'\b{ns}:(\w+)="([^"]*)"', sidecar[i:j]))


def _all_attrs(sidecar: str) -> dict[str, str]:
    i, j = _head(sidecar)
    return dict(re.findall(r'([\w-]+:\w+)="([^"]*)"', sidecar[i:j]))


def set_attrs(sidecar: str, new: dict) -> str:
    """`sidecar` with attributes of its first rdf:Description tag set: {"Temperature": 4200, "xmp:Label": "Red"} (crs
    unless a key names its namespace). One the tag lacks goes in after crs:HasSettings="True". Nested crs:Preset and
    crs:Look blocks are never touched. ValueError when the tag can't take a value as it is."""
    i, j = _head(sidecar)
    head = sidecar[i:j]
    for key, value in new.items():
        ns, _, name = key.rpartition(":")
        ns, value = ns or "crs", str(value)
        if not re.fullmatch(r"\w+", name) or re.search(r'["<&]', value) or f"xmlns:{ns}=" not in head:
            raise ValueError(f"can't set {ns}:{name}={value!r} in this sidecar")
        attribute = f'{ns}:{name}="{value}"'
        head, n = re.subn(rf'\b{ns}:{name}="[^"]*"', lambda _: attribute, head, count=1)
        if not n:
            if 'crs:HasSettings="True"' not in head:
                raise ValueError(f"no top-level {ns}:{name} and no crs:HasSettings to add it after")
            head = head.replace('crs:HasSettings="True"', f'crs:HasSettings="True"\n   {attribute}', 1)
    return sidecar[:i] + head + sidecar[j:]


def _value(v):
    v = str(v).strip().strip('"')
    if v.lower() in ("true", "false"):
        return v.lower()
    try:
        return round(float(v.lstrip("+")), 4)
    except ValueError:
        return v


def mismatches(sidecar: str, settings: dict) -> list[str]:
    """The numeric top-level fields where a sidecar's text and the catalogue's `settings` (`top()`, from a fresh
    copy) disagree: reading that sidecar in would change the photo. Empty means it's safe to write into."""
    found = attrs(sidecar)
    out = [k for k, v in found.items() if k in settings and not IGNORED.match(k)
           and isinstance(_value(settings[k]), float) and _value(v) != _value(settings[k])]
    return out + [k for k, default in DEFAULTS.items()
                  if k not in out and _value(found.get(k, default)) != _value(settings.get(k, default))]


def apply(plan: list[dict], backup, *, write: bool = False, rebuild_from=None) -> dict:
    """Writes planned settings into sidecars. `plan`: [{"xmp": sidecar, "settings": the photo's top-level settings
    from a fresh catalogue copy, "set": {attribute: value}}], master photos only. A sidecar that doesn't match the
    catalogue is skipped, unless `rebuild_from` (an earlier backup zip from here) holds a copy of it that does: that
    copy is then the base. With `write`, every sidecar is first backed up into the new zip `backup` (paths absolute
    minus the leading /), then written, read back and checked that only the planned attributes changed, and the `._`
    files macOS made beside them on exFAT (1 MB each) are removed. Without it nothing is written."""
    sidecars = [str(p["xmp"]) for p in plan]
    if len(set(sidecars)) != len(sidecars):
        raise CompError("two planned photos share a sidecar (a virtual copy?): plan master photos only")
    old = zipfile.ZipFile(rebuild_from) if rebuild_from else None
    ready, skipped, rebuilt = [], [], 0
    for p in plan:
        x = Path(p["xmp"])
        try:
            current = x.read_bytes().decode("utf-8")
            base, wrong = current, mismatches(current, p["settings"])
            name = str(x).lstrip("/")
            if wrong and old and name in old.NameToInfo:
                copy = old.read(name).decode("utf-8")
                if not mismatches(copy, p["settings"]):
                    base, wrong, rebuilt = copy, [], rebuilt + 1
            if wrong:
                skipped.append((str(x), "differs from the catalogue: " + ", ".join(wrong[:4])))
                continue
            ready.append((x, current, base, set_attrs(base, p["set"]), p["set"]))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            skipped.append((str(x), str(error)))
    report = {"ready": len(ready), "rebuilt from the backup": rebuilt, "skipped": skipped}
    if not write or not ready:
        return report
    backup = Path(backup)
    with zipfile.ZipFile(backup, "x", zipfile.ZIP_DEFLATED) as z:
        for x, current, *_ in ready:
            z.writestr(str(x).lstrip("/"), current)
    with zipfile.ZipFile(backup) as z:
        if len(z.namelist()) != len(ready):
            raise CompError(f"the backup {backup} is incomplete: nothing written")
    verified, failed, removed = 0, [], 0
    for x, current, base, out, new in ready:
        appledouble = x.with_name("._" + x.name)
        had = appledouble.exists()
        x.write_bytes(out.encode("utf-8"))
        back = x.read_bytes().decode("utf-8")
        before, after = _all_attrs(base), _all_attrs(back)
        changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
        planned = {k if ":" in k else f"crs:{k}": str(v) for k, v in new.items()}
        if back == out and changed <= planned.keys() and all(after.get(k) == v for k, v in planned.items()) \
                and base[_head(base)[1]:] == back[_head(back)[1]:]:
            verified += 1
        else:
            failed.append(str(x))
        if not had and appledouble.exists():
            appledouble.unlink()
            removed += 1
    return {**report, "written": len(ready), "verified": verified, "failed": failed, "removed ._ files": removed,
            "backup": str(backup)}


def read_in(db, plan: list[dict]) -> dict:
    """After the person's Read Metadata, from a fresh catalogue copy: which planned photos (`plan` entries with the
    photo's "id") now hold their planned develop values. A write isn't done until this says so."""
    now = photos(db)
    done, not_yet = 0, []
    for p in plan:
        settings = now[p["id"]]["settings"]
        if all(_value(settings.get(k)) == _value(v) for k, v in p["set"].items() if ":" not in k):
            done += 1
        else:
            not_yet.append(p["xmp"])
    return {"as planned": done, "not read in": not_yet}


# Framing ------------------------------------------------------------------------------------------------------------

FULL = [0.0, 0.0, 1.0, 1.0]
# The person's framing on ICN Nationals 2026 (medians of 744 of their crops; approved on a sample sheet): margins
# above and below the athlete, in athlete heights.
RULE_TOP, RULE_BOTTOM = 0.17, 0.07
LEVEL_K = 0.654  # their own levelling against Canon's tilt sensor: rotate 0.654 × tilt (corr 0.94)


def rule_crop(people: list[dict], above: float = RULE_TOP, below: float = RULE_BOTTOM, *, most: float = 0.97) -> list:
    """The crop [left, top, right, bottom] of the upright frame round the athlete: the largest person Vision found
    (comp-render subject's `people`, boxes in frame fractions from the top left), `above` and `below` athlete heights
    above and below, centred on them. Square in fractions, so it keeps the frame's shape; meant for portrait frames.
    FULL when no one is found or the crop would keep `most` of the frame or more."""
    found = [p for p in people if p.get("confidence", 1) >= 0.3]
    if not found:
        return FULL
    m = max(found, key=lambda p: p["width"] * p["height"])
    side = (1 + above + below) * m["height"]
    if side >= most:
        return FULL
    y = min(max(m["y"] - above * m["height"], 0), 1 - side)
    x = min(max(m["x"] + m["width"] / 2 - side / 2, 0), 1 - side)
    return [x, y, x + side, y + side]


def inside(crop, degrees: float, width: float, height: float) -> bool:
    """Whether every corner of `crop` (fractions) stays inside the picture turned by `degrees` about its centre, either
    way round (so the sign convention can't leave a blank corner)."""
    cx, cy = width / 2, height / 2
    for a in (math.radians(degrees), -math.radians(degrees)):
        for x, y in ((crop[0] * width, crop[1] * height), (crop[2] * width, crop[1] * height),
                     (crop[0] * width, crop[3] * height), (crop[2] * width, crop[3] * height)):
            u = cx + (x - cx) * math.cos(a) - (y - cy) * math.sin(a)
            v = cy + (x - cx) * math.sin(a) + (y - cy) * math.cos(a)
            if not (-1e-6 <= u <= width + 1e-6 and -1e-6 <= v <= height + 1e-6):
                return False
    return True


def fit(crop, degrees: float, width: float, height: float) -> list:
    """`crop` shrunk about its centre until it sits `inside` the turned picture (FULL gives the largest centred crop).
    Lightroom's Constrain to Image only applies when it's toggled in its window, not when read from a sidecar."""
    if inside(crop, degrees, width, height):
        return list(crop)
    mx, my = (crop[0] + crop[2]) / 2, (crop[1] + crop[3]) / 2
    scaled = lambda s: [mx + (crop[0] - mx) * s, my + (crop[1] - my) * s, mx + (crop[2] - mx) * s, my + (crop[3] - my) * s]
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if inside(scaled(mid), degrees, width, height) else (lo, mid)
    return scaled(lo)


def to_stored(crop, orientation: int) -> list:
    """A crop of the upright frame as Lightroom stores it, in the sensor's own orientation (EXIF `orientation`)."""
    l, t, r, b = crop
    stored = {1: [l, t, r, b], 8: [1 - b, l, 1 - t, r], 6: [t, 1 - r, b, 1 - l], 3: [1 - r, 1 - b, 1 - l, 1 - t]}
    if orientation not in stored:
        raise ValueError(f"orientation {orientation}")
    return stored[orientation]


def crop_settings(crop, rotate: float, orientation: int) -> dict:
    """The sidecar attributes for a crop of the upright frame (already `fit` to the rotation) and a rotation in
    degrees: Upright off, since Upright Auto is computed only when a photo renders (sometimes first at export, once
    turning a photo 54°)."""
    l, t, r, b = (round(v, 6) for v in to_stored(crop, orientation))
    return {"PerspectiveUpright": 0, "PerspectiveRotate": f"{rotate:+.1f}" if rotate else 0, "CropTop": t,
            "CropLeft": l, "CropBottom": b, "CropRight": r, "CropAngle": 0, "CropConstrainToWarp": 1,
            "CropConstrainToUnitSquare": 1, "HasCrop": "True" if list(crop) != FULL else "False"}


def exif(paths, *tags) -> list[dict]:
    """exiftool's values of `tags` (numbers as numbers, -n) for each file: one dict per file, with its SourceFile."""
    paths = [str(p) for p in paths]
    if not paths:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as listing:
        listing.write("\n".join(paths))
        listing.flush()
        try:
            out = subprocess.run(["exiftool", "-n", "-csv", "-charset", "filename=utf8", *[f"-{t}" for t in tags],
                                  "-@", listing.name], capture_output=True, text=True)
        except FileNotFoundError:
            raise CompError("exiftool isn't installed (brew install exiftool)") from None
    return list(csv.DictReader(io.StringIO(out.stdout)))


def camera_tilt(roll: float, orientation: int) -> float:
    """Degrees off level, as the frame is shown, from Canon's RollAngle (0.1°) and the EXIF orientation."""
    return roll + 90 if orientation == 8 else roll - 90 if orientation == 6 else roll


def camera_tilts(paths) -> dict[str, float]:
    """Each RAW's tilt from the camera's level sensor (`camera_tilt`). RAWs without one are left out: the Sony a7R V
    records none."""
    out = {}
    for row in exif(paths, "RollAngle", "Orientation"):
        try:
            out[row["SourceFile"]] = camera_tilt(float(row["RollAngle"]), int(row["Orientation"]))
        except (KeyError, ValueError):
            pass
    return out


def level(tilt: float | None, k: float = LEVEL_K) -> float:
    """The PerspectiveRotate that levels a frame tilted `tilt`° (+ve clockwise, to 0.1°); 0 for no reading or a
    tilt of 15° or more, which is a deliberate angle."""
    return round(k * tilt, 1) if tilt is not None and abs(tilt) < 15 else 0.0


# Blue stage light ---------------------------------------------------------------------------------------------------

def lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0–255) to CIE Lab, D65."""
    rgb = rgb.astype(np.float64) / 255
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T \
        / np.array([0.95047, 1, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def blueness(preview) -> float:
    """How blue the stage light made a frame: the mean Lab b* of its preview (the camera's embedded JPEG, as
    `comp-render previews` writes it), negative for blue. Only meaningful when the camera's white balance was fixed,
    so the cast shows."""
    im = Image.open(preview).convert("RGB")
    im.thumbnail((384, 384))
    return float(lab(np.asarray(im))[..., 2].mean())


def fit_temperature(b, temperature, groups=None) -> dict:
    """Fits the person's own white balance on blue-lit frames: temperature = b × b* + c, plus an offset for each of
    `groups` (one label per frame, e.g. the day) after the first seen. Returns {"b", "c", "offsets", "error": the
    leave-one-out median error in K}. ICN Nationals PRO: b −40.2, c 3,742, Day 2 −444, error 377 K."""
    b, t = np.asarray(b, float), np.asarray(temperature, float)
    names = list(dict.fromkeys(groups))[1:] if groups is not None else []
    X = np.column_stack([b, np.ones(len(b))] + [[g == n for g in groups] for n in names]).astype(float)
    w = np.linalg.lstsq(X, t, rcond=None)[0]
    loo = [abs(X[i] @ np.linalg.lstsq(np.delete(X, i, 0), np.delete(t, i), rcond=None)[0] - t[i]) for i in range(len(t))]
    return {"b": float(w[0]), "c": float(w[1]), "offsets": dict(zip(names, map(float, w[2:]))),
            "error": float(np.median(loo))}


def stage_temperature(b: float, model: dict, group=None, *, blue: float = -40, low: int = 4300,
                      high: int = 6800) -> int | None:
    """The temperature `model` (`fit_temperature`) gives a frame of blueness `b`, to 10 K within [low, high]; None for a frame that isn't
    blue-lit (b* above `blue`), which keeps the show's base. Auto white balance was rejected: it "kills things"."""
    if b > blue:
        return None
    t = model["b"] * b + model["c"] + model["offsets"].get(group, 0.0)
    return int(round(min(max(t, low), high), -1))


# Before upload ------------------------------------------------------------------------------------------------------

def _corner(white: np.ndarray) -> int:
    """The longer leg in pixels of a blank wedge in the top-left corner of a white mask, or 0: white along both
    edges from the corner, joined by a straight edge with white just inside it and picture just outside. A bright
    screen or logo at the edge is white along one edge, or not cut straight, and gives 0."""
    h, w = white.shape
    lx = w if white[0].all() else int(np.argmin(white[0]))
    ly = h if white[:, 0].all() else int(np.argmin(white[:, 0]))
    if lx < 3 or ly < 3:
        return 0
    inner = outer = 0
    for t in np.linspace(0.15, 0.85, 9):
        x, y = lx * (1 - t), ly * t
        inner += white[int(y * 0.8), int(x * 0.8)]
        outer += not white[min(int(y * 1.25) + 2, h - 1), min(int(x * 1.25) + 2, w - 1)]
    return max(lx, ly) if inner >= 0.8 * 9 and outer >= 0.7 * 9 else 0


def blank_corners(jpeg) -> list[int]:
    """Blank (pure white, ≥252) wedges in an exported photo's corners, as Upright leaves when it turns a photo it
    didn't crop: the longer leg in pixels at 1,200 px for the top-left, top-right, bottom-left and bottom-right
    corners, 0 where there's none. Zoom into anything small before calling it real."""
    im = Image.open(jpeg)
    im.draft("RGB", (1300, 1300))
    im = im.convert("RGB")
    im.thumbnail((1200, 1200), Image.NEAREST)
    white = (np.asarray(im) >= 252).all(-1)
    return [_corner(white), _corner(white[:, ::-1]), _corner(white[::-1]), _corner(white[::-1, ::-1])]


# Delivery -----------------------------------------------------------------------------------------------------------

EXPORT_TAGS = ("Model", "DateTimeOriginal", "SubSecTimeOriginal", "RawFileName", "PreservedFileName")
NAMING = "{show} - {division} - {date} - Fit Focus Media{number}"


def _hundredths(when: str, subsec=None) -> str:
    """A capture time to 1/100 s, from comp-render's probe (2026-10-17 08:45:31.450) or EXIF (2026:10:17 08:45:31
    with its SubSecTimeOriginal)."""
    when = when[:10].replace(":", "-") + when[10:]
    if subsec not in (None, ""):
        when = f"{when[:19]}.{subsec}"
    return when[:19] + "." + (when[20:] + "00")[:2]


def match_exports(exports: list[dict], frames) -> tuple[dict[str, str], Counter]:
    """Each exported JPEG's RAW. `exports`: exiftool rows of the JPEGs (`exif(jpegs, *EXPORT_TAGS)`); `frames`: the
    cull's entries for the RAWs (`cull.scan`, or the lines of the cull cache), each with its probe's camera and
    capture time. Matched on capture time to 1/100 s and camera model (file numbers repeat between cameras and days,
    and Lightroom adds -2), then the RAW's name where that still leaves two. Returns ({jpeg: raw}, counts): exports
    with no or several candidates are counted, never guessed."""
    by_time = defaultdict(list)
    for entry in frames:
        probe = entry.get("probe") or {}
        if probe.get("captured"):
            by_time[_hundredths(probe["captured"])].append((entry["path"], probe.get("camera") or ""))
    found, counts = {}, Counter()
    for row in exports:
        model = row.get("Model") or "?"
        candidates = [path for path, camera in by_time.get(_hundredths(row["DateTimeOriginal"],
                                                                        row.get("SubSecTimeOriginal")), [])
                      if camera.endswith(model)]  # the probe's camera is make and model: "Canon Canon EOS R5"
        stem = Path(row.get("RawFileName") or row.get("PreservedFileName") or "").stem
        candidates = [path for path in candidates if Path(path).stem == stem] or candidates
        if len(candidates) == 1:
            found[row["SourceFile"]] = candidates[0]
            counts["matched"] += 1
        else:
            counts[f"{len(candidates)} candidates"] += 1
    return found, counts


def delivery_plan(matches: dict[str, str], exports: list[dict], gallery, show: str, naming: str = NAMING) -> list:
    """Where each matched export goes: [(export, delivered file, raw)], the file being
    <gallery>/<Division>/<naming>.jpg ({show}, {division}: the RAW's folder, {date}: dd-mm-yy it was shot, {number}:
    the RAW's file number), with characters Windows rejects replaced. The same name twice in a division (two
    cameras, two days, a virtual copy) gets " (2)" on the later one."""
    rows = {r["SourceFile"]: r for r in exports}
    division = lambda jpeg: _clean(Path(matches[jpeg]).parent.name)
    order = sorted(matches, key=lambda jpeg: (division(jpeg), _hundredths(rows[jpeg]["DateTimeOriginal"],
                                                                          rows[jpeg].get("SubSecTimeOriginal"))))
    plan, used = [], Counter()
    for jpeg in order:
        raw = Path(matches[jpeg])
        y, mo, d = rows[jpeg]["DateTimeOriginal"][:10].split(":")
        digits = re.search(r"(\d+)$", raw.stem)
        base = _clean(naming.format(show=show, division=division(jpeg), date=f"{d}-{mo}-{y[2:]}",
                                    number=digits.group(1) if digits else raw.stem))
        used[(division(jpeg), base)] += 1
        n = used[(division(jpeg), base)]
        plan.append((jpeg, str(Path(gallery) / division(jpeg) / (base + (f" ({n})" if n > 1 else "") + ".jpg")), str(raw)))
    targets = [p[1] for p in plan]
    if len(set(targets)) != len(targets):
        raise CompError("two exports would get the same name")
    return plan


_UNDO = """#!/bin/zsh
# Puts every photo back where it was exported, from the manifest beside this script.
cd "$(dirname "$0")"
python3 - <<'PY'
import csv, os, shutil
for r in csv.DictReader(open({manifest!r})):
    if os.path.exists(r["renamed_to"]) and not os.path.exists(r["exported_as"]):
        shutil.move(r["renamed_to"], r["exported_as"])
print("done")
PY
"""


def organise(plan: list, gallery, *, go: bool = False) -> dict:
    """Moves each export to its delivered name (`delivery_plan`). With `go`, first writes a manifest and an undo
    script into `gallery`; refuses if any target already exists. Without it, only counts."""
    gallery = Path(gallery)
    report = {"files": len(plan), "divisions": len({Path(t).parent.name for _, t, _ in plan}),
              "same number twice in a division": sum(bool(re.search(r" \(\d+\)\.jpg$", t)) for _, t, _ in plan)}
    if not go:
        return report
    clash = [t for _, t, _ in plan if Path(t).exists()]
    if clash:
        raise CompError(f"{len(clash)} delivered names already exist, e.g. {clash[0]}: nothing moved")
    stamp = time.strftime("%Y%m%d-%H%M")
    manifest, undo = gallery / f"_rename-manifest-{stamp}.csv", gallery / f"_undo-rename-{stamp}.command"
    with open(manifest, "x", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["exported_as", "renamed_to", "raw"])
        writer.writerows(plan)
    undo.write_text(_UNDO.format(manifest=manifest.name))
    undo.chmod(0o755)
    for source, target, _ in plan:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)
    return {**report, "moved": len(plan), "manifest": str(manifest), "undo": str(undo)}


def embedded(jpeg, ns: str = "crs") -> dict[str, str]:
    """The top-level develop settings Lightroom embedded in an export's XMP, to check a re-export (its Temperature,
    PerspectiveUpright …) before it replaces a delivered file."""
    data = Path(jpeg).read_bytes()
    i = data.find(b"<x:xmpmeta")
    j = data.find(b"</x:xmpmeta>", i)
    if i < 0 or j < 0:
        return {}
    try:
        return attrs(data[i:j].decode("utf-8", "replace"), ns)
    except ValueError:
        return {}


def replace(pairs: list, gallery, backup, *, go: bool = False) -> dict:
    """Swaps re-exports into the delivery under the names already delivered: `pairs` are (re-export, delivered file).
    Each delivered file moves to the same place under `backup`, outside the gallery, and each swap is listed in
    backup/_replaced.csv as it's made. Check the re-exports with `embedded` first."""
    gallery, backup = Path(gallery).resolve(), Path(backup).resolve()
    delivered = [Path(old).resolve() for _, old in pairs]
    if len(set(delivered)) != len(delivered):
        raise CompError("two re-exports map to one delivered file")
    if backup.is_relative_to(gallery):
        raise CompError("keep the replaced files outside the gallery")
    missing = [str(p) for p in delivered if not p.exists() or not p.is_relative_to(gallery)]
    if missing:
        raise CompError(f"{len(missing)} delivered files aren't in the gallery, e.g. {missing[0]}")
    if not go:
        return {"to swap": len(pairs)}
    backup.mkdir(parents=True, exist_ok=True)
    start, swapped = time.time(), 0
    with open(backup / "_replaced.csv", "a", newline="") as f:
        log = csv.writer(f)
        for (new, _), old in zip(pairs, delivered):
            kept = backup / old.relative_to(gallery)
            if kept.exists():
                raise CompError(f"{kept} already exists: stopped after {swapped} swaps")
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old, kept)
            shutil.move(new, old)
            log.writerow([Path(new).name, str(old.relative_to(gallery))])
            f.flush()
            swapped += 1
            for q in (kept, old):
                appledouble = q.with_name("._" + q.name)
                if appledouble.exists() and appledouble.stat().st_mtime >= start - 1:
                    appledouble.unlink()
    return {"swapped": swapped, "kept in": str(backup)}
