---
name: compositor-toolkit
description: Install, update, rebuild, test, troubleshoot or extend the Compositor automation toolkit — comp-render, compkit and the compositor-* skills — in the user's fork of the Compositor app. Use when `comp-render` or `compkit` is missing or erroring, a .comp project won't open or won't update live in the app, after pulling a new Compositor release, when asked to add a command to comp-render or a feature to compkit, or to commit/push toolkit changes to the fork.
---

# Maintaining the Compositor automation toolkit

`compkit where` prints the toolkit folder (`<repo>/automation`); the repo is its parent. If `compkit` itself is
missing, the repo is normally `~/Documents/Claude Projects/Compositor`.

```
<repo>/
├── Compositor/                 the app's Swift sources (upstream: robbietilton/Compositor)
└── automation/
    ├── comp-render/            main.swift (the CLI), Develop.swift (Lightroom presets, RAW stage, .cube looks),
    │                           Cull.swift (probe/score/previews), SparkleStub.swift, build.sh
    ├── compkit/                Python library (__init__.py), culling (cull.py) and CLI (__main__.py)
    ├── skills/                 compositor-design, -batch, -photo, -grade, -cull, -toolkit (linked into ~/.claude/skills)
    ├── tests/test_compkit.py   every case checked with the app's own loader and renderer
    ├── install.sh              build + venv + launchers + skill links
    ├── bin/ .venv/ .build/     local, git-ignored
    └── README.md
```

## Install or repair

```bash
<repo>/automation/install.sh            # idempotent: builds comp-render if stale, sets up .venv,
                                         # writes ~/.local/bin/{comp-render,compkit,compkit-python},
                                         # links each skill into ~/.claude/skills
<repo>/automation/install.sh --rebuild  # force a comp-render rebuild
```

Needs the Swift toolchain (Xcode is installed and selected) and Python 3. The build compiles every app source
except the SwiftUI entry point (Sparkle is stubbed), which takes about 2 minutes.

## After pulling a new Compositor release

Git remotes: `origin` is upstream, `fork` is the user's GitHub fork. The fork's `main` mirrors upstream; the
toolkit lives on the `automation-toolkit` branch.

```bash
git switch main && git pull origin main && git push fork main
git switch automation-toolkit && git merge main
automation/install.sh                     # rebuilds comp-render against the new app sources
automation/.venv/bin/python -m unittest discover -s automation/tests
```

If the build breaks, the app changed an API that `comp-render/main.swift` uses: read the compiler error, find
the new name in `Compositor/` and adapt main.swift. If upstream bumped `ProjectManifest.current` (the format
version), check `docs/project-format.md` for new fields compkit should write or preserve. compkit keeps
records it doesn't understand as they are.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A `.comp` won't open, or the open canvas doesn't change after a write | The app refuses invalid projects without saying why. `comp-render validate x.comp` prints the app's own reason. Writing with compkit's `save()` validates automatically. |
| Canvas doesn't update although the file is valid | The app compares content: rewriting identical bytes is ignored, and a replaced PNG of exactly the same byte size needs a manifest change too. The person's own unsaved edits make the app ask before reloading. |
| `comp-render: … not a picture macOS can read` | Format ImageIO can't open; convert first (`sips -s format png in --out out.png`). |
| Text in the wrong font | Not a PostScript name. `comp-render fonts <part of name>` lists what's installed. |
| `No module named compkit` | Run through `compkit` / `compkit-python`, or `automation/.venv/bin/python` from inside `automation/`. |
| `comp-render … is missing` | Run `install.sh`. |
| App tests: `SliderSnapTests` fails under Xcode 27 | Known: fixed upstream in PR robbietilton/Compositor#210 (the test must draw its window first). |
| A RAW develops wrongly or not at all | `comp-render probe file.ARW` shows what macOS reads; the RAW stage needs a camera macOS supports (CIRAWFilter). RAW-stage tests run only with `COMPKIT_TEST_RAW=/path/to/file.ARW` set. |
| Grades drift from Lightroom after a new camera or preset | Learn a new look: `compkit learn-look EXPORTS RAWS -o new.cube` (needs exports with embedded develop settings). |
| A preset setting is ignored | `comp-render preset x.xmp` shows what's mapped and what's skipped. To map a new Lightroom key, add it to `PresetMapping` in `comp-render/Develop.swift` (field paths are in `CameraRawFields`), rebuild and add a case to the preset test. |

App tests (only needed when touching app sources):
`xcodebuild test -project Compositor.xcodeproj -scheme Compositor -destination 'platform=macOS,arch=arm64' CODE_SIGN_IDENTITY=-`

## Extending comp-render

comp-render is the app's code, so any app feature whose core is a plain function can become a command without
reimplementing it. Look for `nonisolated enum`/`static func` APIs in `Compositor/Document` and
`Compositor/Rendering` that take a `CGImage` or settings struct and don't need the UI, as `SubjectRemoval`,
`ImageExporter`, `EditorSession.textImage` and the `FilterJob`/`PixelFilter` filters do. Then:

1. Add a `case "name":` to the `switch` in `comp-render/main.swift`, plus a usage line. Use `loadPicture()` to
   read photos and `write()`/`printJSON()` for output. Write errors with `fail()`.
2. `automation/comp-render/build.sh` (top-level code is MainActor, matching the app's default isolation).
3. Wrap it in compkit if scripts need it, add a test in `tests/test_compkit.py` that checks the result through
   comp-render, run the suite, and update `automation/README.md` and the relevant skill.

Match the surrounding style: American spelling, comments that say why, names that read as English.

## Committing

Commit toolkit work on `automation-toolkit` and push to `fork`. No git identity is configured, so pass the
user's GitHub noreply identity per commit:
`git -c user.name="FitFocusMedia" -c user.email="96030734+FitFocusMedia@users.noreply.github.com" commit …`.
Only commit or push when asked. Changes to the app itself that should go upstream get their own branch off
`main` and a PR to robbietilton/Compositor.
