"""A hands-off show day: cards copied and verified as they arrive, decisions that later cards never change, statuses
and crops set by hand, leveling, the runner, the dashboard and the card watcher's launchd agents. Folders stand in
for SD cards and the show drive.

    automation/.venv/bin/python -m unittest discover -s automation/tests -v
"""

import datetime as dt
import json
import os
import plistlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import CompError, comp_render  # noqa: E402
from compkit import show as shows  # noqa: E402
from compkit import showday  # noqa: E402
from compkit.framing import inscribed_scale, suggest  # noqa: E402
from test_compkit import PRESET, shot  # noqa: E402
from test_show import moments  # noqa: E402


def horizon(path: Path, tilt: float) -> Path:
    """A landscape with a hard horizon, turned `tilt` degrees counterclockwise."""
    image = Image.new("RGB", (1500, 1000), (120, 170, 230))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 520, 1500, 1000], fill=(60, 110, 50))
    for x in range(0, 1500, 120):
        draw.rectangle([x, 470, x + 60, 520], fill=(90, 90, 100))
    image.filter(ImageFilter.GaussianBlur(1)).rotate(tilt, resample=Image.BICUBIC, fillcolor=(120, 170, 230)).save(path, quality=92)
    return path


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_a_later_card_never_changes_earlier_decisions(self):
        first = moments(1, 3, 5, 2)
        shows.decide(self.folder, first, rule=shows.DEFAULT_RULE, dial={"rate": 0.4})
        before = {f["path"]: f["status"] for f in first}
        later = moments(1, 3, 5, 2, 4, 6)  # the same frames again plus two new moments
        for f in later[len(first):]:
            f["path"] = f["path"].replace("/card/", "/card2/")
            f["score"] = 99  # better than anything on the first card
        shows.decide(self.folder, later, rule=shows.DEFAULT_RULE, dial={"rate": 0.4})
        self.assertEqual({f["path"]: f["status"] for f in later[:len(first)]}, before)
        new = later[len(first):]
        self.assertEqual(sum(f["status"] == "pick" for f in new), round(0.4 * len(new)))
        # A new rate decides everything again.
        shows.decide(self.folder, later, rule=shows.DEFAULT_RULE, dial={"rate": 0.8}, again=True)
        self.assertEqual(sum(f["status"] == "pick" for f in later), round(0.8 * len(later)))

    def test_statuses_set_by_hand_win_and_stay(self):
        from compkit.cull import write_results
        frames = moments(2, 3)
        for f in frames:
            f.update({k: None for k in ("sharpness", "face_quality", "eyes", "aesthetics", "lightness", "blown", "iso", "ev", "camera")})
            f["faces"] = 0
        shows.decide(self.folder, frames, rule=shows.DEFAULT_RULE, dial={"count": 2})
        write_results(frames, self.folder, sheets=False, settings={"rule": shows.DEFAULT_RULE, "count": 2})
        alternate = next(f for f in frames if f["status"] == "alternate")
        summary = shows.set_status(self.folder, {alternate["path"]: "pick"})
        self.assertEqual(summary["picks"], 3)
        self.assertIn(alternate["path"], (self.folder / "picks.txt").read_text())
        shows.decide(self.folder, frames, rule=shows.DEFAULT_RULE, dial={"rate": 0.2}, again=True)
        self.assertEqual(alternate["status"], "pick")  # a re-dial keeps what was set by hand
        shows.set_status(self.folder, {alternate["path"]: None})
        with self.assertRaises(CompError):
            shows.set_status(self.folder, {"/elsewhere/x.ARW": "pick"})


class FramingTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_leveling_turns_the_right_way_and_trims_to_fill(self):
        tilted = horizon(self.folder / "tilted.jpg", 4)
        level = json.loads(comp_render("subject", tilted))["level"]
        self.assertAlmostEqual(level, -4, delta=0.5)  # the turn that levels it: clockwise
        preset = self.folder / "plain.xmp"
        preset.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                          '<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
                          'crs:Exposure2012="0"/></rdf:RDF></x:xmpmeta>')
        comp_render("develop", tilted, preset, self.folder / "fixed.png", "--rotate", str(level))
        after = json.loads(comp_render("subject", self.folder / "fixed.png")).get("level")
        self.assertTrue(after is None or abs(after) < 0.5)
        with Image.open(self.folder / "fixed.png") as image:
            keep = inscribed_scale(1500, 1000, level)
            self.assertAlmostEqual(image.width, 1500 * keep, delta=2)
            self.assertAlmostEqual(image.width / image.height, 1.5, delta=0.01)  # same shape, no empty corners

    def test_suggestions_crop_in_on_people_and_leave_full_frames(self):
        small = {"width": 6000, "height": 4000, "level": 3.0,
                 "people": [{"x": 0.55, "y": 0.35, "width": 0.12, "height": 0.4, "confidence": 0.9}]}
        found = suggest(small)
        self.assertEqual(found["rotate"], 3.0)
        x, y, w, h = found["crop"]
        self.assertEqual(w, h)  # the photo's own shape
        self.assertGreaterEqual(w, 0.7)  # never tighter than 70%
        self.assertLessEqual(x, 0.55)
        self.assertGreaterEqual(x + w, 0.67)
        group = {"width": 6000, "height": 4000, "people": [{"x": 0.02, "y": 0.1, "width": 0.96, "height": 0.88, "confidence": 0.9}]}
        self.assertIsNone(suggest(group)["crop"])
        self.assertEqual(suggest({**group, "level": 15})["rotate"], 0)  # a steep angle is meant, or misread
        self.assertEqual(suggest({**group, "level": 0.4})["rotate"], 0)


class ShowDayTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {"COMPKIT_PROFILES": str(self.folder / "profiles"), "COMPKIT_QUIET": "1"})
        self.environment.start()
        preset = self.folder / "grade.xmp"
        preset.write_text(PRESET)
        shows._write_json(self.folder / "profiles" / "profiles.json", {
            "preset": str(preset), "delivery": {"naming": "{show} {number}", "sizes": [
                {"name": "Full Res", "max_side": None, "max_kb": 60}, {"name": "Web", "max_side": 300, "max_kb": 60}]}})
        self.drive = self.folder / "drive"
        self.drive.mkdir()
        when = dt.datetime(2026, 6, 27, 19, 0, 0)
        self.cards = []
        for card, layout in enumerate([[(0, 1), (0.5, 1), (60, 2), (120, 3)], [(600, 4), (600.5, 4), (660, 5), (720, 6)]]):
            folder = self.folder / f"CARD_{card + 1}"
            (folder / "DCIM" / "100MSDCF").mkdir(parents=True)
            for i, (seconds, seed) in enumerate(layout):
                shot(folder / "DCIM" / "100MSDCF" / f"DSC0{card + 1}{i:03d}.jpg", seed=seed, when=when + dt.timedelta(seconds=seconds),
                     shift=int(seconds * 2) % 7)
            self.cards.append(folder)

    def tearDown(self):
        self.environment.stop()
        shutil.rmtree(self.folder)

    def test_a_hands_off_show_day(self):
        show = showday.start_day(self.drive, "Spring Classic", "workshop", date="2026-06-27")
        self.assertEqual(showday.active().folder, show.folder)
        self.assertEqual(show.folder.parent, self.drive.resolve() / "Shows")

        copied = showday.ingest(show, [self.cards[0]], notify_user=False)
        self.assertEqual((copied["copied"], copied["failed"]), (4, 0))
        self.assertFalse(list(show.folder.rglob(".*.part")))
        ledger = json.loads((show.folder / "ingest.json").read_text())
        self.assertTrue(all(len(entry["sha1"]) == 40 for entry in ledger["files"].values()))
        result = showday.run(show, notify_user=False)
        show = shows.Show(show.folder)
        self.assertEqual(show.data["settings"]["approved"], "B")  # hands-off: the show base
        self.assertEqual(result["export"]["failed"], 0)
        first = json.loads((show.cull_folder / "decisions.json").read_text())["auto"]
        delivered = sorted((show.folder / "delivery").rglob("*.jpg"))
        self.assertEqual(len(delivered), 2 * sum(s == "pick" for s in first.values()))

        # The same card again adds nothing; the second card adds its photos and leaves the first card's decisions be.
        again = showday.ingest(show, [self.cards[0], self.cards[1]], notify_user=False)
        self.assertEqual((again["copied"], again["already"]), (4, 4))
        showday.run(show, notify_user=False)
        decisions = json.loads((show.cull_folder / "decisions.json").read_text())["auto"]
        self.assertEqual({p: decisions[p] for p in first}, first)
        self.assertEqual(len(decisions), 8)

        # A pick taken away by hand leaves the delivery; a crop and leveling set by hand are graded and delivered.
        picks = [f for f in show.frames() if f["status"] == "pick"]
        shows.set_status(show.cull_folder, {picks[0]["path"]: "reject"})
        shows.set_framing(show, picks[1]["path"], crop=(0.1, 0.1, 0.8, 0.8), rotate=2.0)
        showday.run(show, notify_user=False)
        stem = Path(picks[0]["path"]).stem
        self.assertFalse([p for p in (show.folder / "delivery").rglob("*.jpg") if p.stem.endswith(stem[3:])])
        names = shows.graded_names([f["path"] for f in show.frames()])
        project = json.loads((show.graded_folder / f"{names[picks[1]['path']]}.sources.json").read_text())["photos"]
        entry = next(iter(project.values()))
        self.assertEqual((entry["crop"], entry["rotate"], entry["framed"]), ([0.1, 0.1, 0.8, 0.8], 2.0, True))
        full = next(p for p in (show.folder / "delivery").rglob("*.jpg")
                    if p.parent.parent.name == "Full Res" and p.stem.endswith(Path(picks[1]["path"]).stem[3:]))
        with Image.open(full) as image:
            self.assertAlmostEqual(image.width, 800 * inscribed_scale(800, 600, 2.0) * 0.8, delta=3)
        showday.finish_day()
        self.assertIsNone(showday.active())

    def test_a_card_with_no_show_today_is_left_alone(self):
        with mock.patch.object(showday, "card_volumes", return_value=[self.cards[0]]):
            self.assertEqual(showday.on_mount(), {"cards": 1, "show": None})
        self.assertFalse(list(self.drive.rglob("*.jpg")))

    def test_the_dashboard_serves_the_show_and_takes_changes(self):
        from compkit.dashboard import serve
        show = showday.start_day(self.drive, "Spring Classic", "workshop", date="2026-06-27")
        showday.ingest(show, [self.cards[0]], notify_user=False)
        shows.cull_show(show, sheets=False)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        threading.Thread(target=serve, args=(port, None, False), daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/state")
                break
            except OSError:
                time.sleep(0.1)

        def get(path):
            with urllib.request.urlopen(base + path) as response:
                return response.read()

        def post(path, body):
            request = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read())

        self.assertIn(b"Show Dashboard", get("/"))
        state = json.loads(get("/api/state"))
        self.assertEqual(state["show"]["name"], "Spring Classic")
        frames = json.loads(get("/api/frames"))["frames"]
        self.assertEqual(len(frames), 4)
        thumb = get("/img/thumb?path=" + urllib.request.quote(frames[0]["path"]))
        self.assertEqual(thumb[:2], b"\xff\xd8")  # a JPEG
        with self.assertRaises(urllib.error.HTTPError):  # only this show's frames are served
            get("/img/loupe?path=" + urllib.request.quote(str(self.folder / "grade.xmp")))
        alternate = next(f for f in frames if f["status"] != "pick")
        with mock.patch.object(showday, "start_runner", return_value="started") as runner:
            changed = post("/api/status", {"changes": {alternate["path"]: "pick"}, "run": False})
            self.assertFalse(runner.called)
            post("/api/framing", {"path": alternate["path"], "crop": [0, 0, 0.9, 0.9], "rotate": 1})
            self.assertTrue(runner.called)
        self.assertEqual(changed["picks"], sum(f["status"] == "pick" for f in frames) + 1)
        updated = {f["path"]: f for f in json.loads(get("/api/frames"))["frames"]}[alternate["path"]]
        self.assertEqual((updated["status"], updated["manual"], updated["framing"]["rotate"]), ("pick", "pick", 1.0))

    def test_profile_settings_from_the_command_line(self):
        import contextlib
        import io
        from compkit.__main__ import main
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["show", "profiles", "--set", "fight-night", "auto_crop=true"]), 0)
            self.assertEqual(main(["show", "profiles", "--set", "fight-night", "rate=0.25"]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["show", "profiles", "--set", "fight-night", "preset=x"]), 1)
        merged = shows.profile("fight-night")
        self.assertEqual((merged["auto_crop"], merged["rate"]), (True, 0.25))

    def test_the_watchers_launchd_agents(self):
        agents = showday._agents()
        cards, board = agents[f"{showday.LABEL}.cards"], agents[f"{showday.LABEL}.dashboard"]
        self.assertTrue(cards["StartOnMount"])
        self.assertEqual(cards["ProgramArguments"][-2:], ["show", "on-mount"])
        self.assertTrue(board["KeepAlive"])
        self.assertIn("--no-open", board["ProgramArguments"])
        for agent in agents.values():
            plistlib.loads(plistlib.dumps(agent))  # valid property lists


if __name__ == "__main__":
    unittest.main()
