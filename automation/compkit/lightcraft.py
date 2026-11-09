"""A show's LightCraft catalogue: one Lightroom-style library per show that the team browses, flags, stars, adjusts
and exports in LightCraft, kept in step with the show's cull by the toolkit. Nothing is graded into per-photo
projects: a photo is its original plus settings, and the catalogue's previews are 2048 px whatever the camera.

    SHOW/catalogue/        the LightCraft library: open it in LightCraft
    SHOW/catalogue.json    the toolkit's side: what it last gave each photo, the base photos and the samples

`sync` imports the cards' new photos in place (no copies, and no sidecars written next to the originals), gives them
the show's base (its preset, or the base photos' settings once `base` set them) and writes the cull's decisions as
flags and stars: pick = Pick ★3, alternate = ★1, reject = Reject. A photo whose flag or stars someone changed in
LightCraft is never overwritten, and a changed flag becomes a decision of the show made by hand, so it holds
through later cards. `base` makes photos someone adjusted by hand the show's base: every photo still on the base
takes the settings of the base photo shot nearest it in time (so each round's lighting can have its own), and the
adjusted photos are kept as samples (original and settings) to learn the look from later. `export` writes the
Pick-flagged photos to the show's delivery spec, with the same folders and names as `compkit show export`.

The toolkit talks to LightCraft through `lightcraft-cli mcp`: headless while the library is closed, or through the
running app's control port while the team has it open (start LightCraft with `--control 7980`; LIGHTCRAFT_CONTROL
names another address).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import AUTOMATION, CompError
from . import show as shows

# The cull's decisions as LightCraft flags and stars.
MARKS = {"pick": ("pick", 3), "alternate": ("none", 1), "reject": ("reject", 0)}
# Settings that belong to one photo, not to a look: develop.copy leaves them out, and changing them isn't
# taking a photo off the base.
OWN = {"crop", "masks", "spots", "red_eye", "orientation", "geometry", "version"}
# Photos per export command: about half a minute at 61 MP, well inside the running app's command timeout.
BATCH = 24


def cli() -> str:
    for candidate in (os.environ.get("LIGHTCRAFT_CLI"), shutil.which("lightcraft-cli"), AUTOMATION / "bin" / "lightcraft-cli"):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise CompError("lightcraft-cli isn't installed: run automation/install.sh")


class Catalogue:
    """An MCP session with lightcraft-cli on one library: headless, or the running app when it has the library open."""

    def __init__(self, library: Path):
        self.calls, self.connected = 0, False
        try:
            self._start(["mcp", "--library", str(library)])
        except CompError as error:
            if "already open" not in str(error):
                raise
            address = os.environ.get("LIGHTCRAFT_CONTROL", "127.0.0.1:7980")
            try:
                self._start(["mcp", "--connect", address])
                self.connected = True
            except CompError:
                raise CompError(f"the catalogue is open in LightCraft without its control port: start LightCraft with "
                                f"`--control {address.rsplit(':', 1)[-1]}`, or close it, and run this again") from None

    def _start(self, args: list[str]) -> None:
        self.errors = tempfile.TemporaryFile("w+")  # a file, not a pipe: a chatty stderr can't stall the session
        self.process = subprocess.Popen([cli(), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, text=True)
        try:
            self._call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "compkit", "version": "1"}})
        except CompError:
            self.process.kill()
            self.errors.seek(0)
            raise CompError(self.errors.read().strip() or "lightcraft-cli mcp didn't start") from None
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _call(self, method: str, params: dict):
        self.calls += 1
        try:
            self._send({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params})
        except BrokenPipeError:
            raise CompError("lightcraft-cli stopped") from None
        for line in self.process.stdout:
            message = json.loads(line)
            if message.get("id") == self.calls:
                if "error" in message:
                    raise CompError(f"LightCraft: {message['error'].get('message')}")
                return message["result"]
        raise CompError("lightcraft-cli stopped")

    def tool(self, name: str, **arguments):
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise CompError(f"LightCraft {name}: {' '.join(c.get('text', '') for c in result.get('content', []))}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        text = "".join(c.get("text", "") for c in result.get("content", []))  # a list result comes back as text only
        try:
            return json.loads(text)
        except ValueError:
            return None  # a command with nothing to return says so in words

    def command(self, command: str, **params):
        return self.tool("command_run", id=command, params=params)

    def photos(self) -> dict[int, dict]:
        """Every photo in the library (not just the app's current view): id, fileName, flag, rating, captured…"""
        found, total = [], None
        while total is None or len(found) < total:
            page = self.tool("query_photos", filter={}, limit=2000, offset=len(found))
            total, found = page["total"], found + page["photos"]
            if not page["photos"]:
                break
        return {p["id"]: p for p in found if not p.get("deleted")}

    def ids(self, folders: list[str]) -> dict[str, int]:
        """The library's photo id of every file under `folders`, by path."""
        preview = self.command("library.importPreview", paths=folders)
        return {c["path"]: c["existing"] for c in preview["candidates"] if c.get("existing")}

    def settings(self, photo: int) -> dict:
        """A photo's settings that make its look, leaving out its own crop, masks and spots."""
        return {k: v for k, v in self.tool("get_develop", id=photo).items() if k not in OWN}

    def look(self, photo: int) -> str:
        return hashlib.sha1(json.dumps(self.settings(photo), sort_keys=True).encode()).hexdigest()

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=120)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def library(show: shows.Show) -> Path:
    return show.folder / "catalogue"


def _state(show: shows.Show) -> dict:
    return shows._read_json(show.folder / "catalogue.json", None) or {"preset": None, "given": {}, "written": {},
                                                                     "bases": [], "samples": []}


def _when(captured: str | None) -> float:
    try:
        return dt.datetime.fromisoformat(captured).timestamp() if captured else 0.0
    except ValueError:
        return 0.0


def _give(cat: Catalogue, state: dict, photos: dict[int, dict], ids: list[int]) -> None:
    """Gives photos the show's base: the settings of the base photo shot nearest each one, else the preset."""
    if not ids:
        return
    if not state["bases"]:
        cat.command("preset.apply", id=state["preset"], ids=ids)
    else:
        nearest = lambda i: min(state["bases"], key=lambda b: abs(_when(photos[i]["captured"]) - _when(b["captured"])))
        groups: dict[str, list[int]] = {}
        for i in ids:
            groups.setdefault(nearest(i)["preset"], []).append(i)
        for preset, group in groups.items():
            cat.command("preset.apply", id=preset, ids=group)
    for i in ids:
        state["given"][str(i)] = cat.look(i)


def sync(show: shows.Show) -> dict:
    """Brings the catalogue up to date with the show: new photos in, with the base, and the cull's decisions as
    flags and stars where nobody has changed them by hand (see the module notes)."""
    state = _state(show)
    cards = [str(Path(c)) for c in show.data["cards"]]
    frames = {f["path"]: f for f in show.frames()} if (show.cull_folder / "cull.json").exists() else {}
    with Catalogue(library(show)) as cat:
        if not state["preset"]:
            cat.command("library.xmpPreferences", autoWrite=False)  # the originals' folders are only ever read
            state["preset"] = cat.command("preset.import", paths=[show.data["preset"]])["imported"][0]["id"]
        new = cat.command("library.import", paths=cards, mode="add")["imported"]
        photos = cat.photos()
        _give(cat, state, photos, new)
        if new and cat.connected:
            # In the open app's background. Headless, waiting would lock the catalogue for hours on a big RAW show
            # (about a second a photo); the app renders what's viewed anyway.
            cat.command("library.buildPreviews", ids=new, size="standard", wait=False)
        by_hand, marks = {}, {}
        for path, photo in cat.ids(cards).items():
            frame, now = frames.get(path), [photos[photo]["flag"], photos[photo]["rating"]]
            written = state["written"].get(path)
            if written is not None and now != written:  # someone changed it in LightCraft: theirs wins
                if now[0] != written[0]:
                    by_hand[path] = {"pick": "pick", "reject": "reject"}.get(now[0], "alternate")
                continue
            if frame is None:
                continue
            wanted = list(MARKS[frame["status"]])
            if now != wanted:
                marks.setdefault(("photo.flag", "flag", wanted[0]), []).append(photo)
                marks.setdefault(("photo.rate", "rating", wanted[1]), []).append(photo)
            state["written"][path] = wanted
        for (command, field, value), ids in marks.items():
            cat.command(command, **{field: value, "ids": ids})
    if by_hand:
        shows.set_status(show.cull_folder, {p: s for p, s in by_hand.items() if p in frames})
    shows._write_json(show.folder / "catalogue.json", state)
    show.data["catalogue"] = str(library(show))
    show.save()
    return {"catalogue": str(library(show)), "photos": len(photos), "imported": len(new),
            "flags and stars written": len({i for ids in marks.values() for i in ids}),
            "changed by hand in LightCraft": len(by_hand)}


def base(show: shows.Show, names: list[str]) -> dict:
    """Makes hand-adjusted photos the show's base: every photo still on the base takes the settings of the base photo
    shot nearest it, and the adjusted photos are kept as samples. `names` are file names or paths."""
    state = _state(show)
    if not state["preset"]:
        raise CompError("no catalogue yet: run `compkit show catalogue SHOW` first")
    cards = [str(Path(c)) for c in show.data["cards"]]
    with Catalogue(library(show)) as cat:
        ids, photos = cat.ids(cards), cat.photos()
        chosen = []
        for name in names:
            matches = [(p, i) for p, i in ids.items() if p == str(Path(name).expanduser().resolve()) or Path(p).name == name]
            if len(matches) != 1:
                raise CompError(f"{name} {'is in the catalogue more than once: give its path' if matches else 'is not in the catalogue'}")
            chosen += matches
        # Each base photo becomes a preset in the show's group (also there for the team to use in LightCraft); making
        # one needs the photo active, so whoever is working in the app gets their selection straight back.
        selection = cat.tool("doc_inspect")["library"]["selection"]
        for old in state["bases"]:
            cat.command("preset.delete", id=old["preset"])
        state["bases"] = []
        for n, (p, i) in enumerate(chosen, start=1):
            cat.command("library.select", ids=[i], active=i)
            preset = cat.command("preset.create", name=f"{show.data['name']} base {n}", group=show.data["name"])["id"]
            state["bases"].append({"id": i, "path": p, "captured": photos[i]["captured"], "preset": preset})
            state["samples"].append({"path": p, "settings": cat.tool("get_develop", id=i), "at": shows._now()})
        if selection.get("ids"):
            cat.command("library.select", ids=selection["ids"], **({"active": selection["active"]} if selection.get("active") else {}))
        else:
            cat.command("library.selectNone")
        mine = {i for _, i in chosen}
        on_base = [i for i in photos if i not in mine and state["given"].get(str(i)) == cat.look(i)]
        _give(cat, state, photos, on_base)
        for i in mine:
            state["given"][str(i)] = cat.look(i)
    shows._write_json(show.folder / "catalogue.json", state)
    return {"bases": [Path(p).name for p, _ in chosen], "photos given the new base": len(on_base),
            "adjusted by hand, left alone": len(photos) - len(on_base) - len(mine), "samples kept": len(state["samples"])}


def export(show: shows.Show, *, prune: bool = False, folder=None) -> dict:
    """Writes the Pick-flagged photos to the show's delivery spec from LightCraft: the folders and names of
    `compkit show export` (shows.delivery_plan), into `folder` instead of the show's delivery folder if given. Only
    what's new or changed is rendered: a file already delivered from the same settings is kept."""
    state = _state(show)
    done = state.setdefault("exported", {})
    cards = [str(Path(c)) for c in show.data["cards"]]
    culled = {f["path"]: f for f in show.frames()} if (show.cull_folder / "cull.json").exists() else {}
    root = shows._clean(show.data["name"])
    target = (Path(folder).expanduser().resolve() if folder else show.delivery_root) / root
    with Catalogue(library(show)) as cat:
        photos, ids = cat.photos(), cat.ids(cards)
        picks = {p: i for p, i in ids.items() if photos[i]["flag"] == "pick"}
        frames = [culled.get(p) or {"path": p, "name": Path(p).name,
                                    "captured": (photos[i]["captured"] or "").replace("T", " ")[:19] or None}
                  for p, i in picks.items()]
        plan = shows.delivery_plan(show, frames)
        quality = round(show.delivery.get("quality", 0.92) * 100)
        settings = {i: json.dumps(cat.tool("get_develop", id=i), sort_keys=True) for i in picks.values()}
        written = kept = 0
        for n, size in enumerate(show.delivery["sizes"]):
            by_folder: dict[Path, list[tuple[int, Path]]] = {}
            for entry in plan:
                out = target / entry["outputs"][n]["path"].relative_to(show.delivery_root / root)
                by_folder.setdefault(out.parent, []).append((picks[entry["frame"]["path"]], out))
            options = {"longEdge": size.get("max_side") or 0, "quality": quality, "metadata": "all", "conflict": "overwrite"}
            if size.get("max_kb"):
                options["limitKb"] = size["max_kb"]
            made_from = lambda i: hashlib.sha1((settings[i] + json.dumps(options, sort_keys=True)).encode()).hexdigest()
            for out_folder, items in by_folder.items():
                due = [(i, out) for i, out in items if not out.exists() or done.get(str(out)) != made_from(i)]
                kept += len(items) - len(due)
                if not due:
                    continue
                items = due
                staging = out_folder / ".lightcraft"
                staging.mkdir(parents=True, exist_ok=True)
                try:
                    # Batches (LightCraft renders a batch in parallel), named by the original's name, then moved to
                    # the planned names; a name two picks share goes on its own. Batches stay small: the running
                    # app's control channel times out a long command.
                    stems = Counter(Path(photos[i]["fileName"]).stem for i, _ in items)
                    batch = [(i, out) for i, out in items if stems[Path(photos[i]["fileName"]).stem] == 1]
                    for start in range(0, len(batch), BATCH):
                        part = batch[start:start + BATCH]
                        cat.command("app.export", ids=[i for i, _ in part], dir=str(staging), naming="{name}", **options)
                        for i, out in part:
                            (staging / f"{Path(photos[i]['fileName']).stem}.jpg").replace(out)
                            done[str(out)] = made_from(i)
                            written += 1
                    for i, out in items:
                        if (i, out) not in batch:
                            cat.command("app.export", ids=[i], path=str(staging / f"{i}.jpg"), **options)
                            (staging / f"{i}.jpg").replace(out)
                            done[str(out)] = made_from(i)
                            written += 1
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
                    shows._write_json(show.folder / "catalogue.json", state)  # an interrupted export resumes from here
    expected = {target / o["path"].relative_to(show.delivery_root / root) for e in plan for o in e["outputs"]}
    extra = [p for p in target.rglob("*.jpg") if p not in expected] if target.exists() else []
    if prune:
        for path in extra:
            path.unlink()
    return {"delivery": str(target), "picks": len(picks), "written": written, "already up to date": kept,
            "delivered files that aren't picks": len(extra), "removed": len(extra) if prune else 0}
