"""The Lightroom Classic sidecar routine: the develop-table parser, sidecar edits that leave nested blocks alone, the
write-and-verify step, crops fitted inside a rotation, the blank-corner scan and the delivery names.

    compkit-python -m pytest automation/tests/test_lightroom.py -q
"""

import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import CompError  # noqa: E402
from compkit import lightroom as lr  # noqa: E402
from compkit.framing import inscribed_scale  # noqa: E402

SETTINGS = 's = { Exposure2012 = 0.25, Look = { Name = "Adobe Color", Parameters = { Temperature = 5600 } }, ' \
           'Preset = { Exposure2012 = 1, PerspectiveUpright = 1, Name = "God, {Tones}", Note = "a \\" quote" }, ' \
           'Temperature = 3841, PerspectiveUpright = 0, }'

SIDECAR = '''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmp:Rating="3"
   crs:Exposure2012="+0.25"
   crs:Temperature="3841"
   crs:PerspectiveUpright="1"
   crs:LookName="a > b"
   crs:HasSettings="True">
   <crs:Look>
    <rdf:Description crs:Name="Adobe Color" crs:Temperature="5600" crs:PerspectiveUpright="1"/>
   </crs:Look>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
'''
CATALOGUE = {"Exposure2012": "0.25", "Temperature": "3841", "PerspectiveUpright": "1"}


class Catalogue(unittest.TestCase):
    def test_top_level_only(self):
        a = lr.top(SETTINGS)
        self.assertEqual(set(a), {"Exposure2012", "Look", "Preset", "Temperature", "PerspectiveUpright"})
        self.assertEqual((a["Exposure2012"], a["Temperature"], a["PerspectiveUpright"]), ("0.25", "3841", "0"))
        self.assertTrue(a["Preset"].startswith("{") and a["Preset"].endswith('"a \\" quote" }'))

    def test_copy_keeps_the_latest_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp, "Show.lrcat")
            writer = sqlite3.connect(live)
            writer.execute("pragma journal_mode=wal")
            writer.execute("create table t (x)")
            writer.execute("insert into t values (1)")
            writer.commit()  # still only in Show.lrcat-wal while the writer is open, as while Lightroom runs
            self.assertTrue(Path(f"{live}-wal").stat().st_size)
            copy = lr.copy_catalogue(live, Path(tmp, "copy"))
            self.assertEqual(copy.execute("select x from t").fetchall(), [(1,)])
            with self.assertRaises(sqlite3.OperationalError):
                copy.execute("insert into t values (2)")
            with self.assertRaises(CompError):
                lr.copy_catalogue(live, tmp)
            writer.close()


class Sidecars(unittest.TestCase):
    def test_set_attrs_leaves_nested_blocks_alone(self):
        out = lr.set_attrs(SIDECAR, {"PerspectiveUpright": 0, "CropTop": 0.1, "xmp:Label": "Red"})
        self.assertEqual(lr.attrs(out), {"Exposure2012": "+0.25", "Temperature": "3841", "PerspectiveUpright": "0",
                                         "LookName": "a > b", "HasSettings": "True", "CropTop": "0.1"})
        self.assertEqual(lr.attrs(out, "xmp"), {"Rating": "3", "Label": "Red"})
        self.assertIn('crs:Temperature="5600" crs:PerspectiveUpright="1"/>', out)  # the Look block as it was
        self.assertEqual(out[out.index("<crs:Look>"):], SIDECAR[SIDECAR.index("<crs:Look>"):])
        for bad in ({"Label": 'a"b'}, {"dc:Title": "x"}):
            with self.assertRaises(ValueError):
                lr.set_attrs(SIDECAR, bad)
        rating_only = '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="3"/>'
        with self.assertRaises(ValueError):  # no crs settings yet: Lightroom hasn't written this one
            lr.set_attrs(rating_only, {"xmp:Label": "Red"})

    def test_mismatches(self):
        self.assertEqual(lr.mismatches(SIDECAR, CATALOGUE), [])
        self.assertEqual(lr.mismatches(SIDECAR, {**CATALOGUE, "Temperature": "4200"}), ["Temperature"])
        self.assertEqual(lr.mismatches(SIDECAR, {**CATALOGUE, "CropTop": "0.1"}), ["CropTop"])  # crop not in file

    def test_apply_backs_up_writes_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, stale = Path(tmp, "IMG_0001.xmp"), Path(tmp, "IMG_0002.xmp")
            good.write_text(SIDECAR)
            stale.write_text(SIDECAR.replace('crs:Temperature="3841"', 'crs:Temperature="4425"'))
            plan = [{"xmp": x, "settings": CATALOGUE, "set": {"Temperature": 4200, "xmp:Label": "Red"}}
                    for x in (good, stale)]
            backup = Path(tmp, "backup.zip")
            dry = lr.apply(plan, backup)
            self.assertEqual((dry["ready"], len(dry["skipped"])), (1, 1))
            self.assertFalse(backup.exists())
            self.assertEqual(good.read_text(), SIDECAR)
            done = lr.apply(plan, backup, write=True)
            self.assertEqual((done["written"], done["verified"], done["failed"]), (1, 1, []))
            self.assertEqual(lr.attrs(good.read_text())["Temperature"], "4200")
            self.assertEqual(lr.attrs(stale.read_text())["Temperature"], "4425")  # left as it was
            self.assertEqual(zipfile.ZipFile(backup).read(str(good).lstrip("/")).decode(), SIDECAR)
            again = [{**plan[0], "settings": {**CATALOGUE, "Temperature": "4200"}}]  # after the person's read
            with self.assertRaises(FileExistsError):  # an earlier backup is never overwritten
                lr.apply(again, backup, write=True)
            # The stale one comes back from a backup copy that matches the catalogue.
            rebuilt = lr.apply(plan[1:], Path(tmp, "second.zip"), write=True, rebuild_from=_zip_of(Path(tmp, "old.zip"), stale, SIDECAR))
            self.assertEqual((rebuilt["rebuilt from the backup"], rebuilt["verified"]), (1, 1))
            self.assertEqual(lr.attrs(stale.read_text())["Exposure2012"], "+0.25")
            with self.assertRaises(CompError):
                lr.apply(plan + plan[:1], backup)


def _zip_of(path, sidecar, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(str(sidecar).lstrip("/"), text)
    return path


class Framing(unittest.TestCase):
    def test_inside_and_fit(self):
        self.assertTrue(lr.inside([.1, .1, .9, .9], 1, 683, 1024))
        self.assertFalse(lr.inside(lr.FULL, 1, 683, 1024))
        for degrees in (-3.5, 0.4, 2, 6):
            c = lr.fit(lr.FULL, degrees, 683, 1024)
            self.assertTrue(lr.inside(c, degrees, 683, 1024))
            self.assertAlmostEqual(c[2] - c[0], inscribed_scale(683, 1024, degrees), places=4)
        off_centre = lr.fit([0, .3, .7, 1], 2, 683, 1024)
        self.assertTrue(lr.inside(off_centre, 2, 683, 1024))
        self.assertAlmostEqual((off_centre[0] + off_centre[2]) / 2, .35)  # shrunk about its own centre

    def test_rule_crop_and_settings(self):
        athlete = {"x": .4, "y": .2, "width": .2, "height": .5, "confidence": .9}
        crop = lr.rule_crop([athlete, {"x": 0, "y": .5, "width": .1, "height": .2, "confidence": .9}])
        np.testing.assert_allclose(crop, [.19, .115, .81, .735])  # 1.24 athlete heights, square in fractions
        self.assertEqual(lr.rule_crop([{**athlete, "height": .9}]), lr.FULL)
        np.testing.assert_allclose(lr.to_stored([.1, .2, .7, .9], 8), [.1, .1, .8, .7])
        s = lr.crop_settings([.1, .2, .7, .9], lr.level(1.53), 8)
        self.assertEqual((s["PerspectiveRotate"], s["PerspectiveUpright"], s["HasCrop"]), ("+1.0", 0, "True"))
        self.assertEqual(lr.level(20), 0.0)
        self.assertEqual(lr.camera_tilt(-88.5, 8), 1.5)

    def test_stage_temperature(self):
        b = np.array([-45, -50, -60, -70, -48, -55])
        day = ["Day 1", "Day 1", "Day 1", "Day 2", "Day 2", "Day 2"]
        fit = lr.fit_temperature(b, -40 * b + 3700 + np.array([0, 0, 0, -400, -400, -400]), day)
        self.assertAlmostEqual(fit["b"], -40)
        self.assertAlmostEqual(fit["offsets"]["Day 2"], -400)
        self.assertEqual(lr.stage_temperature(-50, fit), 5700)
        self.assertEqual(lr.stage_temperature(-90, fit), 6800)
        self.assertIsNone(lr.stage_temperature(-20, fit))


class Corners(unittest.TestCase):
    def test_wedge_not_band(self):
        wedge = np.zeros((100, 100), bool)
        for y in range(40):
            wedge[y, :40 - y] = True
        self.assertEqual(lr._corner(wedge), 40)
        band = np.zeros((100, 100), bool)
        band[:10, :60] = True  # a bright screen along the top edge
        self.assertEqual(lr._corner(band), 0)
        self.assertEqual(lr._corner(np.zeros((100, 100), bool)), 0)
        self.assertEqual(lr._corner(wedge[::-1, ::-1][::-1, ::-1]), 40)


class Delivery(unittest.TestCase):
    def test_match_and_names(self):
        frames = [{"path": "/raw/Day 1/Ms Figure /IMG_1234.CR3",
                   "probe": {"camera": "Canon Canon EOS R5", "captured": "2026-10-17 08:45:31.450"}},
                  {"path": "/raw/Day 2/Ms Figure /IMG_1234.CR3",
                   "probe": {"camera": "Canon Canon EOS R5", "captured": "2026-10-18 09:00:00.120"}},
                  {"path": "/raw/Day 2/Sponsors : Misc/3N6A0001.CR3",
                   "probe": {"camera": "Canon Canon EOS R5 C", "captured": "2026-10-18 09:00:00.120"}}]
        exports = [{"SourceFile": "/export/IMG_1234.jpg", "Model": "Canon EOS R5", "DateTimeOriginal": "2026:10:17 08:45:31",
                    "SubSecTimeOriginal": "45", "RawFileName": "IMG_1234.CR3"},
                   {"SourceFile": "/export/IMG_1234-2.jpg", "Model": "Canon EOS R5", "DateTimeOriginal": "2026:10:18 09:00:00",
                    "SubSecTimeOriginal": "12", "RawFileName": "IMG_1234.CR3"},
                   {"SourceFile": "/export/3N6A0001.jpg", "Model": "Canon EOS R5 C", "DateTimeOriginal": "2026:10:18 09:00:00",
                    "SubSecTimeOriginal": "12", "RawFileName": "3N6A0001.CR3"},
                   {"SourceFile": "/export/lost.jpg", "Model": "Canon EOS R5", "DateTimeOriginal": "2026:10:18 10:00:00",
                    "SubSecTimeOriginal": "", "RawFileName": ""}]
        found, counts = lr.match_exports(exports, frames)
        self.assertEqual(found, {"/export/IMG_1234.jpg": frames[0]["path"], "/export/IMG_1234-2.jpg": frames[1]["path"],
                                 "/export/3N6A0001.jpg": frames[2]["path"]})
        self.assertEqual(counts, {"matched": 3, "0 candidates": 1})
        plan = lr.delivery_plan(found, exports, "/gallery", "ICN Nationals")
        self.assertEqual([p[1] for p in plan], [
            "/gallery/Ms Figure/ICN Nationals - Ms Figure - 17-10-26 - Fit Focus Media1234.jpg",
            "/gallery/Ms Figure/ICN Nationals - Ms Figure - 18-10-26 - Fit Focus Media1234.jpg",
            "/gallery/Sponsors - Misc/ICN Nationals - Sponsors - Misc - 18-10-26 - Fit Focus Media0001.jpg"])
        same_day = [{**r, "DateTimeOriginal": "2026:10:17 08:45:31"} for r in exports[:2]]  # two cameras, one number
        twice = lr.delivery_plan({r["SourceFile"]: found[r["SourceFile"]] for r in same_day}, same_day, "/g", "S")
        self.assertEqual([Path(p[1]).name for p in twice], ["S - Ms Figure - 17-10-26 - Fit Focus Media1234.jpg",
                                                            "S - Ms Figure - 17-10-26 - Fit Focus Media1234 (2).jpg"])


if __name__ == "__main__":
    unittest.main()
