"""Template automation for Compositor projects.

    python -m compkit info TEMPLATE.comp
    python -m compkit analyze PHOTO [--box 1080x1350] [--preview crop.jpg]
    python -m compkit crop PHOTO... --box 1080x1350 [--box 1080x1920] --out crops/
    python -m compkit cull SHOOT/ --out cull/ [--profile workshop | --rate 40% | --count 1000 | --per-moment 1] [--ratings]
    python -m compkit pick cull/ --rate 35% | --count 1000          (re-dial a cull's picks in seconds)
    python -m compkit show new|cull|pick|setup|grade|export|compare|status|profiles|defaults SHOW …
    python -m compkit show start --drive /Volumes/X --name "…" --profile competition   (a hands-off show day)
    python -m compkit show watch --install | run SHOW | mark SHOW … | crop SHOW … | finish
    python -m compkit dashboard [--port 8765]
    python -m compkit learn-look EXPORTS/ RAWS/ -o look.cube
    python -m compkit grade PHOTO... | --list cull/picks.txt --preset base.xmp [--match look.cube] --out graded/ [--render] [--jobs 6]
    python -m compkit grade project.comp --layer Photo --preset base.xmp [--out new.comp]
    python -m compkit sheet renders/*.jpg -o renders/_contact-sheet.jpg
    python -m compkit measure graded/*.jpg [--subject] [--json]
    python -m compkit where
    python -m compkit fill TEMPLATE.comp OUT.comp --text Headline="New PB" --image Photo=shot.jpg --render OUT.jpg
    python -m compkit batch TEMPLATE.comp rows.csv --out renders/ [--format jpg|png] [--keep-comp] [--max-side 1080]

A template is an ordinary .comp whose replaceable layers have stable names. `fill` swaps content by layer name and
keeps everything else (placement, masks, effects, grades). Pictures are cropped around faces and the subject
automatically; a focus ("x,y", 0–1) overrides that. In a batch CSV, `name` is each output's file name and the
other columns name what to change:

    name,text:Headline,text:Subhead,image:Photo,focus:Photo,hide:Tag
    jack-pb,New PB,Squat 220 kg,shots/jack.jpg,"0.5,0.3",
    mia-pb,Back at it,Deadlift 180 kg,shots/mia.heic,,yes
"""

import argparse
import csv
import json
import os

from PIL import Image
import sys
from pathlib import Path

from pathlib import Path as _Path

from . import (AUTOMATION, CompError, Project, analyze, auto_focus, cover_crop, cull, fit_image, learn_look, load_image,
               measure, write_ratings, write_results)
from . import show as shows
from .cull import parse_rate
from .runlog import Progress


def tree(project: Project) -> str:
    lines = []

    def visit(parent, depth):
        for layer in reversed(project.children(parent)):
            if layer.is_group: kind = "folder"
            elif layer.adjustment: kind = f"adjustment: {layer.adjustment['kind']}"
            elif layer.text:
                kind = f"text: {layer.text['content']!r} ({layer.text['fontName']} {layer.text['fontSize']:g}px"
                if layer.text.get("boxSize"):
                    area = layer.text_area
                    kind += f", box {area[2]:g}×{area[3]:g} at {area[0]:g},{area[1]:g})"
                else:
                    kind += ", point text)"
                if layer.text.get("boxSize") and layer.text_metrics()["overflow"]:
                    kind += "  ⚠ OVERFLOWS ITS BOX (cut off; batch fills will shrink it)"
            else:
                (w, h) = layer.transform["size"]
                kind = f"pixels {w:g}×{h:g} at {layer.transform['origin'][0]:g},{layer.transform['origin'][1]:g}"
            extras = []
            if not layer.visible: extras.append("hidden")
            if layer.opacity != 1: extras.append(f"{layer.opacity:.0%}")
            if layer.blend_mode != "Normal": extras.append(layer.blend_mode)
            if layer.record.get("maskFile"): extras.append("mask")
            if layer.record.get("maskSourceID"): extras.append("clipped")
            if layer.record.get("effects"): extras.append("fx: " + ", ".join(layer.record["effects"]))
            lines.append(f"{'  ' * depth}{layer.name}  [{kind}]{'  ' + ' · '.join(extras) if extras else ''}")
            if layer.is_group:
                visit(layer, depth + 1)

    visit(None, 0)
    return f"{project.width}×{project.height}, {len(project.layers)} layers (top first)\n" + "\n".join(lines)


def preview(picture, found, crop, output) -> None:
    """The photo with what the auto crop saw drawn over it: faces green, subject yellow, people magenta, salient
    blue, and the chosen crop white, with everything outside it dimmed."""
    from PIL import ImageDraw
    image = load_image(picture).convert("RGB")
    image.thumbnail((1600, 1600))
    w, h = image.size
    scale = w / found["width"]
    if crop:
        left, top, cw, ch = (round(v * scale) for v in crop)
        shade = image.point(lambda v: v // 3)
        shade.paste(image.crop((left, top, left + cw, top + ch)), (left, top))
        image = shade
    draw = ImageDraw.Draw(image)
    line = max(2, w // 300)

    def outline(box, color):
        draw.rectangle((box["x"] * w, box["y"] * h, (box["x"] + box["width"]) * w, (box["y"] + box["height"]) * h),
                       outline=color, width=line)

    for box in found.get("salient", []): outline(box, "#3D8BFF")
    for box in found.get("people", []): outline(box, "#FF3DD8")
    if found.get("subject"): outline(found["subject"], "#FFD400")
    for box in found.get("faces", []): outline(box, "#3DFF6E")
    if crop:
        draw.rectangle((left, top, left + cw - 1, top + ch - 1), outline="#FFFFFF", width=line * 2)
    image.save(output, quality=88)
    print(f"preview {output}", file=sys.stderr)


def padded(image, size, pad, label):
    """The whole picture fitted inside `size`, over a blurred, darkened copy of itself or a solid color."""
    from PIL import ImageFilter
    from . import rgb
    w, h = size
    fitted, (px, py, pw, ph) = fit_image(image, size, "contain", max_scale=None, label=label)
    if pad == "blur":
        back, _ = fit_image(image, size, "cover", (0.5, 0.5), max_scale=None)
        back = back.resize(size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(max(w, h) / 40))
        back = Image.blend(back, Image.new("RGBA", size, (0, 0, 0, 255)), 0.35)
    else:
        back = Image.new("RGBA", size, tuple(round(c * 255) for c in rgb(pad)) + (255,))
    back.alpha_composite(fitted.resize((round(pw), round(ph)), Image.LANCZOS), (round(px), round(py)))
    return back


def contact_sheet(images, output, columns=0, size=360) -> str:
    """Every image as a labeled thumbnail on one page."""
    import math
    from PIL import ImageDraw, ImageFont
    paths = [_Path(p) for p in images if _Path(p).resolve() != _Path(output).resolve()]
    # Each design before its alternatives (a "-alt" sorts after the name it varies).
    paths.sort(key=lambda p: (p.stem.removesuffix("-alt"), p.stem.endswith("-alt"), str(p)))
    if not paths:
        raise CompError("no images for the contact sheet")
    columns = columns or max(1, math.ceil(math.sqrt(len(paths))))
    rows = math.ceil(len(paths) / columns)
    cell, label, gap = size, 28, 12
    page = Image.new("RGB", (columns * (cell + gap) + gap, rows * (cell + label + gap) + gap), "#1E1E1E")
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except OSError:
        font = ImageFont.load_default()
    for index, path in enumerate(paths):
        x = gap + (index % columns) * (cell + gap)
        y = gap + (index // columns) * (cell + label + gap)
        try:
            with Image.open(path) as image:
                dimensions = f"{image.width}×{image.height}"
                image.thumbnail((cell, cell))
                page.paste(image.convert("RGB"), (x + (cell - image.width) // 2, y + (cell - image.height) // 2))
        except OSError as error:
            draw.text((x + 8, y + cell // 2), f"unreadable: {error}", fill="#FF6B6B", font=font)
            dimensions = "?"
        text, room = f"{path.stem}  ({dimensions})", max(12, cell // 8)
        if len(text) > room:
            text = text[: room // 2 - 1] + "…" + text[-(room - room // 2):]
        draw.text((x, y + cell + 6), text, fill="#DDDDDD", font=font)
    page.save(output, quality=88)
    return f"{output}: {len(paths)} images, {columns}×{rows}"


def pairs(values, flag):
    result = []
    for value in values or []:
        if "=" not in value:
            raise CompError(f"{flag} takes LAYER=VALUE, not {value!r}")
        result.append(tuple(value.split("=", 1)))
    return result


def fill(project: Project, texts=(), images=(), hide=(), show=(), focus=None, preset=None, amount=1.0) -> None:
    focus = focus or {}
    for name, value in texts:
        project.layer(name).set_text(value.replace("\\n", "\n"), shrink_to_fit=True)
    for name, path in images:
        layer = project.layer(name)
        if preset and not layer.graded_parts():
            project.grade_layer(layer, preset, source=path, focus=focus.get(name, "auto"), amount=amount)
        else:
            layer.replace_image(path, focus=focus.get(name, "auto"), preset=preset, amount=amount)
    for name in hide:
        project.layer(name).visible = False
    for name in show:
        project.layer(name).visible = True


def show_command(args) -> int:
    """`compkit show …`: the steps of a show (see compkit/show.py)."""
    def settings_from(pairs_list):
        settings = {}
        for text in pairs_list:
            for pair in text.split(","):
                field, _, value = pair.partition("=")
                if not value:
                    raise CompError(f"settings are FIELD=VALUE, not {pair!r}")
                settings[field.strip()] = float(value)
        return settings

    step = args.step
    if step == "profiles":
        if args.set:
            name, assignment = args.set
            field, _, text = assignment.partition("=")
            if field not in ("rate", "gap", "distance", "max_moment", "auto_crop"):
                raise CompError("profile settings that can be set: rate, gap, distance, max_moment, auto_crop")
            value = text.lower() in ("true", "on", "yes", "1") if field == "auto_crop" else None if text == "none" else float(text)
            shows.profile(name)  # an unknown profile fails here
            shows.save_profile(name, {field: value})
        names = sorted(set(shows.BUILT_IN) | set(shows.saved_profiles().get("profiles", {})))
        print(json.dumps({"saved in": str(shows.profiles_home() / "profiles.json"),
                          "profiles": {n: shows.profile(n) for n in names}, "defaults": shows.defaults()}, indent=2))
        return 0
    if step == "defaults":
        data = shows.saved_profiles()
        for key in ("preset", "match"):
            if getattr(args, key):
                data[key] = str(_Path(getattr(args, key)).expanduser().resolve())
        delivery = data.setdefault("delivery", {})
        if args.naming: delivery["naming"] = args.naming
        if args.folders: delivery["folders"] = args.folders
        if args.date_format: delivery["date_format"] = args.date_format
        if args.max_kb:
            delivery["sizes"] = [{**s, "max_kb": args.max_kb} for s in delivery.get("sizes", shows.DELIVERY["sizes"])]
        shows._write_json(shows.profiles_home() / "profiles.json", data)
        print(json.dumps(shows.defaults(), indent=2))
        return 0
    from . import showday
    if step in ("start", "finish", "on-mount", "watch") or (step == "ingest" and not args.show):
        if step == "start":
            started = showday.start_day(args.drive, args.name, args.profile, date=args.date, naming=args.naming)
            result = {"show": str(started.folder), "profile": started.data["profile"],
                      "next": "insert cards; watch at http://localhost:8765 (compkit dashboard)"}
        elif step == "finish":
            showday.finish_day()
            result = {"finished": True}
        elif step == "on-mount":
            result = showday.on_mount()
        elif step == "watch":
            result = showday.watch("install" if args.install else "uninstall" if args.uninstall else "status")
        else:
            show = showday.active()
            if not show:
                raise CompError("no show today: start one with `compkit show start`")
            result = showday.ingest(show)
        print(json.dumps(result, indent=2, default=str))
        return 0
    if step == "new":
        show = shows.Show.create(args.show, cards=args.card, profile_name=args.profile, name=args.name, date=args.date,
                                 preset=args.preset, match=args.match, naming=args.naming,
                                 delivery_folder=args.delivery_folder)
        print(json.dumps({"show": str(show.folder), "profile": shows.profile(args.profile),
                          "next": f"compkit show cull {show.folder}"}, indent=2))
        return 0
    show = shows.Show(args.show)
    if step == "ingest":
        print(json.dumps(showday.ingest(show), indent=2, default=str))
        return 0
    if step == "run":
        print(json.dumps(showday.run(show, hands_off=not args.wait_for_approval), indent=2, default=str))
        return 0
    if step == "mark":
        known = {_Path(f["path"]).name: f["path"] for f in show.frames()} | {f["path"]: f["path"] for f in show.frames()}
        changes = {}
        for status in ("pick", "alternate", "reject", "auto"):
            for photo in getattr(args, status):
                if photo not in known:
                    raise CompError(f"{photo} isn't a frame of this show")
                changes[known[photo]] = None if status == "auto" else status
        summary = shows.set_status(show.cull_folder, changes)
        print(json.dumps({**summary, "runner": showday.start_runner(show) if changes else None}, indent=2))
        return 0
    if step == "crop":
        known = {_Path(f["path"]).name: f["path"] for f in show.frames()} | {f["path"]: f["path"] for f in show.frames()}
        if args.photo not in known:
            raise CompError(f"{args.photo} isn't a frame of this show")
        crop = [float(v) for v in args.crop.split(",")] if args.crop else None
        print(json.dumps(shows.set_framing(show, known[args.photo], crop=crop, rotate=args.rotate, clear=args.clear), indent=2))
        return 0
    if step == "cull":
        summary = shows.cull_show(show, rate=parse_rate(args.rate) if args.rate else None, count=args.count,
                                  sheets=not args.no_sheets)
        summary["next"] = f"look at {show.cull_folder / 'sheets'}, re-dial with `compkit show pick`, then " \
                          f"`compkit show setup {show.folder}`"
    elif step == "pick":
        import time
        began = time.monotonic()
        summary = shows.pick_show(show, rate=parse_rate(args.rate) if args.rate else None, count=args.count,
                                  per_moment=args.per_moment, sheets=not args.no_sheets)
        summary["seconds"] = round(time.monotonic() - began, 1)
    elif step == "setup":
        if args.approve:
            summary = shows.approve_setup(show, args.approve, settings_from(args.set) or None)
            summary["next"] = f"compkit show grade {show.folder}"
        else:
            if not 1 <= args.frames <= 12:
                raise CompError("--frames takes 1 to 12")
            setup = shows.setup_show(show, frames=args.frames, tries=[settings_from([t]) for t in args.tries])
            summary = {"sheet": setup["sheet"], "base from": setup["base_from"],
                       "candidates": {k: f"{c['name']}: {shows._describe(c['settings'])}" for k, c in setup["candidates"].items()},
                       "rows": [f"{r['row']}: {r['name']} at {(r['captured'] or '')[11:19]}, EV {r['ev']}" for r in setup["frames"]],
                       "next": f"look at the sheet, then `compkit show setup {show.folder} --approve B` (or 1=B,2=C,… per row)"}
    elif step == "grade":
        summary = shows.grade_show(show, jobs=args.jobs)
        summary["next"] = f"compkit show export {show.folder}"
    elif step == "export":
        summary = shows.export_show(show, jobs=args.jobs, sizes=args.size, prune=args.prune)
    elif step == "compare":
        report = shows.compare_show(show, args.delivered, learn=args.learn)
        print(shows.compare_markdown(report))
        print(f"(also in {show.folder / 'report'})")
        return 0
    else:
        summary = shows.status(show)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m compkit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    info = commands.add_parser("info", help="print a project's layers")
    info.add_argument("project")

    look = commands.add_parser("analyze", help="what a photo's auto crop sees: faces, people, subject, salient")
    look.add_argument("picture")
    look.add_argument("--box", metavar="WxH", help="also choose the crop for a box of this shape, e.g. 1080x1350")
    look.add_argument("--preview", metavar="OUT.jpg", help="draw the boxes (and the crop) over the photo")
    look.add_argument("--focus", metavar="X,Y", help="show the crop centered here instead of the automatic one")

    cut = commands.add_parser("crop", help="crop photos to exact sizes around faces and the subject")
    cut.add_argument("pictures", nargs="+")
    cut.add_argument("--box", action="append", required=True, metavar="WxH", help="output size; repeat for several")
    cut.add_argument("--out", required=True, help="folder for the crops (named <photo>-<W>x<H>.<format>)")
    cut.add_argument("--format", choices=["jpg", "png"], default="jpg")
    cut.add_argument("--quality", type=int, default=92)
    cut.add_argument("--focus", metavar="X,Y", help="center every crop on this point of the photo (0–1 across, "
                     "0–1 down) instead of choosing automatically; rerun one photo with it to fix just that crop")
    cut.add_argument("--pad", metavar="blur|#RRGGBB", help="fit the whole photo inside each size instead of cropping, "
                     "over a blurred copy of it or a solid color (for subjects that don't fit the shape)")

    commands.add_parser("where", help="print the toolkit's folder (README, examples, tests)")

    gauge = commands.add_parser("measure", help="tone and color numbers (CIELAB) for tuning images to guidelines")
    gauge.add_argument("images", nargs="+")
    gauge.add_argument("--subject", action="store_true", help="measure only the subject (what Remove Background keeps)")
    gauge.add_argument("--json", action="store_true", help="print the full numbers as JSON")

    sheet = commands.add_parser("sheet", help="a contact sheet of images, to check a batch at a glance")
    sheet.add_argument("images", nargs="+")
    sheet.add_argument("-o", "--output", required=True)
    sheet.add_argument("--columns", type=int, default=0, help="default: about square")
    sheet.add_argument("--size", type=int, default=360, help="longest side of each thumbnail")

    culling = commands.add_parser("cull", help="find the frames worth grading in a shoot of hundreds or thousands")
    culling.add_argument("inputs", nargs="+", help="shoot folders (searched recursively) or files")
    culling.add_argument("--out", required=True, help="folder for cull.csv, picks.txt, cull.json and contact sheets")
    culling.add_argument("--profile", help="a show profile (workshop, competition, fight-night): its keep rate, "
                         "grouping and per-moment rule")
    culling.add_argument("--rate", help="keep this share of the frames, spread over moments by size (e.g. 40%%)")
    culling.add_argument("--count", type=lambda v: int(v.replace(",", "")), help="keep this many frames, spread over moments by size")
    culling.add_argument("--keep", type=int, help="with --per-moment: at most this many picks, the best-scoring first")
    culling.add_argument("--per-moment", type=int, help="a fixed number of picks per moment (the default without a "
                         "profile, rate or count: 1)")
    culling.add_argument("--gap", type=float, help="seconds between frames of one moment (default 2)")
    culling.add_argument("--distance", type=float, help="how alike frames of one moment look (default 0.6)")
    culling.add_argument("--no-sheets", action="store_true", help="skip the contact sheets")
    culling.add_argument("--ratings", action="store_true",
                         help="also write Lightroom star ratings as .xmp sidecars beside the RAWs (never over existing ones)")

    redial = commands.add_parser("pick", help="pick again from a finished cull's scores, in seconds")
    redial.add_argument("cull", help="the cull's --out folder")
    redial.add_argument("--rate", help="keep this share of the frames (e.g. 35%%)")
    redial.add_argument("--count", type=lambda v: int(v.replace(",", "")), help="keep this many frames")
    redial.add_argument("--per-moment", type=int, help="a fixed number per moment instead")
    redial.add_argument("--no-sheets", action="store_true", help="skip redrawing the contact sheets")

    show = commands.add_parser("show", help="a whole show: cull, setup, grade, export, compare (see `compkit show -h`)",
                               description=shows.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    steps = show.add_subparsers(dest="step", required=True)
    step = steps.add_parser("new", help="start a show folder")
    step.add_argument("show")
    step.add_argument("--card", action="append", required=True, help="a folder of the show's RAWs (repeat for several cards)")
    step.add_argument("--profile", required=True, help="workshop, competition or fight-night")
    step.add_argument("--name", required=True, help="the show's title, as delivered file names use it")
    step.add_argument("--date", help="YYYY-MM-DD (default: the first frame's capture date)")
    step.add_argument("--preset", help="the base grade (.xmp); default: the one saved with `compkit show defaults`")
    step.add_argument("--match", help="the learned look (.cube); default: the saved one")
    step.add_argument("--naming", help="this show's file names, e.g. \"{show} - {date} - {number}\" (fields: show, date, "
                      "number, stem, seq, hour)")
    step.add_argument("--delivery-folder", help="where the delivery goes (default: SHOW/delivery)")
    step = steps.add_parser("cull", help="cull the show's cards with its profile (resumable)")
    step.add_argument("show")
    step.add_argument("--rate", help="override the profile's keep rate (e.g. 35%%)")
    step.add_argument("--count", type=lambda v: int(v.replace(",", "")), help="keep this many instead")
    step.add_argument("--no-sheets", action="store_true")
    step = steps.add_parser("pick", help="re-dial the keep rate from the cull's scores, in seconds")
    step.add_argument("show")
    step.add_argument("--rate", help="e.g. 35%%")
    step.add_argument("--count", type=lambda v: int(v.replace(",", "")), help="e.g. 1000")
    step.add_argument("--per-moment", type=int)
    step.add_argument("--no-sheets", action="store_true")
    step = steps.add_parser("setup", help="the white balance and exposure sheet, and approving it")
    step.add_argument("show")
    step.add_argument("--frames", type=int, default=5, help="representative frames (rows), 4–6 (default 5)")
    step.add_argument("--try", dest="tries", action="append", default=[], metavar="FIELD=VALUE[,FIELD=VALUE]",
                      help="add a candidate column: these settings on top of the show base")
    step.add_argument("--approve", metavar="B | 1=B,2=C,…", help="save candidate B for the show, or one per sheet row")
    step.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", help="with --approve: adjust on top")
    step = steps.add_parser("grade", help="grade the picks with the approved settings (resumable)")
    step.add_argument("show")
    step.add_argument("--jobs", type=int, default=6)
    step = steps.add_parser("export", help="write the delivery by the spec (resumable)")
    step.add_argument("show")
    step.add_argument("--jobs", type=int, default=3, help="photos at once (default 3; each full-resolution job takes "
                      "up to ~6 GB, so use 2 while working in Lightroom)")
    step.add_argument("--size", action="append", help="only this size of the spec (repeatable), e.g. \"Web 2048\"")
    step.add_argument("--prune", action="store_true", help="remove delivered files whose frames are no longer picks")
    step = steps.add_parser("compare", help="compare with what you delivered; --learn updates the profile")
    step.add_argument("show")
    step.add_argument("--delivered", required=True, help="the folder of your delivered JPEGs (e.g. Lightroom exports)")
    step.add_argument("--learn", action="store_true", help="update the show's profile from it")
    step = steps.add_parser("status", help="what's done and what's next")
    step.add_argument("show")
    step = steps.add_parser("profiles", help="the show profiles and what they've learned")
    step.add_argument("--set", nargs=2, metavar=("PROFILE", "FIELD=VALUE"),
                      help="change one setting of a profile, e.g. --set competition auto_crop=true or rate=0.35")
    step = steps.add_parser("start", help="start today's show on a drive: cards inserted from now on run hands-off")
    step.add_argument("--drive", required=True, help="the show drive (a mounted volume or a folder)")
    step.add_argument("--name", required=True)
    step.add_argument("--profile", required=True, help="workshop, competition or fight-night")
    step.add_argument("--date", help="YYYY-MM-DD (default: today)")
    step.add_argument("--naming", help="this show's file names (default: your usual naming)")
    steps.add_parser("finish", help="the day is done: cards are no longer taken in")
    steps.add_parser("on-mount", help="(run by the card watcher) copy a newly mounted card into today's show and run it")
    step = steps.add_parser("ingest", help="copy the mounted cards into a show now")
    step.add_argument("show", nargs="?", help="default: today's show")
    step = steps.add_parser("run", help="take a show as far as it goes: cull, setup, grade, export (hands-off)")
    step.add_argument("show")
    step.add_argument("--wait-for-approval", action="store_true", help="stop at the setup sheet instead of approving B")
    step = steps.add_parser("watch", help="the card watcher and always-on dashboard (launchd agents)")
    step.add_argument("--install", action="store_true")
    step.add_argument("--uninstall", action="store_true")
    step = steps.add_parser("mark", help="set frames' statuses by hand (kept through later cards)")
    step.add_argument("show")
    for status in ("pick", "alternate", "reject", "auto"):
        step.add_argument(f"--{status}", action="append", default=[], metavar="PHOTO", help=f"photos to mark {status}")
    step = steps.add_parser("crop", help="a photo's crop and leveling by hand")
    step.add_argument("show")
    step.add_argument("photo")
    step.add_argument("--crop", metavar="X,Y,W,H", help="fractions of the leveled photo")
    step.add_argument("--rotate", type=float, default=0.0, help="degrees counterclockwise to level it")
    step.add_argument("--clear", action="store_true", help="back to the automatic framing")

    board = commands.add_parser("dashboard", help="the show dashboard in your browser (served from this Mac only)")
    board.add_argument("--port", type=int, default=8765)
    board.add_argument("--show", help="default: today's show")
    board.add_argument("--no-open", action="store_true", help="don't open a browser window")
    step = steps.add_parser("defaults", help="your default preset, look and delivery spec (saved with the profiles)")
    step.add_argument("--preset")
    step.add_argument("--match")
    step.add_argument("--naming")
    step.add_argument("--folders", help="e.g. \"{size}/{hour}\"")
    step.add_argument("--date-format", help="strftime, e.g. %%d-%%m-%%y")
    step.add_argument("--max-kb", type=int, help="the file-size cap of every size, in KB")

    learn = commands.add_parser("learn-look", help="fit a .cube so graded RAWs match your Lightroom exports")
    learn.add_argument("exports", help="folder of Lightroom JPEG exports that embed their develop settings")
    learn.add_argument("raws", help="folder holding their RAW files")
    learn.add_argument("-o", "--output", required=True, help="the .cube to write")
    learn.add_argument("--frames", type=int, default=200, help="most exports to learn from (default 200)")

    grade = commands.add_parser("grade", help="apply a Lightroom preset as a base grade, kept tunable")
    grade.add_argument("inputs", nargs="*", help="photos, or one .comp with --layer")
    grade.add_argument("--list", help="a file of photo paths, one per line (e.g. a cull's picks.txt)")
    grade.add_argument("--match", help="finish with this .cube look (see learn-look)")
    grade.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2), help="photos graded at once")
    grade.add_argument("--preset", required=True, help="Lightroom / Camera Raw preset (.xmp)")
    grade.add_argument("--amount", type=float, default=1.0, help="preset strength, 0–2 (default 1)")
    grade.add_argument("--layer", help="with a .comp: the picture layer (or graded folder) to grade")
    grade.add_argument("--out", help="photos: folder for the projects; .comp: new path (default: in place)")
    grade.add_argument("--max-side", type=int, default=2048,
                       help="photos: longest side of each project (default 2048); only shrinks, never enlarges")
    grade.add_argument("--render", action="store_true", help="photos: also render each project to JPEG")
    grade.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                       help="Camera Raw field override after the preset (see `comp-render preset`)")

    single = commands.add_parser("fill", help="fill a template once")
    single.add_argument("template")
    single.add_argument("output", help="the new .comp")
    single.add_argument("--text", action="append", metavar="LAYER=TEXT", help="new words for a text layer (\\n for a new line)")
    single.add_argument("--image", action="append", metavar="LAYER=PATH", help="new picture for a layer, filling its box")
    single.add_argument("--focus", action="append", metavar="LAYER=X,Y", help="where to keep when cropping (0–1 each)")
    single.add_argument("--hide", action="append", metavar="LAYER", default=[])
    single.add_argument("--show", action="append", metavar="LAYER", default=[])
    single.add_argument("--render", metavar="OUT.jpg|png", help="also flatten the result")
    single.add_argument("--max-side", type=int)
    single.add_argument("--preset", help="grade image fills with this Lightroom preset (.xmp), kept tunable")
    single.add_argument("--amount", type=float, default=1.0, help="preset strength, 0–2")

    batch = commands.add_parser("batch", help="fill a template once per CSV row and render each")
    batch.add_argument("template")
    batch.add_argument("rows", help="CSV: name, text:LAYER, image:LAYER, focus:LAYER, hide:LAYER, show:LAYER columns")
    batch.add_argument("--out", required=True, help="folder for the renders")
    batch.add_argument("--format", choices=["jpg", "png"], default="jpg")
    batch.add_argument("--quality", type=float, default=0.9)
    batch.add_argument("--max-side", type=int)
    batch.add_argument("--preset", help="grade image fills with this Lightroom preset (.xmp), kept tunable")
    batch.add_argument("--amount", type=float, default=1.0, help="preset strength, 0–2")
    batch.add_argument("--keep-comp", action="store_true", help="keep each filled .comp beside its render, for touch-ups")

    args = parser.parse_args(argv)
    try:
        if args.command == "info":
            if not args.project.rstrip("/").endswith(".comp"):
                raise CompError("info reads a .comp project; for a photo use `compkit analyze photo.jpg --box WxH --preview check.jpg`")
            print(tree(Project.open(args.project)))
        elif args.command == "analyze":
            found = analyze(args.picture)
            result = {"analysis": found}
            size = (found["width"], found["height"])
            crop = None
            if args.box:
                box = tuple(float(v) for v in args.box.lower().split("x"))
                focus = tuple(map(float, args.focus.split(","))) if args.focus else auto_focus(found, size, box)
                crop = cover_crop(size, box, focus)
                result["focus"] = [round(v, 3) for v in focus]
                result["crop"] = dict(zip(("left", "top", "width", "height"), crop))
            print(json.dumps(result, indent=2))
            if args.preview:
                preview(args.picture, found, crop, args.preview)
        elif args.command == "cull":
            p = shows.profile(args.profile) if args.profile else {}
            dial = {"count": args.count} if args.count is not None else {"rate": parse_rate(args.rate)} if args.rate else \
                {"per_moment": args.per_moment, "keep": args.keep} if args.per_moment or not p else {"rate": p["rate"]}
            settings = {"profile": args.profile, "gap": args.gap or p.get("gap", 2.0), "distance": args.distance or p.get("distance", 0.6),
                        "max_moment": p.get("max_moment"), "rule": p.get("rule", shows.DEFAULT_RULE)}
            out = _Path(args.out).expanduser()
            progress = Progress("cull", folder=out / "logs", unit="frames")
            frames = cull(args.inputs, gap=settings["gap"], distance=settings["distance"], max_moment=settings["max_moment"],
                          rule=settings["rule"], cache=out / ".cache", progress=progress, **dial)
            progress.finish()
            summary = write_results(frames, out, sheets=not args.no_sheets, settings={**settings, **dial})
            if args.ratings:
                summary["ratings"] = write_ratings(frames)
            print(json.dumps(summary, indent=2))
        elif args.command == "pick":
            import time
            began = time.monotonic()
            summary = shows.redial(args.cull, rate=parse_rate(args.rate) if args.rate else None, count=args.count,
                                   per_moment=args.per_moment, sheets=not args.no_sheets)
            print(json.dumps({**summary, "seconds": round(time.monotonic() - began, 1)}, indent=2))
        elif args.command == "show":
            return show_command(args)
        elif args.command == "dashboard":
            from .dashboard import serve
            serve(args.port, args.show, open_browser=not args.no_open)
        elif args.command == "learn-look":
            print(json.dumps(learn_look(args.exports, args.raws, args.output, frames=args.frames), indent=2))
        elif args.command == "grade":
            settings = {}
            for pair in args.set:
                field, _, value = pair.partition("=")
                settings[field] = float(value)
            inputs = list(args.inputs)
            if args.list:
                inputs += [line.strip() for line in open(_Path(args.list).expanduser()) if line.strip()]
            if not inputs:
                raise CompError("grade needs photos, a --list of them, or a .comp with --layer")
            if len(inputs) == 1 and inputs[0].endswith(".comp"):
                if not args.layer:
                    raise CompError("grading a .comp needs --layer, the picture layer to grade")
                project = Project.open(inputs[0])
                project.grade_layer(project.layer(args.layer), args.preset, amount=args.amount, settings=settings or None,
                                    match=args.match)
                print("saved", project.save(args.out or None))
                return 0
            # Resumable: projects already graded with these settings are kept (see show.grade_batch).
            summary = shows.grade_batch(inputs, args.preset, args.out or ".", settings_for=lambda _: settings,
                                        match=args.match, amount=args.amount, max_side=args.max_side,
                                        render=args.render, jobs=args.jobs)
            print(json.dumps(summary, indent=2))
            return 1 if summary["failed"] else 0
        elif args.command == "measure":
            rows = {picture: measure(picture, "subject" if args.subject else "frame") for picture in args.images}
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                print(f"{'image':28} {'L* p25':>7} {'median':>7} {'p75':>7} {'p95':>7} {'blown%':>7} {'chclip%':>8} "
                      f"{'crush%':>7} {'chroma':>7}  whites L*/a*/b*      cast a*/b*")
                for picture, m in rows.items():
                    w, c, l = m["whites"], m["cast"], m["lightness"]
                    whites = f"{w['L']:5.1f} {w['a']:+5.1f} {w['b']:+5.1f}" if w else "      —          "
                    cast = f"{c['a']:+5.1f} {c['b']:+5.1f}" if c else "   —"
                    print(f"{_Path(picture).name[:28]:28} {l['p25']:7.1f} {l['median']:7.1f} {l['p75']:7.1f} {l['p95']:7.1f} "
                          f"{m['blown']:7.2f} {m['channel_clip']:8.2f} {m['crushed']:7.2f} {m['chroma']['mean']:7.1f}  {whites}    {cast}")
        elif args.command == "sheet":
            print(contact_sheet(args.images, args.output, args.columns, args.size))
        elif args.command == "where":
            print(AUTOMATION)
        elif args.command == "crop":
            out = _Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            boxes = [tuple(int(v) for v in b.lower().split("x")) for b in args.box]
            focus = tuple(map(float, args.focus.split(","))) if args.focus else "auto"
            failures = 0
            for picture in args.pictures:
                try:
                    image = load_image(picture)
                    for w, h in boxes:
                        label = f"{_Path(picture).name} at {w}×{h}"
                        if args.pad:
                            cropped = padded(image, (w, h), args.pad, label)
                        else:
                            cropped, _ = fit_image(image, (w, h), "cover", focus, max_scale=None,
                                                   analysis=lambda: analyze(picture), label=label)
                            cropped = cropped.resize((w, h), Image.LANCZOS)
                        target = out / f"{_Path(picture).stem}-{w}x{h}{'-pad' if args.pad else ''}.{args.format}"
                        if args.format == "jpg":
                            cropped.convert("RGB").save(target, quality=args.quality)
                        else:
                            cropped.save(target)
                        print(target)
                except (CompError, OSError, ValueError) as error:
                    failures += 1
                    print(f"compkit: {picture}: FAILED: {error}", file=sys.stderr)
            return 1 if failures else 0
        elif args.command == "fill":
            project = Project.open(args.template)
            focus = {name: tuple(map(float, value.split(","))) for name, value in pairs(args.focus, "--focus")}
            fill(project, pairs(args.text, "--text"), pairs(args.image, "--image"), args.hide, args.show, focus,
                 args.preset, args.amount)
            print("saved", project.save(args.output))
            if args.render:
                print("rendered", project.render(args.render, max_side=args.max_side))
        elif args.command == "batch":
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            rows_path = Path(args.rows).expanduser()
            with open(rows_path, newline="", encoding="utf-8-sig") as file:
                rows = list(csv.DictReader(file))
            failures = 0
            for number, row in enumerate(rows, start=1):
                name = (row.get("name") or f"{number:03d}").strip()
                try:
                    project = Project.open(args.template)
                    texts, images, hide, show, focus = [], [], [], [], {}
                    for column, value in row.items():
                        if column is None or value is None or ":" not in column or value.strip() == "":
                            continue
                        kind, layer = column.split(":", 1)
                        value = value.strip()
                        if kind == "text": texts.append((layer, value))
                        elif kind == "image":
                            path = Path(value).expanduser()
                            images.append((layer, path if path.is_absolute() else rows_path.parent / path))
                        elif kind == "focus": focus[layer] = tuple(map(float, value.split(",")))
                        elif kind == "hide" and value.lower() not in ("0", "no", "false"): hide.append(layer)
                        elif kind == "show" and value.lower() not in ("0", "no", "false"): show.append(layer)
                        else: raise CompError(f"unknown column {column!r}")
                    fill(project, texts, images, hide, show, focus, args.preset, args.amount)
                    project.save(out / f"{name}.comp")
                    render = project.render(out / f"{name}.{args.format}", max_side=args.max_side, quality=args.quality)
                    if not args.keep_comp:
                        import shutil
                        shutil.rmtree(project.path)
                    print(f"[{number}/{len(rows)}] {render}")
                except (CompError, OSError, ValueError) as error:
                    failures += 1
                    print(f"[{number}/{len(rows)}] {name}: FAILED: {error}", file=sys.stderr)
            return 1 if failures else 0
    except CompError as error:
        print(f"compkit: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Finished work is kept (scores, projects and exports are written whole or not at all).
        print("compkit: stopped; run the same command again to carry on where it stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
