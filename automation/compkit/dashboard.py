"""The show dashboard: a page served from this Mac only (127.0.0.1) to watch a show run, go through every frame by
moment, change picks, crops and the setup approval, and see the comparison report. Nothing leaves the Mac.

    compkit dashboard [--port 8765] [--show SHOW] [--no-open]

It shows today's show (showday.active) unless given one. Every change is saved in the show folder at once
(cull/decisions.json, crops.json, show.json) and the show's runner picks it up: a pick added is graded and
delivered, one taken away is removed from the delivery, a new crop or setting is graded and delivered again.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import CompError
from . import show as shows
from . import showday
from .cull import _run_listed, parse_rate

PAGE = Path(__file__).with_name("dashboard.html")
_preview_lock = threading.Lock()


def _show(fixed) -> shows.Show | None:
    return shows.Show(fixed) if fixed else showday.active()


def _device(path: Path):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def state(show: shows.Show | None) -> dict:
    # External drives only: never the startup disk.
    startup = os.stat("/").st_dev
    drives = [str(p) for p in sorted(Path("/Volumes").iterdir()) if p.is_dir() and not p.name.startswith(".")
              and _device(p) not in (startup, None)] if Path("/Volumes").exists() else []
    profiles = sorted(set(shows.BUILT_IN) | set(shows.saved_profiles().get("profiles", {})))
    if not show:
        return {"show": None, "drives": drives, "profiles": profiles}
    progress = shows._read_json(show.logs / "progress.json", None)
    ingest = shows._read_json(show.folder / "ingest.json", {"ingests": []})
    result = {"show": show.data, "folder": str(show.folder), "drives": drives, "profiles": profiles,
              "progress": progress, "cards": ingest["ingests"],
              "running": showday._alive(show.folder / ".runner.pid"),
              "profile": show.profile, "delivery": show.delivery,
              "setup": shows._read_json(show.folder / "setup" / "setup.json", None)}
    try:
        result["status"] = shows.status(show)
    except (CompError, OSError, ValueError, KeyError) as error:
        result["status"] = {"error": str(error)}
    failures = show.logs / "failures.csv"
    result["failures"] = failures.read_text().splitlines()[-20:] if failures.exists() else []
    return result


def frames(show: shows.Show) -> dict:
    if not (show.cull_folder / "cull.json").exists():
        return {"frames": []}
    every = show.frames()
    decisions = shows._read_json(show.cull_folder / "decisions.json", {"auto": {}, "manual": {}})
    crops = shows._read_json(show.folder / "crops.json", {"manual": {}, "auto": {}})
    names = shows.graded_names([f["path"] for f in every])
    keep = ("path", "name", "moment", "status", "score", "flags", "captured", "sharpness", "face_quality", "eyes",
            "aesthetics", "faces", "ev", "width", "height")
    rows = []
    for f in every:
        row = {k: f.get(k) for k in keep}
        row["auto"] = decisions["auto"].get(f["path"])
        row["manual"] = decisions["manual"].get(f["path"])
        row["graded"] = (show.graded_folder / f"{names[f['path']]}.jpg").exists()
        row["framing"] = crops["manual"].get(f["path"]) or (crops["auto"].get(f["path"]) if show.profile.get("auto_crop") else None)
        row["framing_by_hand"] = f["path"] in crops["manual"]
        rows.append(row)
    return {"frames": rows}


def image(show: shows.Show, kind: str, path: str | None) -> Path:
    """The picture to show: a frame's thumbnail or large preview (made from its embedded preview on first ask), its
    graded render, or the setup sheet. Only frames of this show are served."""
    if kind == "setup":
        return show.folder / "setup" / "setup-sheet.jpg"
    known = {f["path"] for f in show.frames()}
    if path not in known:
        raise CompError("not a frame of this show")
    stem = Path(path).stem
    if kind == "graded":
        names = shows.graded_names(sorted(known))
        return show.graded_folder / f"{names[path]}.jpg"
    folder, size = (show.cull_folder / "sheets" / ".previews", 220) if kind == "thumb" else (show.cull_folder / ".loupe", 1600)
    target = folder / f"{stem}.jpg"
    if not target.exists():
        with _preview_lock:
            if not target.exists():
                _run_listed("previews", [Path(path)], "--out", str(folder), "--size", str(size))
    return target


def act(show: shows.Show | None, action: str, body: dict) -> dict:
    if action == "start":
        started = showday.start_day(body["drive"], body["name"], body["profile"], date=body.get("date") or None,
                                    naming=body.get("naming") or None)
        return {"show": str(started.folder)}
    if action == "finish":
        showday.finish_day()
        return {"finished": True}
    if not show:
        raise CompError("no show today: start one first")
    if action == "status":
        summary = shows.set_status(show.cull_folder, body["changes"])
    elif action == "redial":
        summary = shows.pick_show(show, rate=parse_rate(body["rate"]) if body.get("rate") else None,
                                  count=int(body["count"]) if body.get("count") else None)
    elif action == "approve":
        summary = shows.approve_setup(show, body["choice"], body.get("tweaks") or None)
    elif action == "framing":
        summary = shows.set_framing(show, body["path"], crop=body.get("crop"), rotate=float(body.get("rotate") or 0),
                                    clear=bool(body.get("clear")))
    elif action == "compare":
        report = shows.compare_show(show, body["delivered"], learn=bool(body.get("learn")))
        return {"report": shows.compare_markdown(report)}
    elif action == "open":
        target = show.delivery_root / shows._clean(show.data["name"]) if body.get("what") == "delivery" else show.folder
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(target)])
        return {"opened": str(target)}
    elif action == "run":
        return {"runner": showday.start_runner(show)}
    else:
        raise CompError(f"unknown action {action!r}")
    if body.get("run", True):
        summary = {**(summary if isinstance(summary, dict) else {}), "runner": showday.start_runner(show)}
    return summary


def serve(port: int = 8765, fixed_show=None, open_browser: bool = True) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def send(self, code: int, body: bytes, kind: str = "application/json", cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def fail(self, error):
            self.send(400, json.dumps({"error": str(error)}).encode())

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                show = _show(fixed_show)
                if url.path == "/":
                    self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self.send(200, json.dumps(state(show), default=str).encode())
                elif url.path == "/api/frames":
                    self.send(200, json.dumps(frames(show) if show else {"frames": []}, default=str).encode())
                elif url.path == "/api/report":
                    path = show.folder / "report" / "compare.md" if show else None
                    self.send(200, json.dumps({"report": path.read_text() if path and path.exists() else ""}).encode())
                elif url.path.startswith("/img/") and show:
                    target = image(show, url.path[5:], query.get("path"))
                    if not target.exists():
                        self.send(404, b"{}")
                        return
                    self.send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "image/jpeg",
                              cache=url.path != "/img/graded" and url.path != "/img/setup")
                else:
                    self.send(404, b"{}")
            except (CompError, OSError, ValueError, KeyError) as error:
                self.fail(error)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = act(_show(fixed_show), urlparse(self.path).path.removeprefix("/api/"), body)
                self.send(200, json.dumps(result, default=str).encode())
            except (CompError, OSError, ValueError, KeyError) as error:
                self.fail(error)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/"
    print(f"dashboard at {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()
