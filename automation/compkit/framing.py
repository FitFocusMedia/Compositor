"""Suggested framing for a delivered photo, the two fixes a photographer most often makes in Lightroom's crop tool:
level it when Vision finds a clearly tilted horizon, and crop in, at the photo's own shape, when the people in it
leave a lot of empty frame. Deliberately conservative: on a posed shoot the photographer cropped 8% of the frames
they delivered (keeping 47–97% of the width, never changing the shape) and straightened none.

A suggestion is {"rotate": degrees counterclockwise (0 = leave level), "crop": (x, y, w, h) fractions of the leveled
picture or None, "why": [...]}, as `develop(rotate=…, crop=…)` and graded photos take them.
"""

from __future__ import annotations

import math


def inscribed_scale(width: float, height: float, degrees: float) -> float:
    """The share of a picture's width (and height) left after leveling it by `degrees`: the largest centered
    rectangle of its own shape that the turned picture fills (as comp-render's Straighten keeps)."""
    if not degrees:
        return 1.0
    angle = math.radians(abs(degrees))
    c, s = math.cos(angle), math.sin(angle)
    return min(width / (width * c + height * s), height / (width * s + height * c))

# What counts as people: Vision's whole-body boxes it's reasonably sure of, else faces.
PERSON_CONFIDENCE = 0.4


def _union(boxes):
    if not boxes:
        return None
    left = min(b["x"] for b in boxes)
    top = min(b["y"] for b in boxes)
    right = max(b["x"] + b["width"] for b in boxes)
    bottom = max(b["y"] + b["height"] for b in boxes)
    return left, top, right, bottom


def suggest(analysis: dict, *, level_from: float = 1.0, level_to: float = 8.0, tightest: float = 0.7,
            least: float = 0.1, headroom: float = 0.12, sides: float = 0.12, below: float = 0.06) -> dict:
    """The framing to suggest for one photo from its `analyze()` result (faces, people, subject, level).
    - Level: only a horizon tilted between `level_from` and `level_to` degrees (smaller is noise, larger is a
      deliberate angle or a misread).
    - Crop: the people (or faces, or Vision's subject) with `headroom` above, `sides` either side and `below`
      under them (none if they reach the bottom edge), at the photo's own shape, never tighter than `tightest` of
      its width and only when that removes at least `least` of it."""
    why = []
    rotate = 0.0
    level = analysis.get("level")
    if level is not None and level_from <= abs(level) <= level_to:
        rotate = round(float(level), 2)
        why.append(f"leveled {rotate:+.1f}°")
    people = [b for b in analysis.get("people", []) if (b.get("confidence") or 1) >= PERSON_CONFIDENCE]
    content = _union(people)
    if content is None and analysis.get("faces"):
        # Faces alone: allow for the body below them.
        left, top, right, bottom = _union(analysis["faces"])
        height = bottom - top
        content = (left - height, top, right + height, min(1.0, bottom + 4 * height))
    if content is None and analysis.get("subject") and (analysis["subject"].get("coverage") or 0) >= 0.02:
        s = analysis["subject"]
        content = (s["x"], s["y"], s["x"] + s["width"], s["y"] + s["height"])
    if content is None:
        return {"rotate": rotate, "crop": None, "why": why}
    left, top, right, bottom = content
    # Leveling trims the picture: the same content takes a larger share of what's left.
    keep = inscribed_scale(analysis.get("width") or 3, analysis.get("height") or 2, rotate)
    to_leveled = lambda v: (v - 0.5) / keep + 0.5
    left, top, right, bottom = map(to_leveled, (left, top, right, bottom))
    width, height = right - left, bottom - top
    left -= sides * width
    right += sides * width
    top -= headroom * height
    bottom = 1.0 if bottom >= 0.97 else bottom + below * height
    left, top, right, bottom = max(0.0, left), max(0.0, top), min(1.0, right), min(1.0, bottom)
    size = max(right - left, bottom - top, tightest)
    if size > 1 - least:
        return {"rotate": rotate, "crop": None, "why": why}
    x = min(max(0.0, (left + right) / 2 - size / 2), 1 - size)
    # Keep the headroom: anchor the top of the people near the crop's top rather than centering vertically.
    y = min(max(0.0, top - 0.02), 1 - size)
    if bottom > y + size:
        y = min(max(0.0, bottom - size), 1 - size)
    why.append(f"cropped in to {size:.0%} around {len(people) or 'the'} {'people' if people else 'subject'}")
    return {"rotate": rotate, "crop": (round(x, 4), round(y, 4), round(size, 4), round(size, 4)), "why": why}
