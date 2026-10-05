"""The show pipeline: adaptive picks, re-dialing, resumable culls, the setup sheet, resumable grading and export by a
delivery spec, comparing with what was delivered and learning profiles from it. Synthetic shoots stand in for a
card; set COMPKIT_TEST_RAW to a camera RAW to also test full-resolution export from it.

    automation/.venv/bin/python -m unittest discover -s automation/tests -v
"""

import copy
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compkit import CompError, comp_render  # noqa: E402
import importlib  # noqa: E402
culling = importlib.import_module("compkit.cull")  # the module (compkit.cull is also the name of its function)
from compkit import show as shows  # noqa: E402
from compkit.runlog import Progress  # noqa: E402
from test_compkit import PRESET, shot  # noqa: E402

START = dt.datetime(2026, 6, 27, 19, 0, 0)


def frame(name, moment, seconds, score=50.0, flags=()):
    when = START + dt.timedelta(seconds=seconds)
    return {"name": name, "path": f"/card/{name}", "moment": moment, "score": score, "flags": list(flags),
            "captured": when.strftime("%Y-%m-%d %H:%M:%S.") + f"{when.microsecond // 1000:03d}"}


def moments(*sizes, spacing=1.0):
    """Frames in moments of the given sizes, `spacing` seconds apart inside a moment and a minute between them."""
    frames, clock = [], 0.0
    for moment, size in enumerate(sizes):
        for i in range(size):
            frames.append(frame(f"F{len(frames):04d}.ARW", moment, clock + i * spacing, score=50 + (i % 3)))
        clock += 60
    return frames


def picks_per_moment(frames):
    counts = {}
    for f in frames:
        counts[f["moment"]] = counts.get(f["moment"], 0) + (f["status"] == "pick")
    return [counts[m] for m in sorted(counts)]


class PickingTests(unittest.TestCase):
    """The per-moment rule and the keep-rate dial, on frames laid out by hand."""

    def test_bigger_moments_get_more_picks(self):
        frames = moments(1, 1, 2, 4, 8)
        summary = culling.assign(frames, rate=0.5)
        self.assertEqual(summary["picks"], 8)
        counts = picks_per_moment(frames)
        self.assertEqual(counts, sorted(counts))  # never fewer for a bigger moment
        self.assertGreaterEqual(counts[-1], 3)
        self.assertTrue(all(c >= 1 for c in counts[2:]))  # every burst and held pose is covered

    def test_the_dial_sets_the_count_exactly(self):
        frames = moments(3, 1, 5, 2, 7, 1, 4)
        for count in (3, 7, 10, 23):
            self.assertEqual(culling.assign(frames, count=count)["picks"], count)
        self.assertEqual(culling.assign(frames, rate=culling.parse_rate("35%"))["picks"], round(0.35 * len(frames)))

    def test_cover_reaches_every_moment_before_any_gets_a_second(self):
        frames = moments(1, 1, 2, 4, 8)
        culling.assign(frames, count=5, rule={"tau": 0, "cover": 10, "beta": 0})
        self.assertEqual(picks_per_moment(frames), [1, 1, 1, 1, 1])

    def test_flagged_frames_are_never_picked(self):
        frames = moments(3, 2)
        for f in frames[:3]:
            f["flags"] = ["soft"]
        culling.assign(frames, count=4)
        self.assertEqual([f["status"] for f in frames[:3]], ["reject"] * 3)
        self.assertEqual(sum(f["status"] == "pick" for f in frames), 2)  # only two good frames exist

    def test_equal_priorities_go_to_the_better_score_not_the_earlier_frame(self):
        frames = [frame("A.ARW", 0, 0, score=40), frame("B.ARW", 1, 60, score=70)]
        culling.assign(frames, count=1, rule={"tau": 0, "cover": 1, "beta": 0})
        self.assertEqual([f["status"] for f in frames], ["alternate", "pick"])

    def test_fast_bursts_count_for_less_than_held_poses(self):
        burst = moments(10, spacing=0.1)
        pose = moments(10, spacing=1.0)
        self.assertAlmostEqual(culling.effective_size(burst, 0.5), 1 + 9 * 0.2)
        self.assertEqual(culling.effective_size(pose, 0.5), 10)
        self.assertEqual(culling.effective_size(burst, 0), 10)

    def test_long_runs_of_one_scene_split_into_moments(self):
        frames = moments(12, spacing=1.0)
        for f in frames:
            f["distance"] = 0.1
        self.assertEqual(culling.group(frames, gap=2, distance=0.6), 1)
        self.assertEqual(culling.group(frames, gap=2, distance=0.6, max_moment=4), 3)

    def test_fixed_picks_per_moment_still_work(self):
        frames = moments(1, 3, 5)
        culling.assign(frames, per_moment=2)
        self.assertEqual(picks_per_moment(frames), [1, 2, 2])
        culling.assign(frames, per_moment=2, keep=3)
        self.assertEqual(sum(picks_per_moment(frames)), 3)

    def test_keep_rates_read_as_people_write_them(self):
        self.assertEqual(culling.parse_rate("35%"), 0.35)
        self.assertEqual(culling.parse_rate("35"), 0.35)
        self.assertEqual(culling.parse_rate("0.35"), 0.35)
        with self.assertRaises(CompError):
            culling.parse_rate("135%")

    def test_metrics_and_fitting_against_delivered_frames(self):
        frames = moments(1, 1, 2, 4, 8, 6)
        kept = {f["name"] for f in frames if f["moment"] >= 2 and int(f["name"][1:5]) % 2 == 0}
        culling.assign(frames, count=len(kept))
        metrics = shows.pick_metrics(frames, kept)
        self.assertEqual(metrics["moments delivered from"], 4)
        self.assertEqual(metrics["picks"], len(kept))
        self.assertLessEqual(metrics["moments covered"], 4)
        rule, value = shows.fit_rule([{"frames": copy.deepcopy(frames), "kept": sorted(kept)}])
        self.assertEqual(set(rule), {"tau", "cover", "beta"})
        self.assertGreater(value, 0)
        # The current rule stays when nothing beats it clearly.
        same, _ = shows.fit_rule([{"frames": copy.deepcopy(frames), "kept": sorted(kept)}], current=rule)
        self.assertEqual(same, rule)

    def test_graded_and_delivered_names_never_collide(self):
        names = shows.graded_names(["/a/100MSDCF/DSC01234.ARW", "/b/100MSDCF/DSC01234.ARW", "/a/DSC01235.ARW"])
        self.assertEqual(names["/a/DSC01235.ARW"], "DSC01235")
        self.assertNotEqual(names["/a/100MSDCF/DSC01234.ARW"], names["/b/100MSDCF/DSC01234.ARW"])
        self.assertEqual(shows._clean('Spring: "Classic" 11/10/26'), "Spring- -Classic- 11-10-26")


class ShowTests(unittest.TestCase):
    """A small synthetic show, end to end, with the app's own renderer."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {"COMPKIT_PROFILES": str(self.folder / "profiles")})
        self.environment.start()
        self.preset = self.folder / "grade.xmp"
        self.preset.write_text(PRESET)
        self.card = self.folder / "card" / "DCIM"
        self.card.mkdir(parents=True)
        when = dt.datetime(2026, 6, 27, 19, 50, 0)
        # Two bursts, a held pose, two single frames, across two hours; file numbers like a camera's.
        layout = [(0, 1), (0.5, 1), (1.0, 1), (60, 2), (61, 2), (62, 2), (63, 2), (3600, 3), (3720, 4), (3721, 4)]
        for number, (seconds, seed) in enumerate(layout, start=7750):
            shot(self.card / f"DSC{number:05d}.jpg", seed=seed, when=when + dt.timedelta(seconds=seconds), shift=int(seconds) % 5)
        shows._write_json(self.folder / "profiles" / "profiles.json", {
            "preset": str(self.preset),
            "delivery": {"naming": "{show} - {date} - Test{number}",
                         "sizes": [{"name": "Full Res", "max_side": None, "max_kb": 40},
                                   {"name": "Web", "max_side": 300, "max_kb": 40}]}})

    def tearDown(self):
        self.environment.stop()
        shutil.rmtree(self.folder)

    def new_show(self, **extra):
        return shows.Show.create(self.folder / "show", cards=[self.card.parent], profile_name="workshop",
                                 name="Spring Classic", **extra)

    def test_profiles_merge_what_was_saved_over_the_built_in(self):
        shows.save_profile("fight-night", {"rate": 0.25, "rule": {"tau": 0.8}})
        merged = shows.profile("fight-night")
        self.assertEqual(merged["rate"], 0.25)
        self.assertEqual(merged["rule"], {"tau": 0.8, "cover": 1.0, "beta": 0.3})  # the rest of the rule kept
        self.assertEqual(merged["max_moment"], 4.0)
        with self.assertRaises(CompError):
            shows.profile("wedding")
        with self.assertRaises(CompError):  # the card is only ever read: no show inside it
            shows.Show.create(self.card / "show", cards=[self.card], profile_name="workshop", name="x")

    def test_a_cull_resumes_and_one_bad_file_doesnt_stop_it(self):
        (self.card / "DSC09998.jpg").write_bytes(os.urandom(5000))  # damaged
        (self.card / "DSC09999.ARW").write_bytes(b"")  # empty
        cache = self.folder / "cache"
        progress = Progress("cull", folder=self.folder / "logs", quiet=True)
        paths = culling.gather([self.card])
        poison = str((self.card / "DSC07752.jpg").resolve())
        real = culling._run_listed

        def flaky(command, listed, *extra):  # comp-render dies on one file, as on a corrupt RAW
            if command == "score" and any(str(p) == poison for p in listed):
                raise CompError("comp-render crashed")
            return real(command, listed, *extra)

        with mock.patch.object(culling, "_run_listed", flaky):
            entries = culling.scan(paths, cache, chunk=4, progress=progress)
        frames = {Path(e["path"]).name: e for e in entries}
        self.assertIn("comp-render failed on it", frames["DSC07752.jpg"]["score"]["error"])
        self.assertTrue(frames["DSC07753.jpg"]["print"])  # its batch-mates were scored
        self.assertTrue(frames["DSC09998.jpg"]["score"].get("error"))
        self.assertTrue(frames["DSC09999.ARW"]["score"].get("error"))
        self.assertIn("DSC07752.jpg", (self.folder / "logs" / "failures.csv").read_text())
        judged = culling.frames_from(entries)
        culling.group(judged)
        culling.judge(judged)
        self.assertTrue(all("unreadable" in f["flags"] for f in judged if f["name"] in ("DSC07752.jpg", "DSC09998.jpg", "DSC09999.ARW")))

        # Stopped partway: only the frames not yet in the cache are scored again.
        lines = (cache / "frames.jsonl").read_text().splitlines()
        (cache / "frames.jsonl").write_text("\n".join(lines[:5]) + "\n" + lines[5][:40])
        counted = []
        original = culling._score_batch
        with mock.patch.object(culling, "_score_batch", lambda batch, size: counted.extend(batch) or original(batch, size)):
            again = culling.scan(paths, cache, chunk=4)
        failed = sum(1 for line in lines[:5] if json.loads(line)["score"].get("error"))  # failures are retried
        self.assertEqual(len(counted), len(paths) - 5 + failed)
        self.assertEqual([e["path"] for e in again], [e["path"] for e in entries])

    def test_a_stop_is_not_a_bad_file(self):
        import signal
        import subprocess
        from compkit import Stopped, check_comp_render
        with self.assertRaises(Stopped):
            check_comp_render(subprocess.CompletedProcess([], -signal.SIGTERM, "", ""), "comp-render score")
        with self.assertRaises(CompError) as crash:  # a crash on the file it read is the file's failure
            check_comp_render(subprocess.CompletedProcess([], -signal.SIGSEGV, "", ""), "comp-render score")
        self.assertNotIsInstance(crash.exception, Stopped)
        paths = culling.gather([self.card])
        cache = self.folder / "cache"
        with mock.patch.object(culling, "_run_listed", side_effect=Stopped("stopped")):
            with self.assertRaises(Stopped):
                culling.scan(paths, cache, chunk=4)
        self.assertFalse((cache / "frames.jsonl").read_text().strip())  # nothing recorded as unreadable
        # A file that failed is tried again on the next run (it may have been a bad moment, not a bad file).
        real = culling._run_listed
        with mock.patch.object(culling, "_run_listed", lambda c, listed, *e: (_ for _ in ()).throw(CompError("x"))
                               if c == "score" and paths[0] in listed else real(c, listed, *e)):
            first = culling.scan(paths, cache, chunk=4)
        self.assertTrue(first[0]["score"].get("error"))
        again = culling.scan(paths, cache, chunk=4)
        self.assertFalse(again[0]["score"].get("error"))

    def test_feature_print_distances_match_visions_own(self):
        paths = sorted(culling.gather([self.card]))
        entries = culling.scan(paths)
        frames = culling.frames_from(entries)
        ordered = [Path(f["path"]) for f in frames]
        vision = json.loads(culling._run_listed("score", ordered))
        for f, v in list(zip(frames, vision))[1:]:
            self.assertAlmostEqual(f["distance"], v["distanceToPrevious"], places=4)

    def test_a_whole_show(self):
        show = self.new_show()
        summary = shows.cull_show(show)
        self.assertEqual(summary["frames"], 10)
        self.assertEqual(summary["picks"], 4)  # the workshop profile's 40%
        self.assertEqual(show.data["date"], "2026-06-27")

        # Re-dial from the scores, without reading a frame again.
        with mock.patch.object(culling, "scan", side_effect=AssertionError("re-scanned")):
            again = shows.pick_show(show, count=6)
        self.assertEqual(again["picks"], 6)
        self.assertEqual(len((show.cull_folder / "picks.txt").read_text().split("\n")) - 1, 6)
        self.assertEqual(json.loads((show.cull_folder / "settings.json").read_text())["count"], 6)
        self.assertTrue(list((show.cull_folder / "sheets").glob("moments-*.jpg")))
        self.assertEqual(shows.cull_show(show, sheets=False)["picks"], 6)  # culling again keeps the dial

        # The command line, as the runbook uses it.
        from compkit.__main__ import main
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["pick", str(show.cull_folder), "--count", "6", "--no-sheets"]), 0)
            self.assertEqual(main(["show", "status", str(show.folder)]), 0)
        self.assertIn("compkit show setup", out.getvalue())
        with mock.patch.object(shows, "status", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["show", "status", str(show.folder)]), 130)
        self.assertIn("run the same command again", errors.getvalue())

        # Setup: representative frames × candidate settings on one sheet, then approval.
        with self.assertRaises(CompError):
            shows.grade_show(show)  # nothing approved yet
        setup = shows.setup_show(show, frames=3, tries=[{"exposure": 0.2}], size=160, jobs=2)
        self.assertEqual(len(setup["frames"]), 3)
        self.assertEqual(list(setup["candidates"])[:2], ["A", "B"])
        self.assertIn("Try", [c["name"] for c in setup["candidates"].values()])
        self.assertTrue(Path(setup["sheet"]).exists())
        rows = len(setup["frames"])
        times = [f["captured"] for f in setup["frames"]]
        self.assertEqual(times, sorted(times))
        per_row = ",".join(f"{r}={'B' if r == 1 else 'E'}" for r in range(1, rows + 1))
        approved = shows.approve_setup(show, per_row, {"raw.tint": 25})
        self.assertEqual(len(approved["by_lighting"]), rows)
        first = next(f for f in show.frames() if f["name"] == setup["frames"][0]["name"])
        self.assertEqual(shows.settings_for(show, first), {**setup["candidates"]["B"]["settings"], "raw.tint": 25})
        approved = shows.approve_setup(show, "B")
        self.assertIsNone(approved["by_lighting"])

        # Grade: resumable.
        graded = shows.grade_show(show, jobs=3)
        self.assertEqual((graded["done now"], graded["failed"]), (6, 0))
        again = shows.grade_show(show, jobs=3)
        self.assertEqual((again["done now"], again["already done"]), (0, 6))

        # Export by the spec: <show>/<size>/<hour>/<name>.jpg, capped, with the camera's metadata.
        exported = shows.export_show(show, jobs=2)
        self.assertEqual(exported["failed"], 0)
        root = show.folder / "delivery" / "Spring Classic"
        files = sorted(root.rglob("*.jpg"))
        self.assertEqual(len(files), 12)
        self.assertEqual({f.parent.parent.name for f in files}, {"Full Res", "Web"})
        self.assertEqual({f.parent.name for f in files}, {"19.00", "20.00"})
        self.assertTrue(all(f.name.startswith("Spring Classic - 27-06-26 - Test077") for f in files))
        self.assertTrue(all(f.stat().st_size <= 40 * 1024 for f in files))
        with Image.open(next(f for f in files if f.parent.parent.name == "Web")) as web:
            self.assertEqual(max(web.size), 300)
        full = next(f for f in files if f.parent.parent.name == "Full Res")
        with Image.open(full) as image:
            self.assertEqual(max(image.size), 800)
        source = next(f for f in show.picks() if full.stem.endswith(Path(f["path"]).stem[3:]))
        self.assertEqual(json.loads(comp_render("probe", full))[0]["captured"], source["captured"])
        again = shows.export_show(show, jobs=2)
        self.assertEqual(again["done now"], 0)
        files[0].unlink()
        (root / "Web" / "stray.jpg").write_bytes(b"x")
        again = shows.export_show(show, jobs=2, prune=True)
        self.assertEqual((again["done now"], again["removed"]), (1, 1))
        self.assertTrue(files[0].exists())

        # Compare with what was "delivered" (three of the exports, matched by capture time) and learn from it.
        delivered = self.folder / "delivered"
        delivered.mkdir()
        for path in files[:3]:
            shutil.copy(path, delivered / f"final-{path.name}")
        report = shows.compare_show(show, delivered, learn=True)
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["grades"]["compared"], 3)
        self.assertLess(report["grades"]["delta_e mean"], 3)  # its own exports: nearly identical
        learned = shows.profile("workshop")
        self.assertEqual(learned["rate"], round(3 / 10, 3))
        self.assertEqual(learned["shows learned from"], 1)
        self.assertTrue((show.folder / "report" / "compare.md").exists())
        self.assertTrue(list((self.folder / "profiles" / "history" / "workshop").glob("*.json")))
        self.assertEqual(shows.status(show)["next"].split()[2], "compare")

    def test_a_picture_that_fails_to_grade_is_logged_and_the_rest_carry_on(self):
        good = sorted(self.card.glob("*.jpg"))[:2]
        bad = self.card / "DSC08000.jpg"
        bad.write_bytes(os.urandom(3000))
        progress = Progress("grade", folder=self.folder / "logs", quiet=True)
        summary = shows.grade_batch(good + [bad], self.preset, self.folder / "graded", jobs=2, progress=progress)
        self.assertEqual((summary["done now"], summary["failed"]), (2, 1))
        self.assertIn("DSC08000.jpg", (self.folder / "logs" / "failures.csv").read_text())
        # New settings grade again; a missing JPEG is only rendered again.
        (self.folder / "graded" / f"{good[0].stem}.jpg").unlink()
        states = [shows._graded_state(self.folder / "graded", p.stem, {"source": str(p.resolve()), "preset": str(self.preset.resolve()),
                                                                     "match": None, "amount": 1.0, "settings": {}}, True) for p in good]
        self.assertEqual(states, ["render", "done"])
        changed = shows.grade_batch(good, self.preset, self.folder / "graded", settings_for=lambda _: {"exposure": 0.3},
                                    jobs=2, progress=Progress("grade", quiet=True))
        self.assertEqual(changed["done now"], 2)

    def test_delivered_names_follow_the_shows_own_naming(self):
        show = self.new_show(naming="{show}_{seq}", date="2026-07-01")
        shows.cull_show(show, sheets=False)
        plan = shows.delivery_plan(show, show.picks())
        names = [o["path"].name for e in plan for o in e["outputs"] if o["size"]["name"] == "Web"]
        self.assertEqual(names, [f"Spring Classic_{n:04d}.jpg" for n in range(1, len(names) + 1)])
        first = show.picks()[0]
        twin = {**first, "path": str(self.folder / "second-camera" / Path(first["path"]).name)}  # same number, same hour
        show.data["delivery"]["naming"] = "{number}"
        plan = shows.delivery_plan(show, [first, twin], sizes=["Web"])
        self.assertEqual([e["outputs"][0]["path"].name for e in plan], ["07750.jpg", "07750-2.jpg"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_parallel_grading_matches_the_apps_own(self):
        photo = shot(self.folder / "p.jpg", seed=5, when=START)
        preset = self.folder / "grade.xmp"
        preset.write_text(PRESET)
        comp_render("develop", photo, preset, self.folder / "fast.png", "--set", "clarity=30", "--set", "curve.shadows=10")
        comp_render("develop", photo, preset, self.folder / "slow.png", "--set", "clarity=30", "--set", "curve.shadows=10", "--serial")
        self.assertTrue((np.asarray(Image.open(self.folder / "fast.png")) == np.asarray(Image.open(self.folder / "slow.png"))).all())

    def test_render_caps_the_file_size_and_keeps_the_camera_metadata(self):
        from compkit import Project
        photo = shot(self.folder / "p.jpg", seed=6, when=START)
        p = Project.new(800, 600)
        p.add_image(photo, name="Photo")
        p.save(self.folder / "p.comp")
        comp_render("render", self.folder / "p.comp", self.folder / "big.jpg", "--quality", "1")
        comp_render("render", self.folder / "p.comp", self.folder / "small.jpg", "--quality", "1", "--max-bytes", "30000",
                    "--metadata-from", photo)
        self.assertGreater((self.folder / "big.jpg").stat().st_size, 30000)
        self.assertLessEqual((self.folder / "small.jpg").stat().st_size, 30000)
        self.assertEqual(json.loads(comp_render("probe", self.folder / "small.jpg"))[0]["captured"], "2026-06-27 19:00:00.000")

    @unittest.skipUnless(os.environ.get("COMPKIT_TEST_RAW"), "set COMPKIT_TEST_RAW to a camera RAW file to test the RAW stage")
    def test_full_resolution_export_from_a_raw(self):
        raw = Path(os.environ["COMPKIT_TEST_RAW"])
        preset = self.folder / "grade.xmp"
        preset.write_text(PRESET)
        # raw.temperature=0 keeps the camera's white balance instead of the preset's.
        report = json.loads(comp_render("develop", raw, preset, self.folder / "s.png", "--size", "300x200",
                                        "--set", "raw.temperature=0"))
        self.assertIsNone(report["raw"].get("temperature"))
        info = json.loads(comp_render("probe", raw))[0]
        summary = shows.grade_batch([raw], preset, self.folder / "graded", max_side=600, render=False, jobs=1,
                                    progress=Progress("grade", quiet=True))
        self.assertEqual(summary["failed"], 0)
        project = self.folder / "graded" / f"{raw.stem}.comp"
        entry = {"project": project, "frame": {"path": str(raw)}}
        job = shows._export_job(entry, [{"path": self.folder / "full.jpg", "size": {"name": "Full", "max_side": None, "max_kb": 6000}},
                                        {"path": self.folder / "web.jpg", "size": {"name": "Web", "max_side": 2048, "max_kb": 2000}}], 0.92)
        self.assertIsNone(job["develop"]["crop"])  # the project's near-whole crop delivers the whole frame
        (self.folder / "job.json").write_text(json.dumps(job))
        written = json.loads(comp_render("export", self.folder / "job.json"))
        self.assertEqual((written[0]["width"], written[0]["height"]), (info["width"], info["height"]))
        self.assertLessEqual(written[0]["bytes"], 6000 * 1024)
        self.assertTrue(written[0]["fits"])
        self.assertEqual(max(written[1]["width"], written[1]["height"]), 2048)
        self.assertEqual(json.loads(comp_render("probe", self.folder / "full.jpg"))[0]["captured"], info["captured"])


if __name__ == "__main__":
    unittest.main()
