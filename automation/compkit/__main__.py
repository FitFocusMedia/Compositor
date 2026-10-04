"""Template automation for Compositor projects.

    python -m compkit info TEMPLATE.comp
    python -m compkit analyze PHOTO [--box 1080x1350] [--preview crop.jpg]
    python -m compkit crop PHOTO... --box 1080x1350 [--box 1080x1920] --out crops/
    python -m compkit grade PHOTO... --preset base.xmp --out graded/ [--max-side 2048] [--render]
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

from PIL import Image
import sys
from pathlib import Path

from pathlib import Path as _Path

from . import AUTOMATION, CompError, Project, analyze, auto_focus, cover_crop, fit_image, load_image, measure


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

    grade = commands.add_parser("grade", help="apply a Lightroom preset as a base grade, kept tunable")
    grade.add_argument("inputs", nargs="+", help="photos, or one .comp with --layer")
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
        elif args.command == "grade":
            settings = {}
            for pair in args.set:
                field, _, value = pair.partition("=")
                settings[field] = float(value)
            if len(args.inputs) == 1 and args.inputs[0].endswith(".comp"):
                if not args.layer:
                    raise CompError("grading a .comp needs --layer, the picture layer to grade")
                project = Project.open(args.inputs[0])
                project.grade_layer(project.layer(args.layer), args.preset, amount=args.amount, settings=settings or None)
                print("saved", project.save(args.out or None))
                return 0
            out = _Path(args.out or ".").expanduser()
            out.mkdir(parents=True, exist_ok=True)
            failures = 0
            for picture in args.inputs:
                try:
                    found = analyze(picture)
                    w, h = found["width"], found["height"]
                    scale = min(1, args.max_side / max(w, h))
                    project = Project.new(max(1, round(w * scale)), max(1, round(h * scale)))
                    project.add_graded_photo(picture, args.preset, name="Photo", amount=args.amount,
                                             settings=settings or None, max_scale=1)
                    saved = project.save(out / f"{_Path(picture).stem}.comp")
                    print(saved)
                    if args.render:
                        print(project.render(saved.with_suffix(".jpg"), quality=0.92))
                except (CompError, OSError, ValueError, KeyError) as error:
                    failures += 1
                    print(f"compkit: {picture}: FAILED: {error}", file=sys.stderr)
            return 1 if failures else 0
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
