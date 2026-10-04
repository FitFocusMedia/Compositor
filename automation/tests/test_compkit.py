"""Checks compkit against the app itself: every project written here must pass `comp-render validate`, which loads
it with the app's own ProjectStore, and render with its ImageExporter.

    automation/.venv/bin/python -m unittest discover -s automation/tests -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import math

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import (CompError, Project, analyze, auto_focus, comp_render, cover_crop, defaults, develop, measure,  # noqa: E402
                     read_preset)

PRESET = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:WhiteBalance="Custom" crs:Temperature="6000" crs:Tint="+20" crs:AsShotTemperature="5000" crs:AsShotTint="0"
   crs:Exposure2012="+1.00" crs:Clarity2012="+20" crs:HueAdjustmentOrange="-10" crs:Sharpness="40"
   crs:SharpenRadius="+1.0" crs:LensProfileEnable="1" crs:FooBar="3" crs:ToneCurveName2012="Custom">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Test Grade</rdf:li></rdf:Alt></crs:Name>
   <crs:ToneCurvePV2012Red><rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>128, 150</rdf:li><rdf:li>255, 255</rdf:li></rdf:Seq></crs:ToneCurvePV2012Red>
   <crs:MaskGroupBasedCorrections><rdf:Seq><rdf:li><rdf:Description crs:What="Correction" crs:CorrectionActive="true"
     crs:CorrectionAmount="1" crs:CorrectionName="Center" crs:LocalExposure2012="0.5">
     <crs:CorrectionMasks><rdf:Seq><rdf:li crs:What="Mask/CircularGradient" crs:MaskInverted="false" crs:Flipped="true"
       crs:MaskValue="1" crs:Top="0.25" crs:Left="0.25" crs:Bottom="0.75" crs:Right="0.75" crs:Angle="0" crs:Feather="50"/>
     </rdf:Seq></crs:CorrectionMasks></rdf:Description></rdf:li></rdf:Seq></crs:MaskGroupBasedCorrections>
   <crs:Look><rdf:Description crs:Name="Adobe Color"/></crs:Look>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def box(x, y, width, height, **extra):
    return {"x": x, "y": y, "width": width, "height": height, **extra}


def shot(path, seed, when, blur=0, shift=0):
    """A synthetic photo with a capture time: shapes on a fine texture, so it has detail to be sharp or soft."""
    from PIL import ImageDraw, ImageFilter
    rng = np.random.default_rng(seed)
    base = (rng.random((600, 800, 3)) * 60 + 90).astype(np.uint8)
    image = Image.fromarray(base)
    draw = ImageDraw.Draw(image)
    for _ in range(6):
        x, y = rng.integers(0, 700), rng.integers(0, 500)
        draw.ellipse((x + shift, y, x + shift + 120, y + 90), fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    exif = image.getexif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    ifd[0x9291] = f"{when.microsecond // 1000:03d}"
    image.save(path, quality=92, exif=exif)
    return path


class AutoFocusTests(unittest.TestCase):
    """The crop rules, on detections laid out by hand (the sample photos have no human faces)."""

    def crop(self, analysis, size, target):
        left, top, width, height = cover_crop(size, target, auto_focus(analysis, size, target))
        return left / size[0], top / size[1], (left + width) / size[0], (top + height) / size[1]

    def test_nothing_found_crops_the_center(self):
        self.assertEqual(auto_focus({}, (2000, 1000), (1000, 1000)), (0.5, 0.5))

    def test_same_shape_needs_no_focus(self):
        self.assertEqual(auto_focus({"faces": [box(0.9, 0.9, 0.05, 0.05)]}, (1000, 1000), (500, 500)), (0.5, 0.5))

    def test_a_subject_that_fits_is_centered(self):
        found = {"subject": box(0.70, 0.2, 0.2, 0.6, coverage=0.1)}  # right side of a wide photo
        left, _, right, _ = self.crop(found, (3000, 1000), (1000, 1000))
        self.assertAlmostEqual((left + right) / 2, 0.8, places=2)

    def test_a_face_sits_in_the_top_third_of_a_tall_subject(self):
        # A full-length athlete in a portrait photo, cropped to a wide banner.
        found = {"faces": [box(0.45, 0.30, 0.1, 0.06, confidence=0.9)],
                 "subject": box(0.3, 0.25, 0.4, 0.75, coverage=0.2)}
        _, top, _, bottom = self.crop(found, (1000, 2000), (1600, 500))
        self.assertLessEqual(top, 0.30)
        self.assertGreaterEqual(bottom, 0.36)
        self.assertGreaterEqual(top, 0.25 - 1e-6)  # no empty space above the subject
        self.assertLess((0.33 - top) / (bottom - top), 0.5)  # in the upper half of the crop

    def test_every_face_stays_whole(self):
        # Two people at the edges of a wide group shot, cropped to a 4:5 post.
        found = {"faces": [box(0.10, 0.3, 0.08, 0.15, confidence=0.9), box(0.42, 0.3, 0.08, 0.15, confidence=0.9)],
                 "subject": box(0.05, 0.2, 0.9, 0.8, coverage=0.4)}
        left, _, right, _ = self.crop(found, (3000, 1500), (1200, 1500))
        self.assertLessEqual(left, 0.10)
        self.assertGreaterEqual(right, 0.50)

    def test_weak_faces_are_ignored(self):
        found = {"faces": [box(0.9, 0.1, 0.05, 0.05, confidence=0.2)], "subject": box(0.1, 0.1, 0.3, 0.8, coverage=0.2)}
        left, _, right, _ = self.crop(found, (3000, 1000), (1000, 1000))
        self.assertLess(right, 0.6)

    def test_without_faces_the_salient_part_of_a_big_subject_is_kept(self):
        found = {"subject": box(0.0, 0.0, 1.0, 1.0, coverage=0.7), "salient": [box(0.7, 0.2, 0.2, 0.3)]}
        left, _, right, _ = self.crop(found, (2000, 1000), (500, 1000))
        self.assertLessEqual(left, 0.7)
        self.assertGreaterEqual(right, 0.9)

    def test_a_real_photo_keeps_the_head(self):
        zebra = Path("/Library/User Pictures/Animals/Zebra.heic")
        if not zebra.exists():
            self.skipTest("no sample photo")
        found = analyze(zebra)
        self.assertIsNotNone(found.get("subject"))
        # The zebra faces right; its head is right of center.
        self.assertGreater(auto_focus(found, (found["width"], found["height"]), (400, 1000))[0], 0.6)


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

    def test_replace_image_crops_automatically(self):
        zebra = Path("/Library/User Pictures/Animals/Zebra.heic")
        if not zebra.exists():
            self.skipTest("no sample photo")
        p = Project.new(400, 1000)
        p.add_image(self.photo, name="Photo")
        p.layer("Photo").replace_image(zebra)  # focus='auto' by default
        p.save(self.folder / "auto.comp")
        p.render(self.folder / "auto.png")

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

    def test_focus_is_the_point_the_crop_centers_on(self):
        image = Image.open(self.photo).convert("RGBA")  # 400×300, red left half, blue right half
        self.assertEqual(cover_crop(image.size, (100, 300), (0.25, 0.5)), (50, 0, 100, 300))
        self.assertEqual(cover_crop(image.size, (100, 300), (0.75, 0.5)), (250, 0, 100, 300))
        self.assertEqual(cover_crop(image.size, (100, 300), (0.0, 0.5))[0], 0)  # held inside the picture
        p = Project.new(100, 300)
        p.add_image(self.photo, focus=(0.25, 0.5))
        p.save(self.folder / "point.comp")
        self.assertEqual(self.pixel(p.render(self.folder / "point.png"), 50, 150), (255, 0, 0))

    def test_a_box_is_the_texts_own_area(self):
        p = Project.new(1000, 600)
        layer = p.add_text("Centered", x=100, y=50, size=60, align="Center", box=(800, 100))
        x, y, w, h = layer.text_area
        self.assertAlmostEqual(x, 100); self.assertAlmostEqual(y, 50)
        self.assertAlmostEqual(w, 800); self.assertAlmostEqual(h, 100)
        ink = layer.ink_box()
        self.assertAlmostEqual(ink[0] + ink[2] / 2, 500, delta=3)  # centered on the area, not 12 px off
        self.assertFalse(layer.text_metrics()["overflow"])  # one 60 px line (72 px leading) fits 100 px

    def test_a_box_without_height_fits_its_text(self):
        p = Project.new(1000, 600)
        layer = p.add_text("Two lines of text that wrap", x=40, y=40, size=50, box=(400, None))
        metrics = layer.text_metrics()
        self.assertFalse(metrics["overflow"])
        self.assertGreater(metrics["textHeight"], 100)  # wrapped
        self.assertAlmostEqual(layer.text_area[3], math.ceil(metrics["textHeight"]))

    def test_overflow_is_reported_when_text_is_added(self):
        p = Project.new(1000, 600)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            p.add_text("Far too many words for such a small box", x=0, y=0, size=60, box=(300, 60))
        self.assertIn("doesn't fit its box", errors.getvalue())
        from compkit.__main__ import tree
        p.save(self.folder / "overflow.comp")
        self.assertIn("OVERFLOWS", tree(Project.open(self.folder / "overflow.comp")))

    def test_a_new_box_size_doesnt_stretch_the_text(self):
        p = Project.new(1000, 600)
        layer = p.add_text("Headline", x=50, y=50, size=60, box=(500, 100))
        layer.set_text(box=(700, 160))
        found = layer.text_metrics()
        self.assertAlmostEqual(layer.transform["size"][0], found["width"])
        self.assertAlmostEqual(layer.transform["size"][1], found["height"])
        self.assertAlmostEqual(layer.text_area[2], 700)

    def test_place_text_and_numpy_numbers(self):
        p = Project.new(1000, 600)
        p.add_fill("#123456", x=np.int64(10), y=np.float32(20), width=np.int64(100), height=np.int64(50))
        text = p.add_text("Hi", x=0, y=0, size=40, anchor="top-left")
        text.place_text(x=np.int64(300), y=np.float64(200))
        self.assertAlmostEqual(text.text_area[0], 300)
        self.assertAlmostEqual(text.text_area[1], 200)
        p.layer("Color Fill").place(x=np.int64(5))
        p.save(self.folder / "numbers.comp")

    def test_enlarged_pictures_are_reported(self):
        p = Project.new(1600, 1600)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            p.add_image(self.photo, name="Small", fit="cover", focus=(0.5, 0.5))
        self.assertIn("enlarged 5.3×", errors.getvalue())

    def test_crop_pads_instead_of_cropping(self):
        from compkit.__main__ import main
        out = self.folder / "crops"
        self.assertEqual(main(["crop", str(self.photo), "--box", "300x900", "--pad", "blur", "--out", str(out)]), 0)
        with Image.open(out / "photo-300x900-pad.jpg") as result:  # beside, not over, a regular crop
            self.assertEqual(result.size, (300, 900))

    def preset(self):
        path = self.folder / "test.xmp"
        path.write_text(PRESET)
        return path

    def gray(self, value=100, size=(200, 200)):
        path = self.folder / "gray.png"
        Image.new("RGB", size, (value, value, value)).save(path)
        return path

    def test_preset_maps_and_reports(self):
        found = read_preset(self.preset())
        self.assertEqual(found["name"], "Test Grade")
        mapped = {e["to"]: e["value"] for e in found["mapped"]}
        self.assertAlmostEqual(mapped["exposure"], 1)
        self.assertAlmostEqual(mapped["clarity"], 20)
        self.assertAlmostEqual(mapped["mixer.hue.orange"], -10)
        self.assertAlmostEqual(mapped["detail.sharpenRadius"], 20)  # 1.0 px of Lightroom's 0.5–3.0
        self.assertIn("curve.red", mapped)
        # 5000 K → 6000 K assumes bluer light, so the picture warms; more tint turns it magenta.
        self.assertGreater(found["whiteBalance"]["temperature"], 5)
        self.assertGreater(found["whiteBalance"]["tint"], 5)
        skipped = " ".join(found["skipped"])
        self.assertIn("Adobe Color", skipped)
        self.assertIn("Lens profile", skipped)
        self.assertIn("FooBar", skipped)
        self.assertEqual(found["localCorrections"][0]["masks"][0]["What"], "Mask/CircularGradient")

    def test_develop_grades_and_scales(self):
        source = self.gray()
        full, none, overridden = (self.folder / n for n in ("full.png", "none.png", "over.png"))
        report = develop(source, self.preset(), full, original=self.folder / "orig.png")
        develop(source, self.preset(), none, amount=0)
        develop(source, self.preset(), overridden, settings={"exposure": 0, "temperature": 0, "tint": 0, "clarity": 0})
        mean = lambda path: np.asarray(Image.open(path).convert("RGB")).astype(float).mean()
        self.assertEqual(report["preset"], "Test Grade")
        self.assertGreater(mean(full), mean(self.folder / "orig.png") + 20)  # +1 stop
        self.assertAlmostEqual(mean(none), 100, delta=1.5)
        self.assertLess(mean(overridden), mean(full) - 20)

    def test_graded_photo_keeps_the_original_and_tune_layers(self):
        p = Project.new(400, 300)
        p.add_fill("#202020", name="Panel")
        photo = p.add_graded_photo(self.photo, self.preset(), name="Photo", x=50, y=50, width=300, height=200)
        parts = photo.graded_parts()
        self.assertFalse(parts["original"].visible)
        self.assertIn("Test Grade", parts["graded"].name)
        self.assertEqual([l.name for l in parts["tune"]], ["Tune · Exposure", "Tune · Curves", "Tune · Hue/Saturation", "Tune · Color Balance"])
        self.assertEqual(len(parts["local"]), 1)
        self.assertTrue(parts["local"][0].record.get("maskFile"))
        self.assertEqual(parts["graded"].transform["size"], [300, 200])
        p.save(self.folder / "graded.comp")
        render = p.render(self.folder / "graded.png")
        self.assertEqual(self.pixel(render, 10, 10), (32, 32, 32))  # the panel outside the photo is untouched

    def test_regrade_keeps_tuning(self):
        p = Project.new(300, 300)
        photo = p.add_graded_photo(self.gray(), self.preset())
        tune = photo.graded_parts()["tune"][0]
        tune.set_adjustment(exposureSettings={"exposure": 0.4})
        photo.regrade(self.preset(), amount=0.5)
        parts = photo.graded_parts()
        self.assertIn("at 50%", parts["graded"].name)
        self.assertEqual(parts["tune"][0].adjustment["exposureSettings"]["exposure"], 0.4)
        self.assertEqual(len(parts["local"]), 1)
        self.assertIn("+0.25 EV", parts["local"][0].name)
        p.save(self.folder / "regraded.comp")

    def test_grading_a_template_layer_and_batch_with_a_preset(self):
        p = Project.new(400, 400)
        p.add_image(self.photo, name="Photo", x=0, y=0, width=400, height=300)
        p.add_text("Title", name="Headline", x=20, y=360, size=30)
        template = p.save(self.folder / "template.comp")
        rows = self.folder / "rows.csv"
        rows.write_text(f"name,image:Photo,text:Headline\none,{self.gray()},Graded\n")
        from compkit.__main__ import main
        out = self.folder / "renders"
        self.assertEqual(main(["batch", str(template), str(rows), "--out", str(out), "--keep-comp",
                               "--preset", str(self.preset())]), 0)
        filled = Project.open(out / "one.comp")
        parts = filled.layer("Photo").graded_parts()
        self.assertIsNotNone(parts)
        self.assertEqual(parts["graded"].transform["size"], [400, 300])
        self.assertEqual(filled.layer("Headline").text["content"], "Graded")

    def test_move_stays_in_its_folder(self):
        p = Project.new(100, 100)
        folder = p.add_group("Badge")
        text = p.add_text("Hi", x=10, y=50, size=20, parent=folder)
        fill = p.add_fill("#FF0", name="Fill", x=5, y=30, width=40, height=30, parent=folder)
        p.add_fill("#000", name="Top")
        p.move(text, to_top=True)
        self.assertEqual(text.record["parentID"], folder.id)
        self.assertEqual([l.name for l in p.children(folder)], ["Fill", "Hi"])
        p.move(text, below=fill)
        self.assertEqual([l.name for l in p.children(folder)], ["Hi", "Fill"])

    def test_backdrop_and_place_ink(self):
        p = Project.new(800, 400)
        folder = p.add_group("Badge")
        text = p.add_text("IN 12 WEEKS", x=100, y=200, size=40, color="#111", parent=folder)
        text.place_ink(x=120, y=150)
        ink = text.ink_box()
        self.assertAlmostEqual(ink[0], 120, delta=0.01); self.assertAlmostEqual(ink[1], 150, delta=0.01)
        plate = text.add_backdrop("#E4FF3A", padding=(20, 10))
        self.assertEqual([l.name for l in p.children(folder)], [plate.name, text.name])  # behind the text, same folder
        self.assertAlmostEqual(plate.transform["origin"][0], 100, delta=0.01)
        self.assertAlmostEqual(plate.transform["size"][0], ink[2] + 40, delta=0.01)
        p.save(self.folder / "badge.comp")

    def test_info_shows_text_boxes_and_sheet_works(self):
        from compkit.__main__ import main, tree
        p = Project.new(400, 200)
        p.add_text("Boxed", x=10, y=10, size=30, box=(300, 60))
        p.add_text("Point", x=10, y=150, size=30)
        p.save(self.folder / "texts.comp")
        listing = tree(Project.open(self.folder / "texts.comp"))
        self.assertIn("box 300×60 at 10,10", listing)
        self.assertIn("point text", listing)
        self.assertEqual(main(["info", str(self.photo)]), 1)  # a photo, not a project: a message, not a traceback
        p.render(self.folder / "a.jpg")
        self.assertEqual(main(["sheet", str(self.folder / "a.jpg"), str(self.photo), "-o", str(self.folder / "sheet.jpg")]), 0)

    def test_measure(self):
        white, gray = self.folder / "white.png", self.folder / "mid.png"
        Image.new("RGB", (50, 50), (255, 255, 255)).save(white)
        Image.new("RGB", (50, 50), (119, 119, 119)).save(gray)  # sRGB 119 is L* 50
        self.assertEqual(measure(white)["blown"], 100)
        self.assertEqual(measure(white)["whites"]["L"], 100)
        found = measure(gray)
        self.assertAlmostEqual(found["lightness"]["median"], 50, delta=0.5)
        self.assertEqual(found["blown"], 0)
        self.assertAlmostEqual(found["cast"]["b"], 0, delta=0.5)
        self.assertEqual(measure(self.photo)["channel_clip"], 100)  # pure red and blue max a channel each
        self.assertEqual(measure(self.photo)["blown"], 0)

    def test_analyze_previews_a_given_focus(self):
        from compkit.__main__ import main
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["analyze", str(self.photo), "--box", "100x300", "--focus", "0.25,0.5"]), 0)
        self.assertEqual(json.loads(output.getvalue())["crop"]["left"], 50)

    def test_regrade_records_overrides_and_sheet_order(self):
        p = Project.new(200, 200)
        photo = p.add_graded_photo(self.gray(), self.preset())
        photo.regrade(self.preset(), amount=0.8, settings={"shadows": -5})
        self.assertIn("Test Grade at 80%, shadows -5", photo.graded_parts()["graded"].name)
        from compkit.__main__ import contact_sheet
        for name in ("mia-alt", "mia", "jack"):
            Image.new("RGB", (10, 10)).save(self.folder / f"{name}.jpg")
        order = []
        import compkit.__main__ as cli
        original = cli.Image.open
        cli.Image.open = lambda path, *a: (order.append(Path(path).stem), original(path, *a))[1]
        try:
            contact_sheet([self.folder / "mia-alt.jpg", self.folder / "mia.jpg", self.folder / "jack.jpg"], self.folder / "s.jpg")
        finally:
            cli.Image.open = original
        self.assertEqual(order, ["jack", "mia", "mia-alt"])

    def test_set_text_warns_when_a_box_overflows(self):
        p = Project.new(800, 400)
        layer = p.add_text("Auto height", x=10, y=10, size=30, box=(400, None))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            layer.set_text(size=60)
        self.assertIn("box=(w, None)", errors.getvalue())
        layer.set_text(size=60, box=(400, None))
        self.assertFalse(layer.text_metrics()["overflow"])

    def test_cull_groups_bursts_and_rejects_blur(self):
        import datetime as dt
        from compkit import cull, write_results
        shoot = self.folder / "shoot"
        shoot.mkdir()
        start = dt.datetime(2026, 6, 27, 22, 0, 0)
        for i in range(3):  # a burst: the same scene, a hair apart, half a second apart
            shot(shoot / f"burst-{i}.jpg", seed=1, when=start + dt.timedelta(seconds=0.5 * i), shift=2 * i)
        shot(shoot / "later-a.jpg", seed=2, when=start + dt.timedelta(seconds=60))
        shot(shoot / "later-b.jpg", seed=3, when=start + dt.timedelta(seconds=120))
        shot(shoot / "blurred.jpg", seed=4, when=start + dt.timedelta(seconds=180), blur=8)
        frames = cull([shoot])
        status = {f["name"]: f for f in frames}
        self.assertEqual(len({f["moment"] for f in frames}), 4)
        self.assertEqual(len({status[f"burst-{i}.jpg"]["moment"] for i in range(3)}), 1)
        self.assertEqual(sum(status[f"burst-{i}.jpg"]["status"] == "pick" for i in range(3)), 1)
        self.assertEqual(status["blurred.jpg"]["status"], "reject")
        self.assertIn("soft", status["blurred.jpg"]["flags"])
        summary = write_results(frames, self.folder / "cull", sheets=True)
        self.assertEqual(summary["picks"], 3)
        self.assertEqual(len((self.folder / "cull" / "picks.txt").read_text().split()), 3)
        self.assertTrue(list((self.folder / "cull" / "sheets").glob("moments-*.jpg")))

    def test_lightroom_crop_follows_the_raws_orientation(self):
        from compkit import lightroom_crop
        crop = {"HasCrop": "True", "CropLeft": "0.1", "CropTop": "0.2", "CropRight": "0.7", "CropBottom": "0.9", "CropAngle": "0"}
        close = lambda a, b: all(abs(x - y) < 1e-9 for x, y in zip(a, b))
        self.assertTrue(close(lightroom_crop(crop, 1), (0.1, 0.2, 0.6, 0.7)))
        self.assertTrue(close(lightroom_crop(crop, 8), (0.2, 0.3, 0.7, 0.6)))
        self.assertTrue(close(lightroom_crop(crop, 6), (0.1, 0.1, 0.7, 0.6)))
        self.assertTrue(close(lightroom_crop(crop, 3), (0.3, 0.1, 0.6, 0.7)))
        self.assertIsNone(lightroom_crop({**crop, "HasCrop": "False"}, 1))

    def test_a_look_table_finishes_the_grade(self):
        invert = self.folder / "invert.cube"
        lines = ["LUT_3D_SIZE 2"] + [f"{1 - r} {1 - g} {1 - b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)]
        invert.write_text("\n".join(lines) + "\n")
        plain, inverted = self.folder / "plain.png", self.folder / "inverted.png"
        develop(self.gray(), self.preset(), plain)
        develop(self.gray(), self.preset(), inverted, match=invert)
        a = np.asarray(Image.open(plain).convert("RGB")).astype(int)
        b = np.asarray(Image.open(inverted).convert("RGB")).astype(int)
        self.assertLess(np.abs((255 - a) - b).mean(), 2)

    def test_regrade_goes_back_to_the_source_file(self):
        source = self.folder / "source.png"
        Image.new("RGB", (300, 200), (90, 90, 90)).save(source)
        p = Project.new(300, 200)
        photo = p.add_graded_photo(source, self.preset())
        saved = p.save(self.folder / "sourced.comp")
        record = json.loads((self.folder / "sourced.sources.json").read_text())["photos"][photo.id]
        self.assertEqual(Path(record["source"]), source.resolve())
        q = Project.open(saved)
        q.layer("Photo").regrade(amount=0.5)  # preset and source come from the record
        self.assertIn("at 50%", q.layer("Photo").graded_parts()["graded"].name)
        source.unlink()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            q.layer("Photo").regrade(amount=0.7)
        self.assertIn("missing", errors.getvalue())
        q.save()

    @unittest.skipUnless(os.environ.get("COMPKIT_TEST_RAW"), "set COMPKIT_TEST_RAW to a camera RAW file to test the RAW stage")
    def test_raw_stage(self):
        raw = Path(os.environ["COMPKIT_TEST_RAW"])
        report = develop(raw, self.preset(), self.folder / "raw.png", original=self.folder / "raw-original.png",
                         size=(400, 300), crop=(0.1, 0.1, 0.8, 0.6), settings={"raw.temperature": 4500})
        self.assertEqual(report["raw"]["temperature"], 4500)
        self.assertGreater(report["raw"]["exposure"], 0.9)  # the test preset's +1 EV, applied while decoding
        self.assertTrue(report["raw"]["lensCorrection"])
        self.assertEqual(Image.open(self.folder / "raw.png").size, (400, 300))
        p = Project.new(400, 300)
        photo = p.add_graded_photo(raw, self.preset())
        p.save(self.folder / "raw.comp")
        again = Project.open(self.folder / "raw.comp").layer("Photo").regrade(amount=0.5)
        self.assertIn("raw", again)  # decoded from the RAW again, not the 8-bit original

    def test_box_text_shrinks_until_it_fits(self):
        p = Project.new(1000, 400)
        layer = p.add_text("Short", x=40, y=40, size=90, box=(600, 140))
        layer.set_text("A much longer headline that needs three or four lines", shrink_to_fit=True)
        self.assertLess(layer.text["fontSize"], 90)
        self.assertFalse(json.loads(comp_render("text", self.style_file(layer.text), self.folder / "t.png"))["overflow"])
        p.save(self.folder / "shrink.comp")

    def test_point_text_keeps_a_matching_right_margin(self):
        p = Project.new(1000, 400)
        layer = p.add_text("Short", x=100, y=200, size=80)
        layer.set_text("A very long line of point text that would run off the canvas", shrink_to_fit=True, min_size=12)
        found = json.loads(comp_render("text", self.style_file(layer.text), self.folder / "t.png"))
        self.assertLessEqual(found["textWidth"], 1000 - 2 * 100 + 1)

    def style_file(self, style):
        path = self.folder / "style.json"
        path.write_text(json.dumps(style))
        return path

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
