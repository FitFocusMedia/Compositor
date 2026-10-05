"""A show day, hands-off: the show is set up once in the morning (drive, name, type), and from then on every card
inserted is copied to the show's drive, verified, and taken through cull → setup → grade → export without being
asked, while the dashboard (dashboard.py) shows what it's doing and takes any changes.

    compkit show start --drive /Volumes/SHOWS --name "Spring Classic" --profile competition
    compkit show watch --install      # once: run `on_mount` whenever a disk mounts, keep the dashboard up
    … insert cards through the day …
    compkit show finish               # the day is done: cards are no longer taken in

Cards are only ever read. A card's photos are copied into SHOW/card/<NNN> <card name>/, each one read back from
the drive and checked against what was read from the card before it counts as copied (SHOW/ingest.json keeps
what's been taken in, so the same card inserted again only adds what's new). Then a notification says the card can
come out. One runner works on the show at a time; a card that arrives while it's busy is picked up when it loops.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import AUTOMATION, CompError
from . import show as shows
from .cull import gather
from .runlog import Progress

LABEL = "com.compositor.toolkit"
NOCACHE = getattr(fcntl, "F_NOCACHE", 48)  # macOS: read and write past the cache, so a read-back really checks the disk


def notify(title: str, text: str) -> None:
    """A macOS notification (COMPKIT_QUIET=1 keeps tests quiet)."""
    if os.environ.get("COMPKIT_QUIET"):
        return
    script = f"display notification {json_string(text)} with title {json_string(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def json_string(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


# The day's show ----------------------------------------------------------------------------------------------------

def _active_file() -> Path:
    return shows.profiles_home() / "active.json"


def active() -> shows.Show | None:
    """Today's show, if one has been started and its drive is connected."""
    data = shows._read_json(_active_file(), None)
    if not data or not (Path(data["show"]) / "show.json").exists():
        return None
    return shows.Show(data["show"])


def start_day(drive, name: str, profile_name: str, *, date: str | None = None, naming: str | None = None) -> shows.Show:
    """Sets up today's show on `drive` (a folder or a mounted volume): SHOW = <drive>/Shows/<date> <name>, with its
    cards copied into SHOW/card. Cards inserted from now on go to it."""
    drive = Path(drive).expanduser().resolve()
    if not drive.is_dir():
        raise CompError(f"{drive} isn't connected")
    date = date or dt.date.today().isoformat()
    folder = drive / "Shows" / shows._clean(f"{date} {name}")
    (folder / "card").mkdir(parents=True, exist_ok=True)
    show = shows.Show(folder) if (folder / "show.json").exists() else \
        shows.Show.create(folder, cards=[folder / "card"], profile_name=profile_name, name=name, date=date, naming=naming)
    shows._write_json(_active_file(), {"show": str(show.folder), "started": shows._now()})
    return show


def finish_day() -> None:
    _active_file().unlink(missing_ok=True)


# Cards -------------------------------------------------------------------------------------------------------------

def card_volumes(show: shows.Show | None = None) -> list[Path]:
    """Mounted cards: volumes with a DCIM folder, other than the startup disk and the show's own drive."""
    exclude = {os.stat("/").st_dev}
    if show:
        exclude.add(os.stat(show.folder).st_dev)
    found = []
    for volume in sorted(Path("/Volumes").iterdir()) if Path("/Volumes").exists() else []:
        try:
            if (volume / "DCIM").is_dir() and os.stat(volume).st_dev not in exclude:
                found.append(volume)
        except OSError:
            continue
    return found


def _copy_verified(source: Path, target: Path) -> str:
    """Copies a file, then reads the copy back from the drive and compares checksums before it takes its name."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    digest = hashlib.sha1()
    with open(source, "rb") as reading, open(partial, "wb") as writing:
        fcntl.fcntl(writing.fileno(), NOCACHE, 1)
        while chunk := reading.read(8 << 20):
            digest.update(chunk)
            writing.write(chunk)
        writing.flush()
        os.fsync(writing.fileno())
    check = hashlib.sha1()
    with open(partial, "rb") as reading:
        fcntl.fcntl(reading.fileno(), NOCACHE, 1)
        while chunk := reading.read(8 << 20):
            check.update(chunk)
    if check.hexdigest() != digest.hexdigest():
        partial.unlink(missing_ok=True)
        raise CompError("the copy didn't match the card when read back")
    shutil.copystat(source, partial)
    os.replace(partial, target)
    return digest.hexdigest()


def ingest(show: shows.Show, volumes: list[Path] | None = None, *, notify_user: bool = True) -> dict:
    """Copies every photo on the mounted cards that this show hasn't taken in yet (see the module notes)."""
    volumes = card_volumes(show) if volumes is None else volumes
    ledger_path = show.folder / "ingest.json"
    ledger = shows._read_json(ledger_path, {"files": {}, "ingests": []})
    progress = Progress("ingest", folder=show.logs, unit="photos")
    totals = {"cards": [], "copied": 0, "already": 0, "failed": 0}
    for volume in volumes:
        volume = Path(volume).resolve()
        pictures = gather([volume / "DCIM"])
        todo = []
        for picture in pictures:
            stat = picture.stat()
            key = f"{picture.relative_to(volume)}|{stat.st_size}|{stat.st_mtime_ns}"
            if key not in ledger["files"]:
                todo.append((picture, key, stat.st_size))
        totals["already"] += len(pictures) - len(todo)
        if not todo:
            notify_user and notify(f"{volume.name}: nothing new", "Every photo on this card is already in the show.")
            continue
        need = sum(size for _, _, size in todo)
        free = shutil.disk_usage(show.folder).free
        if need + 2e9 > free:
            notify_user and notify(f"{volume.name}: not copied", f"The show drive needs {need / 1e9:.0f} GB and has {free / 1e9:.0f} GB free.")
            raise CompError(f"{show.folder} has {free / 1e9:.1f} GB free; the card needs {need / 1e9:.1f} GB")
        number = len(ledger["ingests"]) + 1
        destination = show.folder / "card" / shows._clean(f"{number:03d} {volume.name}")
        notify_user and notify(f"Copying {volume.name}", f"{len(todo):,} new photos ({need / 1e9:.1f} GB) to {show.data['name']}")
        progress.start(len(todo))
        copied = failed = 0
        for index, (picture, key, _) in enumerate(todo):
            try:
                target = destination / picture.relative_to(volume)
                ledger["files"][key] = {"to": str(target), "sha1": _copy_verified(picture, target)}
                copied += 1
            except (OSError, CompError) as error:
                progress.fail(picture, error)
                failed += 1
            progress.advance()
            if index % 25 == 24:
                shows._write_json(ledger_path, ledger)
        ledger["ingests"].append({"number": number, "card": volume.name, "folder": str(destination), "at": shows._now(),
                                  "copied": copied, "failed": failed})
        shows._write_json(ledger_path, ledger)
        totals["cards"].append(volume.name)
        totals["copied"] += copied
        totals["failed"] += failed
        if notify_user:
            notify(f"{volume.name}: safe to remove" if not failed else f"{volume.name}: {failed} photos failed to copy",
                   f"{copied:,} photos copied and verified." + (" See the dashboard." if failed else ""))
    progress.finish()
    return totals


# Running the show --------------------------------------------------------------------------------------------------

def _alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        return pid != os.getpid()
    except (OSError, ValueError):
        return False


def run(show: shows.Show, *, hands_off: bool = True, notify_user: bool = True) -> dict:
    """Takes the show as far as it can go: cull (new frames only), setup (approved automatically with the show base,
    column B, when hands-off and nothing is approved yet), grade, export (removing delivered files of frames no longer
    picked). Loops while cards or dashboard changes arrive; if another run is going, leaves it a note instead."""
    lock, pending = show.folder / ".runner.pid", show.folder / ".pending"
    if _alive(lock):
        pending.touch()
        return {"queued": True}
    lock.write_text(str(os.getpid()))
    results = {}
    try:
        while True:
            pending.unlink(missing_ok=True)
            show = shows.Show(show.folder)
            if not gather(show.data["cards"]):
                return {"waiting": "no photos yet"}
            results["cull"] = shows.cull_show(show)
            show = shows.Show(show.folder)
            if not show.data.get("settings") and hands_off:
                shows.setup_show(show)
                shows.approve_setup(show, "B")
                show = shows.Show(show.folder)
                notify_user and notify(show.data["name"], f"Settings chosen: {shows._describe(show.data['settings']['default'])}")
            if not show.data.get("settings"):
                notify_user and notify(show.data["name"], "Waiting for the setup sheet's approval in the dashboard.")
                return {**results, "waiting": "setup approval"}
            results["grade"] = shows.grade_show(show)
            results["export"] = shows.export_show(show, prune=True)
            if notify_user:
                picks = results["cull"]["picks"]
                notify(show.data["name"], f"Delivered {picks:,} photos from {results['cull']['frames']:,}"
                                          + (f"; {results['export']['failed']} failed" if results["export"]["failed"] else ""))
            if not pending.exists():
                return results
    finally:
        lock.unlink(missing_ok=True)


def start_runner(show: shows.Show) -> str:
    """Starts a run in the background, or, if one is going, has it loop once more when it's done."""
    if _alive(show.folder / ".runner.pid"):
        (show.folder / ".pending").touch()
        return "queued"
    show.logs.mkdir(parents=True, exist_ok=True)
    log = open(show.logs / "runner.log", "a")
    subprocess.Popen([sys.executable, "-m", "compkit", "show", "run", str(show.folder)], cwd=str(AUTOMATION),
                     env={**os.environ, "PYTHONPATH": str(AUTOMATION)}, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return "started"


def on_mount() -> dict:
    """What happens when a disk mounts (the launchd agent calls this): a card goes into today's show and the show
    runs on; a card with no show started gets a notification and is left alone."""
    show = active()
    volumes = card_volumes(show)
    if not volumes:
        return {"cards": 0}
    if not show:
        notify("Card inserted", "No show is set up for today, so nothing was copied. Start one in the dashboard.")
        return {"cards": len(volumes), "show": None}
    busy = show.folder / ".ingest.pid"
    if _alive(busy):
        return {"cards": len(volumes), "ingest": "already copying"}
    busy.write_text(str(os.getpid()))
    try:
        summary = ingest(show, volumes)
    finally:
        busy.unlink(missing_ok=True)
    if summary["copied"]:
        summary["runner"] = start_runner(show)
    return summary


# The launchd agents --------------------------------------------------------------------------------------------------

def _agents() -> dict[str, dict]:
    launcher = str(Path("~/.local/bin/compkit").expanduser())
    logs = Path("~/Library/Logs").expanduser()
    return {
        f"{LABEL}.cards": {"Label": f"{LABEL}.cards", "ProgramArguments": [launcher, "show", "on-mount"],
                           "StartOnMount": True, "StandardOutPath": str(logs / "compkit-cards.log"),
                           "StandardErrorPath": str(logs / "compkit-cards.log")},
        f"{LABEL}.dashboard": {"Label": f"{LABEL}.dashboard", "ProgramArguments": [launcher, "dashboard", "--no-open"],
                               "RunAtLoad": True, "KeepAlive": True,
                               "StandardOutPath": str(logs / "compkit-dashboard.log"),
                               "StandardErrorPath": str(logs / "compkit-dashboard.log")},
    }


def watch(action: str) -> dict:
    """install: the card watcher and the always-on dashboard as launchd agents in ~/Library/LaunchAgents;
    uninstall: removes them; status: whether they're loaded."""
    folder = Path("~/Library/LaunchAgents").expanduser()
    domain = f"gui/{os.getuid()}"
    result = {}
    for label, agent in _agents().items():
        plist = folder / f"{label}.plist"
        if action == "install":
            folder.mkdir(parents=True, exist_ok=True)
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
            plist.write_bytes(plistlib.dumps(agent))
            done = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
            result[label] = "installed" if done.returncode == 0 else f"failed: {done.stderr.strip()}"
        elif action == "uninstall":
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
            plist.unlink(missing_ok=True)
            result[label] = "removed"
        else:
            loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True).returncode == 0
            result[label] = "loaded" if loaded else ("installed, not loaded" if plist.exists() else "not installed")
    return result


def wait_for_idle(show: shows.Show, timeout: float = 3600) -> bool:
    """Waits for the show's background run to finish (for scripts and tests)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(show.folder / ".runner.pid") and not (show.folder / ".pending").exists():
            return True
        time.sleep(1)
    return False
