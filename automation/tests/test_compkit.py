"""Checks compkit against the app itself: every project written here must pass `comp-render validate`, which loads
it with the app's own ProjectStore, and render with its ImageExporter.

    automation/.venv/bin/python -m unittest discover -s automation/tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import CompError, Project, comp_render, defaults  # noqa: E402


class CompkitTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.photo = self.folder / "photo.png"
        pixels = np.zeros((300, 400, 3), np.uint8)
        pixels[:, :200] = (255, 0, 0)
        pixels[:, 200:] = (0, 0, 255)
        Image.fromarray(pixels).save(self.photo)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def pixel(self, render: Path, x: int, y: int):
        return Image.open(render).convert("RGB").getpixel((x, y))

    def test_every_adjustment_kind_loads_and_renders(self):
        p = Project.new(64, 64)
        p.add_fill("#808080")
        for kind in defaults()["adjustments"]:
            p.add_adjustment(kind)
        p.save(self.folder / "adjust.comp")
        p.render(self.folder / "adjust.png")

    def test_every_effect_loads(self):
        p = Project.new(200, 200)
        layer = p.add_fill("#FF0000", x=50, y=50, width=100, height=100)
        for kind in defaults()["effects"]:
            layer.set_effect(kind, color="#00FF00")
        p.save(self.folder / "effects.comp")
        p.render(self.folder / "effects.png")

    def test_blend_modes_all_load(self):
        p = Project.new(32, 32)
        p.add_fill("#336699")
        for mode in defaults()["blendModes"]:
            p.add_fill("#CC8844", name=mode, blend=mode, opacity=0.5)
        p.save(self.folder / "blend.comp")

    def test_fill_renders_its_color(self):
        p = Project.new(10, 10)
        p.add_fill("#FF8000")
        p.save(self.folder / "fill.comp")
        r, g, b = self.pixel(p.render(self.folder / "fill.png"), 5, 5)
        self.assertEqual((r, b), (255, 0))
        self.assertAlmostEqual(g, 128, delta=2)

    def test_cover_crops_around_focus(self):
        p = Project.new(100, 300)  # tall box from a wide red|blue picture
        p.add_image(self.photo, fit="cover", focus=(0, 0.5))
        p.save(self.folder / "left.comp")
        self.assertEqual(self.pixel(p.render(self.folder / "left.png"), 50, 150), (255, 0, 0))
        q = Project.new(100, 300)
        q.add_image(self.photo, fit="cover", focus=(1, 0.5))
        q.save(self.folder / "right.comp")
        self.assertEqual(self.pixel(q.render(self.folder / "right.png"), 50, 150), (0, 0, 255))

    def test_contain_letterboxes(self):
        p = Project.new(400, 400)
        layer = p.add_image(self.photo, fit="contain")
        self.assertEqual(layer.transform["size"], [400, 300])
        self.assertEqual(layer.transform["origin"], [0, 50])

    def test_mask_hides(self):
        p = Project.new(20, 20)
        p.add_fill("#FFFFFF")
        top = p.add_fill("#000000")
        mask = np.zeros((20, 20), np.uint8)
        mask[:, 10:] = 255
        top.set_mask(mask)
        p.save(self.folder / "mask.comp")
        render = p.render(self.folder / "mask.png")
        self.assertEqual(self.pixel(render, 2, 10), (255, 255, 255))
        self.assertEqual(self.pixel(render, 17, 10), (0, 0, 0))

    def test_folder_clip_and_reorder(self):
        p = Project.new(50, 50)
        p.add_fill("#FFFFFF", name="Paper")
        folder = p.add_group("Group", opacity=0.5)
        base = p.add_fill("#000000", name="Base", x=0, y=0, width=25, height=50, parent=folder)
        clipped = p.add_fill("#FF0000", name="Clipped", parent=folder)
        clipped.clip_to(base)
        p.add_fill("#00FF00", name="Top")
        p.move(p.layer("Top"), above=p.layer("Paper"))
        p.save(self.folder / "tree.comp")
        render = p.render(self.folder / "tree.png")
        # Inside the base the clipped red shows (dimmed by the folder: its opacity multiplies into each layer, so a
        # clipped layer gets it through its base too); outside the base it's clipped away, leaving only green.
        r, g, b = self.pixel(render, 10, 25)
        self.assertGreater(r, 40); self.assertLess(b, 10)
        self.assertEqual(self.pixel(render, 40, 25), (0, 255, 0))
        p.remove(folder)
        self.assertEqual([l.name for l in p.layers], ["Paper", "Top"])
        p.save()

    def test_text_round_trip_keeps_anchor(self):
        p = Project.new(800, 400)
        text = p.add_text("Hello", x=100, y=200, font="Helvetica-Bold", size=60, color="#FFF")
        before = list(text.transform["origin"])
        p.save(self.folder / "text.comp")
        q = Project.open(self.folder / "text.comp")
        q.layer("Hello").set_text("Hello, much longer line", color="#F00")
        self.assertEqual(q.layer("Hello").transform["origin"], before)
        self.assertGreater(q.layer("Hello").transform["size"][0], text.transform["size"][0])
        q.save()
        info = json.loads(comp_render("info", self.folder / "text.comp"))
        self.assertEqual(info["layers"][0]["text"]["content"], "Hello, much longer line")

    def test_template_fill_copies_into_new_package(self):
        p = Project.new(200, 200)
        p.add_image(self.photo, name="Photo")
        p.add_text("Title", name="Headline", x=10, y=180, size=30)
        p.save(self.folder / "template.comp")
        q = Project.open(self.folder / "template.comp")
        q.layer("Headline").set_text("Changed")
        q.save(self.folder / "filled.comp")  # Photo's PNG must be carried over untouched
        self.assertEqual(len(list((self.folder / "filled.comp" / "images").iterdir())), 2)
        self.assertEqual(Project.open(self.folder / "template.comp").layer("Headline").text["content"], "Title")

    def test_subject_mask_on_a_layer(self):
        parrot = Path("/Library/User Pictures/Animals/Parrot.heic")
        if not parrot.exists():
            self.skipTest("no sample photo")
        p = Project.new(512, 512)
        p.add_fill("#00FF00", name="Backdrop")
        p.add_image(parrot, name="Subject").mask_subject()
        p.save(self.folder / "cutout.comp")
        render = p.render(self.folder / "cutout.png")
        self.assertEqual(self.pixel(render, 5, 5), (0, 255, 0))         # background masked away
        self.assertNotEqual(self.pixel(render, 350, 250), (0, 255, 0))  # the bird stays

    def test_invalid_projects_are_refused_before_writing(self):
        p = Project.new(10, 10)
        layer = p.add_fill("#000")
        layer.record["blendMode"] = "Glow"
        with self.assertRaises(CompError):
            p.save(self.folder / "bad.comp")
        self.assertFalse((self.folder / "bad.comp").exists())

    def test_validate_reports_the_apps_reason(self):
        p = Project.new(10, 10)
        p.add_fill("#000")
        path = p.save(self.folder / "broken.comp")
        for file in (path / "images").iterdir():
            file.unlink()
        with self.assertRaises(CompError) as raised:
            comp_render("validate", path)
        self.assertIn("broken.comp", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
