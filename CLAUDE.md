@AGENTS.md

## Automation toolkit (automation/)

- `automation/bin/comp-render` is the app's own loader/renderer/typesetter as a CLI (`render`, `validate`, `info`,
  `text`, `defaults`). Build or rebuild it with `automation/comp-render/build.sh` after changing app sources.
- `automation/compkit` is a Python library for writing `.comp` projects; run it with `automation/.venv/bin/python`.
  `python -m compkit info|fill|batch` fills named-layer templates. See automation/README.md.
- Always run `comp-render validate` on a project you wrote: the app rejects invalid files without any message.
- Xcode lives at /Applications/Xcode.app; if `xcode-select -p` still points at the Command Line Tools, prefix
  xcodebuild with `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`.
