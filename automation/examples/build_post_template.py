"""Builds a 1080×1350 social post as an editable Compositor template, then renders it.

    automation/.venv/bin/python automation/examples/build_post_template.py [photo] [output.comp]

Every part a batch job would change has a stable layer name: Photo, Headline, Subhead, Tag. Open the .comp in
Compositor to adjust the design by hand; fill_template.py and batch.py then reuse it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from compkit import Project

HERE = Path(__file__).resolve().parent
photo = sys.argv[1] if len(sys.argv) > 1 else "/System/Library/Desktop Pictures/Sonoma.heic"
output = Path(sys.argv[2] if len(sys.argv) > 2 else HERE / "out" / "post-template.comp")

p = Project.new(1080, 1350)

# Picture and grade
p.add_image(photo, name="Photo", fit="cover", focus=(0.5, 0.4))
p.add_adjustment("Curves", name="Warm Grade",
                 curves={"red": [(0, 0), (120, 138), (255, 255)], "blue": [(0, 0), (128, 112), (255, 240)]})
p.add_adjustment("Hue/Saturation", name="Punch", saturation=12)

# Legibility: a dark fade behind the type, and an edge vignette made with a mask
p.add_gradient([(0.0, "#000000", 0.0), (0.45, "#000000", 0.0), (1.0, "#000000", 0.85)], name="Bottom Fade", angle=270)
yy, xx = np.mgrid[0:1350, 0:1080]
distance = np.sqrt(((xx - 540) / 540) ** 2 + ((yy - 675) / 675) ** 2)
vignette = p.add_fill("#000000", name="Vignette", opacity=0.55, blend="Multiply")
vignette.set_mask(np.clip((distance - 0.55) / 0.6, 0, 1) * 255)

# Type, in a folder
title = p.add_group("Title")
p.add_fill("#E4FF3A", name="Accent Bar", x=80, y=1028, width=120, height=10, parent=title)
p.add_text("FIT FOCUS", name="Tag", x=80, y=1000, font="Helvetica-Bold", size=34, color="#E4FF3A",
           tracking=6, parent=title)
headline = p.add_text("New personal best", name="Headline", x=80, y=1050, font="Helvetica-Bold", size=96,
                      color="#FFFFFF", box=(920, 130), parent=title)
headline.set_effect("shadow", opacity=0.45, distance=6, blur=18)
p.add_text("Squat 220 kg  ·  Week 12 of the block", name="Subhead", x=80, y=1240, font="Helvetica", size=38,
           color="#FFFFFF", opacity=0.85, parent=title)

p.add_adjustment("Grain", name="Film Grain", opacity=0.6)
p.add_guide("vertical", 80)
p.add_guide("vertical", 1000)

saved = p.save(output)
print("saved", saved)
print("rendered", p.render(saved.with_suffix(".jpg"), quality=0.92))
