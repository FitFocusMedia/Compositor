"""Template automation for Compositor projects.

    python -m compkit info TEMPLATE.comp
    python -m compkit fill TEMPLATE.comp OUT.comp --text Headline="New PB" --image Photo=shot.jpg --render OUT.jpg
    python -m compkit batch TEMPLATE.comp rows.csv --out renders/ [--format jpg|png] [--keep-comp] [--max-side 1080]

A template is an ordinary .comp whose replaceable layers have stable names. `fill` swaps content by layer name and
keeps everything else (placement, masks, effects, grades). In a batch CSV, `name` is each output's file name and
the other columns name what to change:

    name,text:Headline,text:Subhead,image:Photo,focus:Photo,hide:Tag
    jack-pb,New PB,Squat 220 kg,shots/jack.jpg,"0.5,0.3",
    mia-pb,Back at it,Deadlift 180 kg,shots/mia.heic,,yes
"""

import argparse
import csv
import sys
from pathlib import Path

from . import CompError, Project


def tree(project: Project) -> str:
    lines = []

    def visit(parent, depth):
        for layer in reversed(project.children(parent)):
            if layer.is_group: kind = "folder"
            elif layer.adjustment: kind = f"adjustment: {layer.adjustment['kind']}"
            elif layer.text: kind = f"text: {layer.text['content']!r} ({layer.text['fontName']} {layer.text['fontSize']:g}px)"
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


def pairs(values, flag):
    result = []
    for value in values or []:
        if "=" not in value:
            raise CompError(f"{flag} takes LAYER=VALUE, not {value!r}")
        result.append(tuple(value.split("=", 1)))
    return result


def fill(project: Project, texts=(), images=(), hide=(), show=(), focus=None) -> None:
    focus = focus or {}
    for name, value in texts:
        project.layer(name).set_text(value.replace("\\n", "\n"))
    for name, path in images:
        project.layer(name).replace_image(path, focus=focus.get(name, (0.5, 0.5)))
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

    batch = commands.add_parser("batch", help="fill a template once per CSV row and render each")
    batch.add_argument("template")
    batch.add_argument("rows", help="CSV: name, text:LAYER, image:LAYER, focus:LAYER, hide:LAYER, show:LAYER columns")
    batch.add_argument("--out", required=True, help="folder for the renders")
    batch.add_argument("--format", choices=["jpg", "png"], default="jpg")
    batch.add_argument("--quality", type=float, default=0.9)
    batch.add_argument("--max-side", type=int)
    batch.add_argument("--keep-comp", action="store_true", help="keep each filled .comp beside its render, for touch-ups")

    args = parser.parse_args(argv)
    try:
        if args.command == "info":
            print(tree(Project.open(args.project)))
        elif args.command == "fill":
            project = Project.open(args.template)
            focus = {name: tuple(map(float, value.split(","))) for name, value in pairs(args.focus, "--focus")}
            fill(project, pairs(args.text, "--text"), pairs(args.image, "--image"), args.hide, args.show, focus)
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
                    fill(project, texts, images, hide, show, focus)
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
