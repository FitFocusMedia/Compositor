"""Telling athletes apart by hip number and balancing their solo picks, on frames and reads laid out by hand.

    automation/.venv/bin/python -m unittest discover -s automation/tests -v
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import CompError, athletes  # noqa: E402
from compkit import show as shows  # noqa: E402
from test_show import frame  # noqa: E402


def hip(number=None, *, x=0.4, height=0.6, solo=True):
    """A `comp-render hips` read: one person at `x` (left edge), `height` tall, with `number` read on the hip."""
    return {"people": [[x, 0.2, 0.2, height, 0.9]], "solo": solo,
            "text": [{"text": str(number), "confidence": 1.0}] if number else []}


class AthleteTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        sheet = self.folder / "runsheet.csv"
        sheet.write_text("Number,Name,Divisions\n71,Sam Lee,Men's Physique\n71,Sam Lee,Men's Classic\n"
                         "72,Ana Cruz,Ms Bikini\n172,Ana Cruz,Ms Fitness\n73,Jo Park,Ms Bikini\n")
        self.sheet = athletes.runsheet(sheet)

    def test_the_run_sheet(self):
        self.assertEqual(self.sheet[71], {"name": "Sam Lee", "divisions": ["Men's Physique", "Men's Classic"]})
        self.assertEqual(athletes._people(self.sheet)["ana cruz"], [72, 172])  # one athlete, a number per division
        clash = self.folder / "clash.json"
        clash.write_text('[{"number": 5, "name": "A"}, {"number": 5, "name": "B"}]')
        with self.assertRaises(CompError):
            athletes.runsheet(clash)

    def test_a_read_carries_along_a_burst_and_no_further(self):
        frames = [frame("A1", 0, 0.0), frame("A2", 0, 0.4), frame("A3", 0, 0.8), frame("A4", 0, 2.0),
                  frame("A5", 0, 2.7), frame("A6", 0, 3.1), frame("A7", 0, 3.5), frame("A8", 1, 100), frame("A9", 1, 102)]
        hips = {"/card/A1": hip(), "/card/A2": hip(71), "/card/A3": hip(),  # A1, A3: 0.4 s either side of a read
                "/card/A4": hip(),  # 1.2 s after A3 and 0.7 s before A5: too far from either
                "/card/A5": hip(72), "/card/A6": hip(x=0.6),  # the next athlete along: another place on the frame
                "/card/A7": hip(73),  # read once in five minutes: most likely a misread
                "/card/A8": hip(71, x=0.1), "/card/A9": hip(72, x=0.1)}
        athletes.identify(frames, hips, self.sheet)
        self.assertEqual([f["athlete"] for f in frames], [71, 71, 71, None, 72, None, None, 71, 72])
        self.assertEqual([f["athlete_from"] for f in frames], ["burst", "hip", "burst", None, "hip", None, None, "hip", "hip"])
        self.assertEqual(frames[6]["hip"], 73)  # read all the same: not a number that was never read
        group = [frame("B1", 1, 10.0), frame("B2", 1, 10.3), frame("B3", 1, 20.0), frame("B4", 1, 20.3)]
        athletes.identify(group, {"/card/B1": hip(99), "/card/B2": hip(99),  # not on the run sheet
                                  "/card/B3": hip(71, solo=False), "/card/B4": hip(71, solo=False)}, self.sheet)  # not alone
        self.assertEqual([f["athlete"] for f in group], [None] * 4)

    def test_short_athletes_get_their_best_alternates_one_per_moment(self):
        frames = [frame("P1", 0, 0, score=70), frame("P2", 0, 1, score=90), frame("P3", 0, 2, score=85),
                  frame("P4", 1, 60, score=60), frame("P5", 2, 120, score=50), frame("P6", 3, 180, score=40)]
        for f, status in zip(frames, ["pick", "alternate", "alternate", "alternate", "alternate", "alternate"]):
            f.update(status=status, solo=True, athlete=72 if f["name"] != "P6" else 172, athlete_from="hip")
        frames[4]["athlete"] = 73
        for f in frames:
            f["hip"] = f["athlete"]
        cull = self.folder / "cull"
        shows._write_json(cull / "decisions.json", {"auto": {f["path"]: f["status"] for f in frames}, "manual": {}})
        # Ana (72 and 172) has 1 pick, so 2 more: moments 1 and 3, which have no pick yet, before moment 0's best.
        shows.decide(cull, frames, rule={}, dial={}, sheet=self.sheet, min_solo=3)
        self.assertEqual({f["name"] for f in frames if f["promoted"]}, {"P4", "P6", "P5"})
        # A later run keeps them, and a frame set by hand is never promoted.
        again = [dict(f, status="alternate") for f in frames]
        shows._write_json(cull / "decisions.json", {**shows._read_json(cull / "decisions.json", {}),
                                                    "manual": {"/card/P2": "alternate"}})
        shows.decide(cull, again, rule={}, dial={}, sheet=self.sheet, min_solo=4)
        self.assertEqual({f["name"] for f in again if f["status"] == "pick"}, {"P1", "P3", "P4", "P5", "P6"})
        report = athletes.write_table(again, self.sheet, cull, 4)
        with open(cull / "athletes.csv") as file:
            rows = {row["name"]: row for row in csv.DictReader(file)}
        self.assertEqual((rows["Ana Cruz"]["number"], rows["Ana Cruz"]["solo picks"], rows["Ana Cruz"]["solo alternates"],
                          rows["Ana Cruz"]["promoted"]), ("72 172", "4", "1", "3"))
        self.assertEqual(report["under 4 solo picks"], ["73 Jo Park: 1"])
        self.assertEqual(report["never read (scratched?)"], [71])


if __name__ == "__main__":
    unittest.main()
