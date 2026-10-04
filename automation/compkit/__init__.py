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
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageCms, ImageOps

__all__ = ["Project", "Layer", "CompError", "comp_render", "defaults", "subject_mask", "BLEND_MODES"]

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
    return {"origin": [x, y], "size": [width, height], "rotation": rotation,
            "flipX": flip_x, "flipY": flip_y, "sampling": sampling}


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


def fit_image(image: Image.Image, box: tuple[float, float], fit: str = "cover",
              focus: tuple[float, float] = (0.5, 0.5), max_scale: float | None = 2.0) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Fits `image` to a box of `box` size. Returns the pixels to store and where they sit inside the box
    (x, y, width, height, relative to the box's top-left).

    cover    fills the box, cropping the overflow around `focus` (0–1 on each axis; 0.5, 0.5 is the center)
    contain  fits inside the box, centered
    stretch  fills the box exactly, ignoring aspect ratio
    The stored pixels keep the source's resolution, capped at `max_scale` times the box's size (None: no cap)."""
    bw, bh = box
    w, h = image.size
    if fit == "cover":
        target = bw / bh
        if w / h > target:
            cw, ch = max(1, round(h * target)), h
        else:
            cw, ch = w, max(1, round(w / target))
        left = round((w - cw) * min(1, max(0, focus[0])))
        top = round((h - ch) * min(1, max(0, focus[1])))
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
        t = self.transform
        if x is not None: t["origin"][0] = x
        if y is not None: t["origin"][1] = y
        if width is not None: t["size"][0] = width
        if height is not None: t["size"][1] = height
        if rotation is not None: t["rotation"] = rotation
        if flip_x is not None: t["flipX"] = flip_x
        if flip_y is not None: t["flipY"] = flip_y
        if sampling is not None: t["sampling"] = sampling
        return self

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

    def replace_image(self, source, fit: str = "cover", focus=(0.5, 0.5), max_scale: float | None = 2.0) -> "Layer":
        """Puts a new picture in this layer's box (its current transform), as a template's placeholder: position,
        rotation, mask, effects, opacity and blend mode all stay."""
        t = self.transform
        image, (px, py, pw, ph) = fit_image(load_image(source), tuple(t["size"]), fit, focus, max_scale)
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

    def mask_subject(self, refine: float = 12, contrast: float = 25, shift: float = 0) -> "Layer":
        """Remove Background as a layer mask: hides everything but the subject Apple Vision finds in the layer's
        pixels, without erasing them (paint the mask in Compositor to fix it up)."""
        pixels = self.pixels()
        if pixels is None:
            raise CompError(f"{self.name!r} has no pixels")
        return self.set_mask(subject_mask(pixels, refine=refine, contrast=contrast, shift=shift))

    # Text

    def set_text(self, content: str | None = None, **style) -> "Layer":
        """Changes a text layer's words or style (font, size, color, align, tracking, leading, box) and sets its
        pixels again with the app's typesetter, keeping its top-left corner, scale and rotation as the app does."""
        if not self.text:
            raise CompError(f"{self.name!r} is not a text layer")
        old = self.text
        new = _text_style(old, content, style)
        image, placement = _typeset(new)
        old_pixels = self.pixels()
        t = self.transform
        anchor = _point(t, (0, 0))
        if new.get("boxSize") is None or old.get("boxSize") is None:
            sx = t["size"][0] / old_pixels.width if old_pixels else 1
            sy = t["size"][1] / old_pixels.height if old_pixels else 1
            t["size"] = [image.width * sx, image.height * sy]
            moved = _point(t, (0, 0))
            t["origin"] = [t["origin"][0] + anchor[0] - moved[0], t["origin"][1] + anchor[1] - moved[1]]
        self.project._images[self.id] = image
        self.record["text"] = new
        return self

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
            if value is None:
                result.pop("boxSize", None)
            else:
                result["boxSize"] = [value[0], value[1]]
        else:
            result[key] = value
    return result


def _typeset(style: dict) -> tuple[Image.Image, dict]:
    with tempfile.TemporaryDirectory() as folder:
        style_file, png = Path(folder) / "style.json", Path(folder) / "text.png"
        style_file.write_text(json.dumps(style))
        placement = json.loads(comp_render("text", style_file, png))
        image = Image.open(png).convert("RGBA")
        image.load()
    return image, placement


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

    def _add(self, record: dict, parent: Layer | None, above: Layer | None) -> Layer:
        if parent is not None:
            if not parent.is_group:
                raise CompError(f"{parent.name!r} is not a folder")
            record["parentID"] = parent.id
        layers = self.manifest["layers"]
        if above is not None:
            index = next(i for i, r in enumerate(layers) if r["id"] == above.id) + 1
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
                  focus=(0.5, 0.5), max_scale: float | None = 2.0, opacity: float = 1, blend: str = "Normal",
                  parent: Layer | None = None, above: Layer | None = None) -> Layer:
        """Adds a picture. With no box it fills the canvas (fit='cover'); give x, y, width, height for a box.
        fit=None places it at its own pixel size at x, y."""
        image = load_image(source)
        if name is None:
            name = Path(source).stem if not isinstance(source, Image.Image) else "Layer"
        if fit is None:
            box = (x, y, width or image.width, height or image.height)
        else:
            bw = width if width is not None else (self.width if height is None else image.width * height / image.height)
            bh = height if height is not None else (self.height if width is None else image.height * width / image.width)
            image, (px, py, pw, ph) = fit_image(image, (bw, bh), fit, focus, max_scale)
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
        box      (width, height) makes paragraph text that wraps inside the box; x, y is then the box's top-left
        anchor   for point text: 'baseline' puts the first baseline at y, starting at x (center/right
                 alignment: x is the line's center/right end), as a click with the Type tool does;
                 'top-left' puts the text's top-left at x, y"""
        style = _text_style(defaults()["text"], content, {"font": font, "size": size, "color": color, "align": align,
                                                           "tracking": tracking, "leading": leading, "box": box})
        image, placement = _typeset(style)
        pad = placement["padding"]
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

    def move(self, layer: Layer, above: Layer | None = None, to_top: bool = False) -> None:
        """Moves a layer just above another (within that one's folder), or to the top of the stack."""
        layers = self.manifest["layers"]
        layers.remove(layer.record)
        if to_top or above is None:
            layers.append(layer.record)
            layer.record.pop("parentID", None)
        else:
            index = next(i for i, r in enumerate(layers) if r["id"] == above.id) + 1
            layers.insert(index, layer.record)
            if above.record.get("parentID"):
                layer.record["parentID"] = above.record["parentID"]
            else:
                layer.record.pop("parentID", None)

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
