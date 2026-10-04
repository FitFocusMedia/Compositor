"""compkit: build and edit Compositor projects (.comp) from Python.

A .comp is a folder: manifest.json plus images/<LAYER-ID>.png (and <LAYER-ID>.mask.png). compkit writes that format
following docs/writing-comp-files.md, and leans on `comp-render` (the app's own loader, typesetter and renderer,
built by automation/comp-render/build.sh) for the parts that must match the app exactly: default records, text
pixels, validation and flattening.

    from compkit import Project
    p = Project.new(1080, 1350)
    p.add_image("photo.jpg", name="Photo", fit="cover")
    p.add_adjustment("Curves", name="Warm", curves={"red": [(0, 0), (120, 140), (255, 255)]})
    p.add_text("NEW PB", name="Headline", x=540, y=1200, size=120, font="Helvetica-Bold", color="#FFFFFF", align="Center")
    p.save("~/Desktop/post.comp")
    p.render("~/Desktop/post.jpg")

Saving over a project that is open in Compositor updates the canvas live: images are written first and the manifest
is swapped in atomically, so the app never sees half a project.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageCms, ImageOps

__all__ = ["Project", "Layer", "CompError", "comp_render", "defaults", "subject_mask", "analyze", "auto_focus",
           "fit_image", "read_preset", "develop", "measure", "BLEND_MODES"]

AUTOMATION = Path(__file__).resolve().parent.parent
COMP_RENDER = Path(os.environ.get("COMP_RENDER", AUTOMATION / "bin" / "comp-render"))
FORMAT = "com.compositor.project"
MAX_SIDE = 30_000
SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
# Formats Pillow can't read but macOS can; converted to PNG with `sips` first.
SIPS_SUFFIXES = {".heic", ".heif", ".avif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".svg", ".psd"}


class CompError(Exception):
    pass


def comp_render(*args: str) -> str:
    """Runs comp-render and returns its stdout; raises CompError with its message on failure."""
    if not COMP_RENDER.exists():
        raise CompError(f"{COMP_RENDER} is missing: run automation/comp-render/build.sh")
    result = subprocess.run([str(COMP_RENDER), *map(str, args)], capture_output=True, text=True)
    if result.returncode != 0:
        raise CompError(result.stderr.strip() or f"comp-render {' '.join(map(str, args))} failed")
    return result.stdout


@functools.cache
def defaults() -> dict:
    """The app's own default adjustment, effect and text records (see `comp-render defaults`)."""
    return json.loads(comp_render("defaults"))


@functools.cache
def _blend_modes() -> tuple[str, ...]:
    return tuple(defaults()["blendModes"])


BLEND_MODES = (
    "Normal", "Darken", "Multiply", "Color Burn", "Linear Burn", "Lighten", "Screen", "Color Dodge",
    "Linear Dodge (Add)", "Overlay", "Soft Light", "Hard Light", "Vivid Light", "Linear Light", "Pin Light",
    "Hard Mix", "Difference", "Exclusion", "Subtract", "Divide", "Hue", "Saturation", "Color", "Luminosity",
)


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def rgb(color) -> tuple[float, float, float]:
    """'#RRGGBB', '#RGB', (r, g, b) in 0–255, or (r, g, b) floats in 0–1 → floats in 0–1."""
    if isinstance(color, str):
        value = color.lstrip("#")
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        if len(value) != 6 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise CompError(f"not a color: {color!r}")
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    r, g, b = color[:3]
    if max(r, g, b) > 1:
        return (r / 255, g / 255, b / 255)
    return (float(r), float(g), float(b))


def transform(x: float, y: float, width: float, height: float, rotation: float = 0,
              flip_x: bool = False, flip_y: bool = False, sampling: str = "High quality") -> dict:
    return {"origin": [float(x), float(y)], "size": [float(width), float(height)], "rotation": float(rotation),
            "flipX": bool(flip_x), "flipY": bool(flip_y), "sampling": sampling}


def load_image(source) -> Image.Image:
    """A path or PIL image → RGBA in sRGB, upright (EXIF orientation applied)."""
    if isinstance(source, Image.Image):
        image = source
    else:
        path = Path(source).expanduser()
        if path.suffix.lower() in SIPS_SUFFIXES:
            with tempfile.TemporaryDirectory() as folder:
                converted = Path(folder) / "converted.png"
                subprocess.run(["sips", "-s", "format", "png", "--matchTo",
                                "/System/Library/ColorSync/Profiles/sRGB Profile.icc", str(path), "--out", str(converted)],
                               check=True, capture_output=True)
                image = Image.open(converted)
                image.load()
        else:
            image = Image.open(path)
            image.load()
    image = ImageOps.exif_transpose(image)
    icc = image.info.get("icc_profile")
    if icc and image.mode in ("RGB", "RGBA"):
        try:
            source_profile = ImageCms.ImageCmsProfile(__import__("io").BytesIO(icc))
            alpha = image.getchannel("A") if image.mode == "RGBA" else None
            image = ImageCms.profileToProfile(image.convert("RGB"), source_profile, ImageCms.createProfile("sRGB"),
                                              outputMode="RGB")
            if alpha is not None:
                image.putalpha(alpha)
        except (ImageCms.PyCMSError, OSError):
            pass
    return image.convert("RGBA")


_analyses: dict = {}


def analyze(source) -> dict:
    """What Apple Vision finds in a picture (path or PIL image), from `comp-render subject`: `faces`, `people`,
    `subject` (the foreground Remove Background keeps, with its `coverage`) and `salient` boxes, each
    {x, y, width, height} as fractions of the upright picture from its top-left corner. Cached per file."""
    if isinstance(source, Image.Image):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "picture.png"
            small = source.copy()
            small.thumbnail((1536, 1536))
            small.save(copy)
            return json.loads(comp_render("subject", copy))
    path = Path(source).expanduser().resolve()
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _analyses:
        _analyses[key] = json.loads(comp_render("subject", path))
    return _analyses[key]


def _analysis_for(source) -> dict:
    try:
        return analyze(source)
    except (CompError, OSError) as error:
        print(f"compkit: no subject analysis for {source if not isinstance(source, Image.Image) else 'image'} "
              f"({error}); cropping around the center", file=sys.stderr)
        return {}


def _as_shot_from(name: str):
    import re
    found = re.search(r"as shot (\d+(?:\.\d+)?) K, ([+-]?\d+(?:\.\d+)?)", name)
    return (float(found.group(1)), float(found.group(2))) if found else None


def _label(name: str, source) -> str:
    return f"{name!r}" + ("" if isinstance(source, Image.Image) else f" ({Path(source).name})")


def _union(boxes) -> tuple[float, float, float, float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    edges = [(b[0], b[1], b[2], b[3]) if isinstance(b, tuple) else (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
             for b in boxes]
    return (min(e[0] for e in edges), min(e[1] for e in edges), max(e[2] for e in edges), max(e[3] for e in edges))


def auto_focus(analysis: dict, image_size: tuple[int, int], box: tuple[float, float]) -> tuple[float, float]:
    """Where to crop a picture of `image_size` to fill `box` so what matters stays in frame: the point of the
    picture (0–1 across, 0–1 down) the crop centers on, as fit_image's `focus`. The subject (Remove Background's foreground, else the people, else where the eye
    goes) is centered when it fits. When it doesn't, the crop is anchored on what draws the eye within it:
    - human faces stay whole; when the crop cuts height they sit in the top third, with no empty space above the
      subject
    - otherwise Vision's attention saliency (heads and eyes, for animals too) is centered
    - with neither, the top of the subject is kept when cutting height (where heads are), its middle when
      cutting width
    With nothing found at all, the center."""
    w, h = image_size
    bw, bh = box
    target = bw / bh
    vertical = w / h <= target  # the crop cuts height (True) or width (False)
    length = (w / target) / h if vertical else (h * target) / w  # the crop's share of the axis it cuts
    if length >= 1:
        return (0.5, 0.5)
    faces = _union([f for f in analysis.get("faces", []) if f.get("confidence", 1) >= 0.5])
    salient = _union(analysis.get("salient", []))
    subject = analysis.get("subject")
    if subject and subject.get("coverage", 1) >= 0.01:
        region = _union([subject])
    else:
        region = _union(analysis.get("people", [])) or salient
    region = _union([region, faces])
    anchor = faces or salient
    axis = 1 if vertical else 0
    r = (region[axis], region[axis + 2]) if region else None
    a = (anchor[axis], anchor[axis + 2]) if anchor else None
    if r is None:
        start = 0.5 - length / 2
    elif r[1] - r[0] <= length:
        start = (r[0] + r[1]) / 2 - length / 2
    elif a:
        center = (a[0] + a[1]) / 2
        if a[1] - a[0] > length:
            start = center - length / 2
        else:
            if faces and vertical:
                start = max(center - length / 3, r[0])
            else:
                start = center - length / 2
            # Within the subject where possible, and the anchor whole.
            start = min(max(start, r[0]), r[1] - length)
            start = max(min(start, a[0]), a[1] - length)
    else:
        start = r[0] if vertical else (r[0] + r[1]) / 2 - length / 2
    start = min(max(start, 0), 1 - length)
    center = start + length / 2
    return (0.5, center) if vertical else (center, 0.5)


def cover_crop(size: tuple[int, int], box: tuple[float, float], focus: tuple[float, float]) -> tuple[int, int, int, int]:
    """The (left, top, width, height) of a picture of `size` that fills `box`'s shape, centered on `focus` (a point
    of the picture, 0–1 across and down) as nearly as the picture's edges allow."""
    w, h = size
    target = box[0] / box[1]
    if w / h > target:
        cw, ch = max(1, round(h * target)), h
    else:
        cw, ch = w, max(1, round(w / target))
    left = min(max(focus[0] * w - cw / 2, 0), w - cw)
    top = min(max(focus[1] * h - ch / 2, 0), h - ch)
    return (round(left), round(top), cw, ch)


def fit_image(image: Image.Image, box: tuple[float, float], fit: str = "cover", focus="auto",
              max_scale: float | None = 2.0, analysis=None, label: str | None = None) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Fits `image` to a box of `box` size. Returns the pixels to store and where they sit inside the box
    (x, y, width, height, relative to the box's top-left).

    cover    fills the box, cropping the overflow: focus='auto' keeps faces and the subject in frame (see
             auto_focus), or give the point of the picture to center on, (x, y) 0–1 across and down
    contain  fits inside the box, centered
    stretch  fills the box exactly, ignoring aspect ratio
    `analysis` is the picture's analyze() result, or a function returning it, used only when a crop is needed.
    The stored pixels keep the source's resolution, capped at `max_scale` times the box's size (None: no cap).
    With a `label`, a picture enlarged more than 1.25× to fill its box is reported on stderr: past about 1.5× it
    looks soft, and past 2× a bigger original is worth asking for."""
    bw, bh = box
    w, h = image.size
    if fit == "cover":
        if focus == "auto":
            whole = cover_crop((w, h), box, (0.5, 0.5))
            if (whole[2], whole[3]) == (w, h):
                focus = (0.5, 0.5)
            else:
                found = analysis() if callable(analysis) else analysis
                focus = auto_focus(found if found is not None else _analysis_for(image), (w, h), box)
        left, top, cw, ch = cover_crop((w, h), box, focus)
        image = image.crop((left, top, left + cw, top + ch))
        placement = (0, 0, bw, bh)
    elif fit == "contain":
        scale = min(bw / w, bh / h)
        pw, ph = w * scale, h * scale
        placement = ((bw - pw) / 2, (bh - ph) / 2, pw, ph)
    elif fit == "stretch":
        placement = (0, 0, bw, bh)
    else:
        raise CompError(f"fit must be cover, contain or stretch, not {fit!r}")
    enlarged = max(placement[2] / image.width, placement[3] / image.height)
    if label and enlarged > 1.25:
        print(f"compkit: {label} enlarged {enlarged:.1f}× to {'fill' if fit != 'contain' else 'fit inside the frame at'} "
              f"{placement[2]:g}×{placement[3]:g}"
              f"{'; a bigger original would be sharper' if enlarged > 1.5 else ''}", file=sys.stderr)
    if max_scale:
        limit = max(1, round(placement[2] * max_scale)), max(1, round(placement[3] * max_scale))
        if image.width > limit[0] and image.height > limit[1]:
            image = image.resize(limit, Image.LANCZOS)
    return image, placement


def subject_mask(source, refine: float = 12, contrast: float = 25, shift: float = 0, cutout: bool = False) -> Image.Image:
    """The app's Remove Background on a picture (path or PIL image): its subject mask (white over the subject), or
    with cutout the picture with a transparent background. refine 0–40, contrast 0–100, shift −10–10, as in the
    app's Advanced mode."""
    with tempfile.TemporaryDirectory() as folder:
        picture, output = Path(folder) / "picture.png", Path(folder) / "out.png"
        load_image(source).save(picture)
        args = ["cutout", picture, output, "--refine", str(refine), "--contrast", str(contrast), "--shift", str(shift)]
        comp_render(*args, *([] if cutout else ["--mask"]))
        result = Image.open(output)
        result.load()
    return result.convert("RGBA" if cutout else "L")


# Lightroom presets ----------------------------------------------------------------------------------------------

ORIGINAL, BASE_GRADE, LOCAL, TUNE = " · Original", " · Base Grade", "Preset · ", "Tune · "
_reported: set[str] = set()  # preset notes already printed this run
TUNE_KINDS = ("Exposure", "Curves", "Hue/Saturation", "Color Balance")


def read_preset(preset) -> dict:
    """How a Lightroom / Camera Raw preset (.xmp) maps onto Compositor's Camera Raw (`comp-render preset`): its
    `name`, `mapped` settings, `whiteBalance` conversion, `notes`, `skipped` (no counterpart), `localCorrections`,
    and the `fields` that `settings=` can override."""
    return json.loads(comp_render("preset", Path(preset).expanduser()))


def develop(source, preset, output, *, original=None, crop=None, size=None, amount: float = 1.0, as_shot=None,
            settings: dict | None = None, seed: int = 0) -> dict:
    """Grades a picture with a preset through Compositor's Camera Raw engine (`comp-render develop`) and writes
    `output` (PNG). RAW files are decoded as shot first. `original` also writes the ungraded picture; `crop` is
    (x, y, w, h) in fractions of the upright picture; `size` (w, h) resizes before grading; `amount` 0–2 scales
    the preset; `settings` overrides Camera Raw fields afterwards ({"exposure": 0.2, "curve.shadows": 5}).
    Returns the report: preset name, as-shot white balance used, notes and what was skipped."""
    with tempfile.TemporaryDirectory() as folder:
        if isinstance(source, Image.Image):
            path = Path(folder) / "picture.png"
            source.save(path)
            source = path
        args = ["develop", Path(source).expanduser(), Path(preset).expanduser(), Path(output).expanduser()]
        if original:
            args += ["--original", Path(original).expanduser()]
        if crop:
            args += ["--crop", ",".join(f"{float(v):.6f}" for v in crop)]
        if size:
            args += ["--size", f"{int(size[0])}x{int(size[1])}"]
        if amount != 1:
            args += ["--amount", f"{float(amount):g}"]
        if as_shot:
            args += ["--as-shot", f"{float(as_shot[0]):g},{float(as_shot[1]):g}"]
        for field, value in (settings or {}).items():
            args += ["--set", f"{field}={float(value):g}"]
        if seed:
            args += ["--seed", str(int(seed))]
        return json.loads(comp_render(*args))


def measure(image, region: str = "frame") -> dict:
    """Tone and color numbers for tuning to guidelines, in CIELAB (L* 0–100, a*/b* ±; D65): `lightness` mean and
    percentiles (p5, p25, median, p75, p95), `blown` (% of pixels pure white: detail gone), `channel_clip` (% with
    any one channel at 255: saturated color hitting the limit), `crushed` (% pure black), `chroma` (mean and p90 saturation), `whites` (L* and a*/b* cast of bright near-neutral pixels, or None)
    and `cast` (a*/b* of mid-tone near-neutrals). Percentiles stay put as a picture is tuned, unlike averages over
    a brightness range, so compare those between passes. `region="subject"` measures only what Remove Background
    would keep."""
    picture = load_image(image)
    rgb = np.asarray(picture.convert("RGB"), dtype=np.float64) / 255
    keep = np.ones(rgb.shape[:2], bool)
    if region == "subject":
        keep = np.asarray(subject_mask(picture).resize(picture.size)) > 127
        if not keep.any():
            raise CompError("no subject found to measure")
    elif region != "frame":
        raise CompError("region is 'frame' or 'subject'")
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    L = (116 * f[..., 1] - 16)[keep]
    a = (500 * (f[..., 0] - f[..., 1]))[keep]
    b = (200 * (f[..., 1] - f[..., 2]))[keep]
    c = np.hypot(a, b)
    channels = rgb[keep]
    round1 = lambda v: round(float(v), 1)

    def neutral(select):
        if select.mean() < 0.005:
            return None
        return {"share": round1(select.mean() * 100), "L": round1(L[select].mean()), "a": round1(a[select].mean()),
                "b": round1(b[select].mean())}

    return {
        "region": region, "pixels": int(keep.sum()),
        "lightness": {"mean": round1(L.mean()), **{name: round1(np.percentile(L, q)) for name, q in
                                                  (("p5", 5), ("p25", 25), ("median", 50), ("p75", 75), ("p95", 95))}},
        "blown": round(float((channels >= 254.5 / 255).all(axis=1).mean() * 100), 2),
        "channel_clip": round(float((channels >= 254.5 / 255).any(axis=1).mean() * 100), 2),
        "crushed": round(float((channels <= 0.5 / 255).all(axis=1).mean() * 100), 2),
        "chroma": {"mean": round1(c.mean()), "p90": round1(np.percentile(c, 90))},
        "whites": neutral((L > 80) & (c < 25)),
        "cast": neutral((L > 25) & (L < 80) & (c < 12)),
    }


def _local_weight(correction: dict, size: tuple[int, int]) -> np.ndarray | None:
    """A Lightroom local correction's mask over a frame of `size`, 0–1. Radial and linear gradients are drawn;
    other mask kinds (brush, subject, sky…) can't be, and give None. Coordinates are taken as fractions of the
    framed photo, which is what a base preset's gradients are for."""
    w, h = size
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    fx, fy = (xs + 0.5) / w, (ys + 0.5) / h
    combined = None
    for mask in correction["masks"]:
        what = mask.get("What", "")
        number = lambda key, default=0.0: float(mask.get(key, default))
        if what == "Mask/CircularGradient":
            left, top, right, bottom = number("Left"), number("Top"), number("Right", 1), number("Bottom", 1)
            cx, cy, rx, ry = (left + right) / 2, (top + bottom) / 2, max(1e-6, (right - left) / 2), max(1e-6, (bottom - top) / 2)
            angle = math.radians(number("Angle"))
            dx, dy = fx - cx, fy - cy
            ux, uy = dx * math.cos(angle) + dy * math.sin(angle), -dx * math.sin(angle) + dy * math.cos(angle)
            r = np.sqrt((ux / rx) ** 2 + (uy / ry) ** 2)
            feather = min(max(number("Feather", 50) / 100, 0.001), 1)
            t = np.clip((1 - r) / feather, 0, 1)
            inside = t * t * (3 - 2 * t)
            weight = inside if mask.get("Flipped", "false").lower() == "true" else 1 - inside
        elif what == "Mask/Gradient":
            zx, zy, ax, ay = number("ZeroX"), number("ZeroY"), number("FullX"), number("FullY", 1)
            vx, vy = ax - zx, ay - zy
            t = np.clip(((fx - zx) * vx + (fy - zy) * vy) / max(1e-9, vx * vx + vy * vy), 0, 1)
            weight = t * t * (3 - 2 * t)
        else:
            return None
        if mask.get("MaskInverted", "false").lower() == "true":
            weight = 1 - weight
        weight = weight * number("MaskValue", 1)
        mode = mask.get("MaskBlendMode", "0")
        if combined is None:
            combined = weight
        elif mode == "1":
            combined = combined * (1 - weight)
        elif mode == "2":
            combined = np.minimum(combined, weight)
        else:
            combined = np.maximum(combined, weight)
    return None if combined is None else np.clip(combined * correction.get("amount", 1), 0, 1)


def _merge(base: dict, overrides: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _point(t: dict, unit: tuple[float, float]) -> tuple[float, float]:
    """A point of the layer's box (0–1 on each axis) in document pixels, through its rotation (LayerTransform.point)."""
    (ox, oy), (w, h) = t["origin"], t["size"]
    cx, cy = ox + w / 2, oy + h / 2
    r = math.radians(math.fmod(t.get("rotation", 0), 360))
    x, y = (unit[0] - 0.5) * w, (unit[1] - 0.5) * h
    return cx + x * math.cos(r) - y * math.sin(r), cy + x * math.sin(r) + y * math.cos(r)


class Layer:
    """One record in the manifest. Changes are kept in memory until Project.save."""

    def __init__(self, project: "Project", record: dict):
        self.project = project
        self.record = record

    def __repr__(self):
        kind = "folder" if self.is_group else "adjustment" if self.adjustment else "text" if self.text else "layer"
        return f"<{kind} {self.name!r} {self.id[:8]}>"

    @property
    def id(self) -> str: return self.record["id"]
    @property
    def is_group(self) -> bool: return bool(self.record.get("isGroup"))
    @property
    def adjustment(self) -> dict | None: return self.record.get("adjustment")
    @property
    def text(self) -> dict | None: return self.record.get("text")
    @property
    def transform(self) -> dict: return self.record["transform"]
    @property
    def parent(self) -> "Layer | None":
        parent = self.record.get("parentID")
        return self.project.layer(parent) if parent else None

    @property
    def name(self) -> str: return self.record["name"]
    @name.setter
    def name(self, value: str): self.record["name"] = value

    @property
    def visible(self) -> bool: return self.record.get("isVisible", True)
    @visible.setter
    def visible(self, value: bool): self.record["isVisible"] = bool(value)

    @property
    def opacity(self) -> float: return self.record.get("opacity", 1)
    @opacity.setter
    def opacity(self, value: float):
        if not 0 <= value <= 1:
            raise CompError("opacity runs from 0 to 1")
        self.record["opacity"] = float(value)

    @property
    def blend_mode(self) -> str: return self.record.get("blendMode", "Normal")
    @blend_mode.setter
    def blend_mode(self, value: str):
        if value not in BLEND_MODES:
            raise CompError(f"unknown blend mode {value!r}; one of {', '.join(BLEND_MODES)}")
        if self.is_group and value != "Normal":
            raise CompError("folders are pass-through: their blend mode stays Normal")
        self.record["blendMode"] = value

    def place(self, x=None, y=None, width=None, height=None, rotation=None, flip_x=None, flip_y=None, sampling=None) -> "Layer":
        """Moves or scales the layer's box (its transform), in document pixels. A text layer's box sits the text
        padding (12 px) outside its text; place_text() positions text by the text itself, as add_text does."""
        t = self.transform
        if x is not None: t["origin"][0] = float(x)
        if y is not None: t["origin"][1] = float(y)
        if width is not None: t["size"][0] = float(width)
        if height is not None: t["size"][1] = float(height)
        if rotation is not None: t["rotation"] = float(rotation)
        if flip_x is not None: t["flipX"] = flip_x
        if flip_y is not None: t["flipY"] = flip_y
        if sampling is not None: t["sampling"] = sampling
        return self

    def ink_box(self) -> tuple[float, float, float, float] | None:
        """Where the layer's visible pixels are, as (x, y, width, height) in document pixels (unrotated layers):
        for text, the letters themselves, for lining them up optically."""
        pixels = self.pixels()
        found = pixels.getchannel("A").getbbox() if pixels else None
        if not found:
            return None
        t = self.transform
        sx, sy = t["size"][0] / pixels.width, t["size"][1] / pixels.height
        left, top, right, bottom = found
        return (t["origin"][0] + left * sx, t["origin"][1] + top * sy, (right - left) * sx, (bottom - top) * sy)

    def place_ink(self, x=None, y=None) -> "Layer":
        """Moves the layer so its visible pixels' top-left (ink_box) lands at x, y: for stacking and aligning text
        by its letters rather than its padded box."""
        ink = self.ink_box()
        if ink is None:
            raise CompError(f"{self.name!r} has no visible pixels")
        t = self.transform
        return self.place(x=None if x is None else t["origin"][0] + x - ink[0],
                          y=None if y is None else t["origin"][1] + y - ink[1])

    def add_backdrop(self, color, padding=(24, 12), name: str | None = None, opacity: float = 1,
                     use: str = "ink") -> "Layer":
        """A solid rectangle just behind this layer, in its folder, sized to its letters (`use="ink"`) or its text
        area (`use="area"`) plus `padding` (x, y): a badge, pill or label plate. Returns the new layer."""
        box = self.ink_box() if use == "ink" else self.text_area
        if box is None:
            raise CompError(f"{self.name!r} has nothing to put a backdrop behind")
        px, py = padding if isinstance(padding, (tuple, list)) else (padding, padding)
        record = self.project._record(name or f"{self.name} Backdrop",
                                      transform(box[0] - px, box[1] - py, box[2] + 2 * px, box[3] + 2 * py), opacity, "Normal")
        r, g, b = (round(c * 255) for c in rgb(color))
        record["imageFile"] = f"{record['id']}.png"
        self.project._images[record["id"]] = Image.new("RGBA", (max(1, round(box[2] + 2 * px)), max(1, round(box[3] + 2 * py))), (r, g, b, 255))
        return self.project._add(record, self.parent, None, below=self)

    # Pixels

    def pixels(self) -> Image.Image | None:
        """The layer's stored pixels (not transformed), or None for folders, adjustments and blank layers."""
        if self.id in self.project._images:
            return self.project._images[self.id]
        file = self.record.get("imageFile")
        if not file or not self.project.path:
            return None
        return Image.open(self.project.path / "images" / file).convert("RGBA")

    def set_pixels(self, image, keep_text: bool = False) -> "Layer":
        """Replaces the stored pixels; the transform still decides where and how big they draw. Text and shape
        metadata are dropped (the layer becomes plain pixels), as a pixel edit in the app does."""
        if self.is_group or self.adjustment:
            raise CompError(f"{self.name!r} has no pixels")
        self.project._images[self.id] = load_image(image)
        self.record["imageFile"] = f"{self.id}.png"
        self.record.pop("shape", None)
        if not keep_text:
            self.record.pop("text", None)
        return self

    def replace_image(self, source, fit: str = "cover", focus="auto", max_scale: float | None = 2.0,
                      preset=None, amount: float = 1.0, settings: dict | None = None) -> "Layer":
        """Puts a new picture in this layer's box (its current transform), as a template's placeholder: position,
        rotation, mask, effects, opacity and blend mode all stay. focus='auto' crops around faces and the subject
        (see auto_focus); (x, y) centers the crop on that point. On a graded-photo folder the new picture is graded
        with `preset` (required there), keeping the tune layers' settings."""
        if self.graded_parts():
            if preset is None:
                raise CompError(f"{self.name!r} is a graded photo: give the preset to grade the new picture with")
            self.project._grade_into(self, source, preset, focus=focus, max_scale=max_scale, amount=amount, settings=settings)
            return self
        t = self.transform
        image, (px, py, pw, ph) = fit_image(load_image(source), tuple(t["size"]), fit, focus, max_scale,
                                            analysis=lambda: _analysis_for(source), label=_label(self.name, source))
        if (px, py) != (0, 0):
            # contain: shrink the box around the picture, keeping its center.
            corner = _point(t, (px / t["size"][0], py / t["size"][1]))
            t["size"] = [pw, ph]
            moved = _point(t, (0, 0))
            t["origin"] = [t["origin"][0] + corner[0] - moved[0], t["origin"][1] + corner[1] - moved[1]]
        return self.set_pixels(image)

    def set_mask(self, mask, enabled: bool = True) -> "Layer":
        """A grayscale mask (path, PIL image or array; white shows, black hides) covering the layer's own pixels.
        None removes it."""
        if mask is None:
            self.project._masks.pop(self.id, None)
            self.record.pop("maskFile", None)
            self.record.pop("maskEnabled", None)
            self.record.pop("maskPlacement", None)
            self.record.pop("maskLinked", None)
            return self
        if isinstance(mask, np.ndarray):
            mask = Image.fromarray(mask.astype(np.uint8), "L")
        elif not isinstance(mask, Image.Image):
            mask = Image.open(Path(mask).expanduser())
        if mask.mode in ("RGBA", "LA"):
            mask = mask.getchannel("A")
        self.project._masks[self.id] = mask.convert("L")
        self.record["maskFile"] = f"{self.id}.mask.png"
        self.record["maskEnabled"] = enabled
        return self

    # Graded photos (a Lightroom preset applied with the original kept)

    def graded_parts(self) -> dict | None:
        """For a graded-photo folder (Project.add_graded_photo): its `original`, `graded`, `local` and `tune`
        layers. None for anything else."""
        if not self.is_group:
            return None
        children = self.project.children(self)
        original = next((c for c in children if ORIGINAL in c.name), None)
        graded = next((c for c in children if BASE_GRADE in c.name), None)
        if not original or not graded:
            return None
        return {"original": original, "graded": graded,
                "local": [c for c in children if c.name.startswith(LOCAL)],
                "tune": [c for c in children if c.name.startswith(TUNE)]}

    def regrade(self, preset, amount: float = 1.0, settings: dict | None = None) -> dict:
        """Grades a graded-photo folder again from its kept original: another preset, strength or Camera Raw
        overrides (`settings`). Tune layers keep their settings; the preset's local corrections are rebuilt."""
        parts = self.graded_parts()
        if not parts:
            raise CompError(f"{self.name!r} is not a graded photo (see Project.add_graded_photo)")
        as_shot = _as_shot_from(parts["original"].name)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "graded.png"
            report = develop(parts["original"].pixels(), preset, out, amount=amount, as_shot=as_shot, settings=settings)
            parts["graded"].set_pixels(Image.open(out))
        self.project._finish_graded(self, parts["graded"], report, preset, amount, settings=settings)
        return report

    def mask_subject(self, refine: float = 12, contrast: float = 25, shift: float = 0) -> "Layer":
        """Remove Background as a layer mask: hides everything but the subject Apple Vision finds in the layer's
        pixels, without erasing them (paint the mask in Compositor to fix it up)."""
        pixels = self.pixels()
        if pixels is None:
            raise CompError(f"{self.name!r} has no pixels")
        return self.set_mask(subject_mask(pixels, refine=refine, contrast=contrast, shift=shift))

    # Text

    def set_text(self, content: str | None = None, shrink_to_fit: bool = False, min_size: float | None = None,
                 max_width: float | None = None, **style) -> "Layer":
        """Changes a text layer's words or style (font, size, color, align, tracking, leading, box) and sets its
        pixels again with the app's typesetter, keeping its top-left corner, scale and rotation as the app does.

        shrink_to_fit steps the size down (spacing with it) until the words fit: inside the paragraph box for box
        text, or for point text within `max_width` layer pixels (default: the canvas less the same margin on the
        right as the text has on the left). It stops at `min_size` (default 40% of the size) and warns."""
        if not self.text:
            raise CompError(f"{self.name!r} is not a text layer")
        old = self.text
        new = _text_style(old, content, style)
        image, placement = _typeset(new)
        new = placement.pop("style")
        old_pixels = self.pixels()
        t = self.transform
        if shrink_to_fit:
            if new.get("boxSize") is None and max_width is None and not t.get("rotation") and old_pixels:
                scale = t["size"][0] / old_pixels.width
                left = t["origin"][0] + placement["padding"] * scale
                max_width = (self.project.width - 2 * left) / scale if left < self.project.width / 2 else None

            def too_big(found):
                if new.get("boxSize") is not None:
                    return found["overflow"]
                return max_width is not None and found["textWidth"] > max_width

            start = new["fontSize"]
            floor = min_size or start * 0.4
            while too_big(placement) and new["fontSize"] > floor:
                ratio = max(floor, round(new["fontSize"] * 0.94, 1)) / new["fontSize"]
                new["fontSize"] = round(new["fontSize"] * ratio, 1)
                new["leading"] = round(new.get("leading", 0) * ratio, 1)
                new["tracking"] = round(new.get("tracking", 0) * ratio, 2)
                image, placement = _typeset(new)
                new = placement.pop("style")
            if new["fontSize"] != start:
                print(f"compkit: {self.name!r} set at {new['fontSize']:g}px (from {start:g}) to fit", file=sys.stderr)
            if too_big(placement):
                print(f"compkit: {self.name!r} still doesn't fit at {new['fontSize']:g}px", file=sys.stderr)
        elif placement["overflow"]:
            print(f"compkit: {self.name!r} doesn't fit its box at {new['fontSize']:g}px; it's cut off (pass "
                  f"box=(w, None) to refit an auto-height box, or shrink_to_fit=True)", file=sys.stderr)
        anchor = _point(t, (0, 0))
        # The layer keeps its scale and top-left corner as the pixels change size (a new box, or point text that
        # grew or shrank), so the text is never stretched.
        sx = t["size"][0] / old_pixels.width if old_pixels else 1
        sy = t["size"][1] / old_pixels.height if old_pixels else 1
        t["size"] = [image.width * sx, image.height * sy]
        moved = _point(t, (0, 0))
        t["origin"] = [t["origin"][0] + anchor[0] - moved[0], t["origin"][1] + anchor[1] - moved[1]]
        self.project._images[self.id] = image
        self.record["text"] = new
        return self

    def text_metrics(self) -> dict:
        """How the app sets this text layer now: `width`/`height` of its pixels, `padding`, `baseline` (first
        baseline below the pixels' top), `textWidth`/`textHeight` (the lines themselves) and `overflow` (lines cut
        off by the box). In layer pixels."""
        if not self.text:
            raise CompError(f"{self.name!r} is not a text layer")
        _, placement = _typeset(self.text)
        placement.pop("style")
        return placement

    @property
    def text_area(self) -> tuple[float, float, float, float]:
        """The text's own area as (x, y, width, height) in document pixels (unrotated layers): the paragraph box
        inside its padding, or for point text the lines as set — what add_text's x, y and box describe."""
        found = self.text_metrics()
        t, pad = self.transform, found["padding"]
        sx, sy = t["size"][0] / found["width"], t["size"][1] / found["height"]
        if self.text.get("boxSize"):
            w, h = found["width"] - 2 * pad, found["height"] - 2 * pad
        else:
            w, h = found["textWidth"], found["textHeight"]
        return (t["origin"][0] + pad * sx, t["origin"][1] + pad * sy, w * sx, h * sy)

    def place_text(self, x=None, y=None) -> "Layer":
        """Moves a text layer so its text area's top-left is at x, y (document pixels), as add_text places it."""
        if not self.text:
            raise CompError(f"{self.name!r} is not a text layer")
        pad, t = defaults()["textPadding"], self.transform
        pixels = self.pixels()
        sx = t["size"][0] / pixels.width if pixels else 1
        sy = t["size"][1] / pixels.height if pixels else 1
        return self.place(x=None if x is None else x - pad * sx, y=None if y is None else y - pad * sy)

    # Effects

    def set_effect(self, kind: str, **settings) -> "Layer":
        """Adds or changes a layer effect: stroke, shadow, colorOverlay, innerShadow, outerGlow or innerGlow.
        Colors may be given as color='#RRGGBB'."""
        base = defaults()["effects"]
        if kind not in base:
            raise CompError(f"unknown effect {kind!r}; one of {', '.join(base)}")
        if "color" in settings:
            settings["red"], settings["green"], settings["blue"] = rgb(settings.pop("color"))
        effects = self.record.setdefault("effects", {})
        effects[kind] = _merge(effects.get(kind) or base[kind], settings)
        return self

    def remove_effect(self, kind: str) -> "Layer":
        effects = self.record.get("effects") or {}
        effects.pop(kind, None)
        if not effects:
            self.record.pop("effects", None)
        return self

    # Adjustments

    def set_adjustment(self, **settings) -> "Layer":
        if not self.adjustment:
            raise CompError(f"{self.name!r} is not an adjustment layer")
        self.record["adjustment"] = _merge(self.adjustment, _adjustment_settings(settings))
        return self

    def clip_to(self, base: "Layer | None") -> "Layer":
        """Clipping mask: shows this layer only where `base` has pixels (None releases it)."""
        if base is None:
            self.record.pop("maskSourceID", None)
        else:
            self.record["maskSourceID"] = base.id
        return self


def _text_style(base: dict, content: str | None, style: dict) -> dict:
    result = copy.deepcopy(base)
    if content is not None:
        result["content"] = content
        result.pop("colorRuns", None)
        result.pop("fontRuns", None)
    renames = {"font": "fontName", "size": "fontSize", "align": "alignment"}
    for key, value in style.items():
        key = renames.get(key, key)
        if key == "color":
            result["red"], result["green"], result["blue"] = rgb(value)
        elif key == "alignment":
            result[key] = value.capitalize()
        elif key == "box":
            # The box is the text's own area; the app's record holds it with its padding added.
            if value is None:
                result.pop("boxSize", None)
            else:
                pad = defaults()["textPadding"]
                result["boxSize"] = [float(value[0]) + 2 * pad, None if value[1] is None else float(value[1]) + 2 * pad]
        else:
            result[key] = value
    return result


def _typeset(style: dict) -> tuple[Image.Image, dict]:
    """The text's pixels and placement from the app's typesetter. A box of height None is first made as tall as
    the lines need; the style as set is returned under placement["style"]."""
    style = copy.deepcopy(style)
    box = style.get("boxSize")
    if box is not None and box[1] is None:
        box[1] = 16
        measured = json.loads(_run_typesetter(style)[1])
        box[1] = math.ceil(measured["textHeight"]) + 2 * measured["padding"]
    image, output = _run_typesetter(style)
    placement = json.loads(output)
    placement["style"] = style
    return image, placement


def _run_typesetter(style: dict) -> tuple[Image.Image, str]:
    with tempfile.TemporaryDirectory() as folder:
        style_file, png = Path(folder) / "style.json", Path(folder) / "text.png"
        style_file.write_text(json.dumps(style))
        output = comp_render("text", style_file, png)
        image = Image.open(png).convert("RGBA")
        image.load()
    return image, output


def _adjustment_settings(settings: dict) -> dict:
    """Friendly keyword arguments → adjustment record fields."""
    settings = dict(settings)
    curves = settings.pop("curves", None)
    if isinstance(curves, dict) and "channels" not in curves:
        # curves={"rgb": [(0, 0), (128, 150), (255, 255)], "red": [...]} → four point lists, RGB then R, G, B.
        identity = [{"x": 0, "y": 0}, {"x": 255, "y": 255}]
        channels = []
        for name in ("rgb", "red", "green", "blue"):
            points = curves.get(name)
            channels.append([{"x": x, "y": y} for x, y in points] if points else identity)
        settings["curves"] = {"channel": "RGB", "channels": channels}
    elif curves is not None:
        settings["curves"] = curves
    levels = settings.pop("levels", None)
    if isinstance(levels, dict) and "ranges" not in levels:
        # levels={"rgb": {"black": 10, "gamma": 1.1, "white": 245}} → four channel ranges.
        identity = {"black": 0, "gamma": 1, "white": 255, "outputBlack": 0, "outputWhite": 255}
        settings["levels"] = {"channel": "RGB", "ranges": [{**identity, **levels.get(name, {})} for name in ("rgb", "red", "green", "blue")]}
    elif levels is not None:
        settings["levels"] = levels
    return settings


class Project:
    """A Compositor project in memory. Layers run bottom to top, as in the manifest."""

    def __init__(self, manifest: dict, path: Path | None = None):
        self.manifest = manifest
        self.path = path
        self._images: dict[str, Image.Image] = {}
        self._masks: dict[str, Image.Image] = {}

    # Opening

    @classmethod
    def new(cls, width: int, height: int, resolution: float = 72) -> "Project":
        if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
            raise CompError(f"canvas sides run from 1 to {MAX_SIDE:,} pixels")
        return cls({"format": FORMAT, "version": defaults()["formatVersion"], "colorSpace": "sRGB",
                    "documentID": new_id(), "width": int(width), "height": int(height), "resolution": resolution,
                    "activeLayerID": None, "layers": [], "guides": []})

    @classmethod
    def open(cls, path) -> "Project":
        path = Path(path).expanduser().resolve()
        manifest = json.loads((path / "manifest.json").read_text())
        if manifest.get("format") != FORMAT:
            raise CompError(f"{path} is not a Compositor project")
        return cls(manifest, path)

    # Layers

    @property
    def width(self) -> int: return self.manifest["width"]
    @property
    def height(self) -> int: return self.manifest["height"]

    @property
    def layers(self) -> list[Layer]:
        return [Layer(self, record) for record in self.manifest["layers"]]

    def layer(self, id_or_name: str) -> Layer:
        """A layer by ID, or by name (the topmost with that name)."""
        for record in reversed(self.manifest["layers"]):
            if record["id"] == id_or_name or record["name"] == id_or_name:
                return Layer(self, record)
        raise CompError(f"no layer named {id_or_name!r}; layers are: {', '.join(r['name'] for r in self.manifest['layers'])}")

    def find(self, name: str) -> list[Layer]:
        return [layer for layer in self.layers if layer.name == name]

    def children(self, group: Layer | None) -> list[Layer]:
        parent = group.id if group else None
        return [layer for layer in self.layers if layer.record.get("parentID") == parent]

    def _add(self, record: dict, parent: Layer | None, above: Layer | None, below: Layer | None = None) -> Layer:
        if parent is not None:
            if not parent.is_group:
                raise CompError(f"{parent.name!r} is not a folder")
            record["parentID"] = parent.id
        layers = self.manifest["layers"]
        if above is not None:
            index = next(i for i, r in enumerate(layers) if r["id"] == above.id) + 1
        elif below is not None:
            index = next(i for i, r in enumerate(layers) if r["id"] == below.id)
        else:
            index = len(layers)
        layers.insert(index, record)
        self.manifest["activeLayerID"] = record["id"]
        return Layer(self, record)

    def _record(self, name: str, t: dict, opacity: float, blend: str, visible: bool = True) -> dict:
        if blend not in BLEND_MODES:
            raise CompError(f"unknown blend mode {blend!r}")
        if not name.strip():
            raise CompError("a layer needs a name")
        return {"id": new_id(), "name": name, "isVisible": visible, "isGroup": False,
                "opacity": float(opacity), "blendMode": blend, "transform": t}

    def add_image(self, source, name: str | None = None, *, x: float = 0, y: float = 0,
                  width: float | None = None, height: float | None = None, fit: str | None = "cover",
                  focus="auto", max_scale: float | None = 2.0, opacity: float = 1, blend: str = "Normal",
                  parent: Layer | None = None, above: Layer | None = None) -> Layer:
        """Adds a picture. With no box it fills the canvas (fit='cover'); give x, y, width, height for a box.
        fit=None places it at its own pixel size at x, y. focus='auto' crops around faces and the subject."""
        image = load_image(source)
        if name is None:
            name = Path(source).stem if not isinstance(source, Image.Image) else "Layer"
        if fit is None:
            box = (x, y, width or image.width, height or image.height)
        else:
            bw = width if width is not None else (self.width if height is None else image.width * height / image.height)
            bh = height if height is not None else (self.height if width is None else image.height * width / image.width)
            image, (px, py, pw, ph) = fit_image(image, (bw, bh), fit, focus, max_scale,
                                                analysis=lambda: _analysis_for(source), label=_label(name, source))
            box = (x + px, y + py, pw, ph)
        record = self._record(name, transform(*box), opacity, blend)
        record["imageFile"] = f"{record['id']}.png"
        self._images[record["id"]] = image
        return self._add(record, parent, above)

    def add_fill(self, color, name: str = "Color Fill", *, x: float = 0, y: float = 0, width: float | None = None,
                 height: float | None = None, opacity: float = 1, blend: str = "Normal",
                 parent: Layer | None = None, above: Layer | None = None) -> Layer:
        """A solid color rectangle (the whole canvas by default)."""
        w, h = int(round(width or self.width)), int(round(height or self.height))
        r, g, b = (round(c * 255) for c in rgb(color))
        return self.add_image(Image.new("RGBA", (w, h), (r, g, b, 255)), name, x=x, y=y, fit=None,
                              opacity=opacity, blend=blend, parent=parent, above=above)

    def add_gradient(self, stops: Iterable, name: str = "Gradient", *, angle: float = 90, x: float = 0, y: float = 0,
                     width: float | None = None, height: float | None = None, opacity: float = 1,
                     blend: str = "Normal", parent: Layer | None = None, above: Layer | None = None) -> Layer:
        """A linear gradient. `stops` are colors, or (position 0–1, color) pairs, or (position, color, alpha 0–1).
        angle is in degrees as Photoshop's: 90 runs bottom to top, 0 left to right, 270 top to bottom."""
        w, h = int(round(width or self.width)), int(round(height or self.height))
        stops = list(stops)
        parsed = []
        for i, stop in enumerate(stops):
            if isinstance(stop, (tuple, list)) and len(stop) in (2, 3) and isinstance(stop[0], (int, float)) and not isinstance(stop[1], (int, float)):
                parsed.append((float(stop[0]), rgb(stop[1]), float(stop[2]) if len(stop) == 3 else 1.0))
            else:
                parsed.append((i / max(1, len(stops) - 1), rgb(stop), 1.0))
        parsed.sort(key=lambda s: s[0])
        radians = math.radians(angle)
        dx, dy = math.cos(radians), -math.sin(radians)
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        projection = (xs - w / 2) * dx + (ys - h / 2) * dy
        extent = abs(w / 2 * dx) + abs(h / 2 * dy)
        t = np.clip((projection + extent) / (2 * extent or 1), 0, 1)
        positions = [s[0] for s in parsed]
        channels = [np.interp(t, positions, [s[1][c] for s in parsed]) for c in range(3)]
        alpha = np.interp(t, positions, [s[2] for s in parsed])
        pixels = (np.stack([*channels, alpha], axis=-1) * 255 + 0.5).astype(np.uint8)
        return self.add_image(Image.fromarray(pixels, "RGBA"), name, x=x, y=y, fit=None,
                              opacity=opacity, blend=blend, parent=parent, above=above)

    def add_text(self, content: str, name: str | None = None, *, x: float = 0, y: float = 0,
                 font: str = "Helvetica", size: float = 72, color="#000000", align: str = "Left",
                 tracking: float = 0, leading: float = 0, box: tuple[float, float] | None = None,
                 anchor: str = "baseline", opacity: float = 1, blend: str = "Normal",
                 parent: Layer | None = None, above: Layer | None = None) -> Layer:
        """An editable text layer, set by the app's own typesetter (double-click it in Compositor to edit).

        font     a PostScript name, e.g. 'Helvetica-Bold', 'Futura-CondensedExtraBold', 'SFProDisplay-Black'
        tracking extra space between letters in pixels (AppKit kern; not Photoshop's thousandths of an em)
        leading  baseline to baseline in pixels; 0 is auto (120% of size)
        box      (width, height) makes paragraph text that wraps within that area, whose top-left is x, y; a
                 height of None makes the box as tall as the text needs. Text that doesn't fit is reported
        anchor   for point text: 'baseline' puts the first baseline at y, starting at x (center/right
                 alignment: x is the line's center/right end), as a click with the Type tool does;
                 'top-left' puts the text's top-left at x, y"""
        style = _text_style(defaults()["text"], content, {"font": font, "size": size, "color": color, "align": align,
                                                           "tracking": tracking, "leading": leading, "box": box})
        image, placement = _typeset(style)
        style = placement.pop("style")
        pad = placement["padding"]
        if placement["overflow"]:
            print(f"compkit: {name or content[:30]!r} doesn't fit its box at {size:g}px; it's cut off "
                  f"(give the box more height, or set_text(..., shrink_to_fit=True))", file=sys.stderr)
        if box is not None:
            ox, oy = x - pad, y - pad
        elif anchor == "baseline":
            shift = {"Left": pad, "Center": image.width / 2, "Right": image.width - pad}[style["alignment"]]
            ox, oy = x - shift, y - placement["baseline"]
        elif anchor == "top-left":
            ox, oy = x - pad, y - pad
        else:
            raise CompError("anchor is 'baseline' or 'top-left'")
        name = name or " ".join(content.split())[:40] or "Text"
        record = self._record(name, transform(ox, oy, image.width, image.height), opacity, blend)
        record["imageFile"] = f"{record['id']}.png"
        record["text"] = style
        self._images[record["id"]] = image
        return self._add(record, parent, above)

    def add_adjustment(self, kind: str, name: str | None = None, *, opacity: float = 1, blend: str = "Normal",
                       parent: Layer | None = None, above: Layer | None = None, **settings) -> Layer:
        """An adjustment layer affecting everything below it (within its folder). kind is one of the app's:
        Hue/Saturation, Levels, Curves, Exposure, Gradient Map, Grain, Invert, Black & White, Color Balance,
        Gaussian Blur, Motion Blur, Add Noise. Settings are the record's fields (`comp-render defaults` lists them),
        plus shorthands: curves={"rgb": [(x, y), ...], "red": ...}, levels={"rgb": {"black": .., "white": ..}}."""
        kinds = defaults()["adjustments"]
        if kind not in kinds:
            raise CompError(f"unknown adjustment {kind!r}; one of {', '.join(kinds)}")
        record = self._record(name or kind, transform(0, 0, self.width, self.height), opacity, blend)
        record["adjustment"] = _merge(kinds[kind], _adjustment_settings(settings))
        return self._add(record, parent, above)

    def add_group(self, name: str, *, opacity: float = 1, parent: Layer | None = None,
                  above: Layer | None = None) -> Layer:
        record = self._record(name, transform(0, 0, self.width, self.height), opacity, "Normal")
        record["isGroup"] = True
        return self._add(record, parent, above)

    def add_graded_photo(self, source, preset, name: str = "Photo", *, x: float = 0, y: float = 0,
                         width: float | None = None, height: float | None = None, fit: str = "cover", focus="auto",
                         max_scale: float | None = 2.0, amount: float = 1.0, settings: dict | None = None,
                         local: bool = True, tune: bool = True, parent: Layer | None = None,
                         above: Layer | None = None) -> Layer:
        """A photo with a Lightroom / Camera Raw preset (.xmp) applied as its base grade, kept adjustable. Makes a
        folder `name` holding, bottom to top:
          `<name> · Original`        the photo, ungraded and hidden (kept to grade again from)
          `<name> · Base Grade (…)`  the preset applied by Compositor's Camera Raw engine
          `Preset · …`               the preset's radial/linear local corrections, as masked Exposure layers
          `Tune · Exposure/Curves/Hue/Saturation/Color Balance`  neutral adjustment layers to fine-tune with
        The folder keeps the grade and tuning to this photo. Placement and cropping work as in add_image
        (cover or contain); `amount` 0–2 scales the preset; `settings` overrides Camera Raw fields."""
        group = self.add_group(name, parent=parent, above=above)
        self._grade_into(group, source, preset, x=x, y=y, width=width, height=height, fit=fit, focus=focus,
                         max_scale=max_scale, amount=amount, settings=settings, local=local, tune=tune)
        return group

    def grade_layer(self, layer: Layer, preset, source=None, *, focus="auto", amount: float = 1.0,
                    settings: dict | None = None, max_scale: float | None = 2.0) -> Layer:
        """Turns a plain picture layer into a graded-photo folder of the same name, in the same place: graded from
        `source` (a new picture, cropped into the layer's box) or, without one, from the layer's own pixels. A
        layer mask moves onto the folder; layer effects are dropped (a folder can't carry them)."""
        if layer.graded_parts():
            if source is not None:
                layer.replace_image(source, focus=focus, preset=preset, amount=amount, settings=settings)
            else:
                layer.regrade(preset, amount=amount, settings=settings)
            return layer
        if layer.is_group or layer.adjustment or not layer.record.get("imageFile"):
            raise CompError(f"{layer.name!r} has no picture to grade")
        t = layer.transform
        if t.get("rotation") or t.get("flipX") or t.get("flipY"):
            raise CompError(f"{layer.name!r} is rotated or flipped; grade it in an unrotated box")
        group = self.add_group(layer.name, parent=layer.parent, above=layer)
        group.opacity = layer.opacity
        picture = source if source is not None else layer.pixels()
        self._grade_into(group, picture, preset, x=t["origin"][0], y=t["origin"][1], width=t["size"][0],
                         height=t["size"][1], focus=focus if source is not None else (0.5, 0.5), max_scale=max_scale,
                         amount=amount, settings=settings)
        if layer.record.get("maskFile"):
            mask = self._masks.get(layer.id) or Image.open(self.path / "images" / layer.record["maskFile"])
            group.set_mask(mask, enabled=layer.record.get("maskEnabled", True))
        if layer.record.get("effects"):
            print(f"compkit: {layer.name!r}: its layer effects don't carry over to the graded folder", file=sys.stderr)
        self.remove(layer)
        return group

    def _grade_into(self, group: Layer, source, preset, *, x=None, y=None, width=None, height=None, fit="cover",
                    focus="auto", max_scale=2.0, amount=1.0, settings=None, local=True, tune=True) -> dict:
        parts = group.graded_parts()
        found = _analysis_for(source)
        if found.get("width"):
            size = (found["width"], found["height"])
        else:
            size = load_image(source).size
        if parts:  # a new picture for an existing graded photo: same box
            t = parts["original"].transform
            bx, by, bw, bh = t["origin"][0], t["origin"][1], t["size"][0], t["size"][1]
        else:
            bw = width if width is not None else (self.width if height is None else size[0] * height / size[1])
            bh = height if height is not None else (self.height if width is None else size[1] * width / size[0])
            bx, by = x or 0, y or 0
        w, h = size
        if fit == "cover":
            if focus == "auto":
                focus = auto_focus(found, size, (bw, bh)) if found else (0.5, 0.5)
            left, top, cw, ch = cover_crop(size, (bw, bh), focus)
            crop = (left / w, top / h, cw / w, ch / h)
            placement = (bx, by, bw, bh)
        elif fit == "contain":
            left, top, cw, ch, crop = 0, 0, w, h, None
            scale = min(bw / w, bh / h)
            placement = (bx + (bw - w * scale) / 2, by + (bh - h * scale) / 2, w * scale, h * scale)
        else:
            raise CompError("a graded photo is fitted with cover or contain")
        enlarged = placement[2] / cw
        if enlarged > 1.25:
            print(f"compkit: {group.name!r} ({Path(source).name if not isinstance(source, Image.Image) else 'image'}) "
                  f"enlarged {enlarged:.1f}× to fill {placement[2]:g}×{placement[3]:g}"
                  f"{'; a bigger original would be sharper' if enlarged > 1.5 else ''}", file=sys.stderr)
        keep = min(1.0, (max_scale * placement[2] / cw) if max_scale else 1.0)
        stored = (max(1, round(cw * keep)), max(1, round(ch * keep)))
        with tempfile.TemporaryDirectory() as folder:
            original_png, graded_png = Path(folder) / "original.png", Path(folder) / "graded.png"
            report = develop(source, preset, graded_png, original=original_png, crop=crop, size=stored,
                             amount=amount, settings=settings)
            original_pixels, graded_pixels = Image.open(original_png), Image.open(graded_png)
            original_pixels.load(); graded_pixels.load()
        as_shot = report.get("asShot")
        original_name = f"{group.name}{ORIGINAL}" + (f" (as shot {as_shot[0]:.0f} K, {as_shot[1]:+.0f})" if as_shot else "")
        box = transform(*placement)
        if parts:
            parts["original"].name = original_name
            parts["original"].set_pixels(original_pixels)
            parts["original"].record["transform"] = box
            parts["graded"].set_pixels(graded_pixels)
            parts["graded"].record["transform"] = box
            graded = parts["graded"]
        else:
            group.record["transform"] = copy.deepcopy(box)
            original = self.add_image(original_pixels, original_name, fit=None, parent=group)
            original.record["transform"] = copy.deepcopy(box)
            original.visible = False
            graded = self.add_image(graded_pixels, f"{group.name}{BASE_GRADE}", fit=None, parent=group)
            graded.record["transform"] = copy.deepcopy(box)
            if tune:
                for kind in TUNE_KINDS:
                    layer = self.add_adjustment(kind, f"{TUNE}{kind}", parent=group)
                    layer.record["transform"] = copy.deepcopy(box)
        self._finish_graded(group, graded, report, preset, amount, local=local, settings=settings)
        return report

    def _finish_graded(self, group: Layer, graded: Layer, report: dict, preset, amount: float, local: bool = True,
                       settings: dict | None = None) -> None:
        """Names the base grade after its preset, strength and overrides, and rebuilds the preset's local
        corrections above it."""
        details = [report["preset"] + ("" if amount == 1 else f" at {amount:.0%}")]
        details += [f"{field} {value:+g}" for field, value in (settings or {}).items()]
        graded.name = f"{group.name}{BASE_GRADE} ({', '.join(details)})"
        for layer in group.graded_parts()["local"]:
            self.remove(layer)
        box = graded.transform
        notes = []
        if local:
            above = graded
            for correction in read_preset(preset)["localCorrections"]:
                if not correction["active"]:
                    continue
                stops = correction["settings"].get("LocalExposure2012", 0) * amount
                others = [k for k, v in correction["settings"].items()
                          if v and k not in ("LocalExposure2012", "LocalCurveRefineSaturation")]
                if others:
                    notes.append(f"local correction {correction['name']!r}: only its exposure is kept ({', '.join(others)} aren't)")
                long = max(box["size"])
                weight = _local_weight(correction, (max(1, round(512 * box["size"][0] / long)), max(1, round(512 * box["size"][1] / long))))
                if weight is None:
                    notes.append(f"local correction {correction['name']!r}: its mask kind can't be drawn here")
                    continue
                if not stops:
                    continue
                layer = self.add_adjustment("Exposure", f"{LOCAL}{correction['name']} ({stops:+.2f} EV)",
                                            exposureSettings={"exposure": round(stops, 4)}, parent=group, above=above)
                layer.record["transform"] = copy.deepcopy(box)
                layer.set_mask((weight * 255).round().astype(np.uint8))
                above = layer
        for line in notes + [f"{report['preset']}: {item}" for item in report.get("skipped", [])]:
            if line not in _reported:
                _reported.add(line)
                print(f"compkit: {line}", file=sys.stderr)

    def remove(self, layer: Layer) -> None:
        """Removes a layer, and everything inside it if it's a folder."""
        doomed = {layer.id}
        changed = True
        while changed:
            changed = False
            for record in self.manifest["layers"]:
                if record.get("parentID") in doomed and record["id"] not in doomed:
                    doomed.add(record["id"]); changed = True
        self.manifest["layers"] = [r for r in self.manifest["layers"] if r["id"] not in doomed]
        for record in self.manifest["layers"]:
            if record.get("maskSourceID") in doomed:
                record.pop("maskSourceID")
        for id in doomed:
            self._images.pop(id, None); self._masks.pop(id, None)
        if self.manifest.get("activeLayerID") in doomed:
            self.manifest["activeLayerID"] = self.manifest["layers"][-1]["id"] if self.manifest["layers"] else None

    def move(self, layer: Layer, above: Layer | None = None, below: Layer | None = None, to_top: bool = False,
             to_bottom: bool = False) -> None:
        """Moves a layer just above or below another (joining that one's folder), or to the top or bottom of its
        own folder (of the whole stack, for a layer outside any folder)."""
        layers = self.manifest["layers"]
        layers.remove(layer.record)
        parent = layer.record.get("parentID")
        if above is not None or below is not None:
            other = above if above is not None else below
            index = next(i for i, r in enumerate(layers) if r["id"] == other.id) + (1 if above is not None else 0)
            layers.insert(index, layer.record)
            if other.record.get("parentID"):
                layer.record["parentID"] = other.record["parentID"]
            else:
                layer.record.pop("parentID", None)
        elif to_top or to_bottom:
            siblings = [i for i, r in enumerate(layers) if r.get("parentID") == parent]
            if not siblings:
                layers.append(layer.record)
            elif to_top:
                layers.insert(siblings[-1] + 1, layer.record)
            else:
                layers.insert(siblings[0], layer.record)
        else:
            raise CompError("move needs above=, below=, to_top=True or to_bottom=True")

    def add_guide(self, axis: str, position: float) -> None:
        if axis not in ("horizontal", "vertical"):
            raise CompError("axis is 'horizontal' or 'vertical'")
        self.manifest.setdefault("guides", []).append({"id": new_id(), "axis": axis, "position": float(position)})

    # Checking

    def check(self) -> None:
        """The format's rules (ProjectStore.validate), checked before writing; the app refuses a file that breaks
        one without saying why."""
        m = self.manifest
        if not (1 <= m["width"] <= MAX_SIDE and 1 <= m["height"] <= MAX_SIDE):
            raise CompError("canvas too large")
        records = {r["id"]: r for r in m["layers"]}
        if len(records) != len(m["layers"]):
            raise CompError("two layers share an ID")
        for r in m["layers"]:
            where = f"layer {r.get('name')!r}"
            if r["id"] != r["id"].upper():
                raise CompError(f"{where}: IDs must be uppercase")
            if not r["name"].strip():
                raise CompError(f"{where}: empty name")
            if r.get("imageFile") not in (None, f"{r['id']}.png"):
                raise CompError(f"{where}: imageFile must be {r['id']}.png")
            if r.get("maskFile") not in (None, f"{r['id']}.mask.png"):
                raise CompError(f"{where}: maskFile must be {r['id']}.mask.png")
            if r.get("maskEnabled") is not None and not r.get("maskFile"):
                raise CompError(f"{where}: maskEnabled without a mask")
            if not 0 <= r.get("opacity", 1) <= 1:
                raise CompError(f"{where}: opacity runs from 0 to 1")
            if r.get("blendMode", "Normal") not in BLEND_MODES:
                raise CompError(f"{where}: unknown blend mode {r.get('blendMode')!r}")
            if r.get("isGroup"):
                if r.get("imageFile") or r.get("adjustment") or r.get("text"):
                    raise CompError(f"{where}: a folder has no pixels, adjustment or text")
                if r.get("blendMode", "Normal") != "Normal":
                    raise CompError(f"{where}: a folder's blend mode stays Normal")
            if r.get("adjustment") and (r.get("imageFile") or r.get("text")):
                raise CompError(f"{where}: an adjustment layer has no pixels or text")
            if r.get("text") and not r.get("imageFile"):
                raise CompError(f"{where}: a text layer needs its pixels")
            parent, seen = r.get("parentID"), {r["id"]}
            while parent:
                if parent in seen or parent not in records or not records[parent].get("isGroup") or len(seen) > 64:
                    raise CompError(f"{where}: its folder is missing, not a folder, or nested too deep")
                seen.add(parent)
                parent = records[parent].get("parentID")
            source = r.get("maskSourceID")
            if source and (source == r["id"] or source not in records or records[source].get("isGroup")):
                raise CompError(f"{where}: its clipping base is missing or a folder")
            (ox, oy), (w, h) = r["transform"]["origin"], r["transform"]["size"]
            if not (1 <= w <= 300_000 and 1 <= h <= 300_000 and abs(ox) <= 1e6 and abs(oy) <= 1e6):
                raise CompError(f"{where}: transform out of range")
            for file, kind in ((r.get("imageFile"), "_images"), (r.get("maskFile"), "_masks")):
                if file and r["id"] not in getattr(self, kind) and not (self.path and (self.path / "images" / file).exists()):
                    raise CompError(f"{where}: images/{file} is missing")
        if m.get("activeLayerID") and m["activeLayerID"] not in records:
            raise CompError("activeLayerID names no layer")

    # Writing

    def save(self, path=None, validate: bool = True) -> Path:
        """Writes the project. Saving over the project it was opened from (and that the app may have open) writes
        the images first, then swaps the manifest in atomically, so the canvas updates in one step. Saving
        somewhere new builds the whole package aside and moves it into place. With validate, the written package
        is then loaded with the app's own loader, and a CompError raised if the app would refuse it."""
        target = Path(path).expanduser().resolve() if path else self.path
        if target is None:
            raise CompError("save needs a path for a new project")
        if target.suffix != ".comp":
            raise CompError("a Compositor project's name ends in .comp")
        self.check()
        if self.manifest.get("activeLayerID") is None and self.manifest["layers"]:
            self.manifest["activeLayerID"] = self.manifest["layers"][-1]["id"]
        self.manifest["version"] = max(self.manifest.get("version", 1), defaults()["formatVersion"])
        if target == self.path and target.exists():
            self._write_into(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{target.stem}-", suffix=".comp", dir=target.parent))
            try:
                (staging / "images").mkdir()
                if self.path:
                    for record in self.manifest["layers"]:
                        for key in ("imageFile", "maskFile"):
                            file = record.get(key)
                            source = self.path / "images" / file if file else None
                            pending = self._images if key == "imageFile" else self._masks
                            if file and record["id"] not in pending and source.exists():
                                shutil.copy2(source, staging / "images" / file)
                self._write_into(staging)
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            self.path = target
        self._images.clear()
        self._masks.clear()
        if validate:
            comp_render("validate", target)
        return target

    def _write_into(self, package: Path) -> None:
        images = package / "images"
        images.mkdir(exist_ok=True)
        for id, image in self._images.items():
            image.save(images / f"{id}.png", icc_profile=SRGB_ICC)
        for id, mask in self._masks.items():
            mask.save(images / f"{id}.mask.png")
        temporary = package / ".manifest.json.tmp"
        temporary.write_text(json.dumps(self.manifest, indent=2, sort_keys=True))
        os.replace(temporary, package / "manifest.json")
        referenced = {r.get(k) for r in self.manifest["layers"] for k in ("imageFile", "maskFile")}
        for file in images.iterdir():
            if file.name not in referenced:
                file.unlink()
        shutil.rmtree(package / "QuickLook", ignore_errors=True)

    def render(self, output, *, max_side: int | None = None, quality: float = 0.9, background: str = "FFFFFF") -> Path:
        """Flattens the saved project to PNG or JPEG with the app's renderer. Save first."""
        if self.path is None or self._images or self._masks:
            raise CompError("save the project before rendering it")
        output = Path(output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        args = ["render", self.path, output, "--quality", str(quality), "--background", background.lstrip("#")]
        if max_side:
            args += ["--max-side", str(max_side)]
        comp_render(*args)
        return output

    def open_in_app(self) -> None:
        """Opens the saved project in Compositor, where it then updates live as it's saved again."""
        subprocess.run(["open", "-a", "Compositor", str(self.path)], check=True)
