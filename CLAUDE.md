@AGENTS.md

## Automation toolkit (automation/)

- `automation/bin/comp-render` is the app's own loader/renderer/typesetter/Vision code as a CLI (`render`,
  `validate`, `info`, `text`, `defaults`, `cutout`, `subject`, `fonts`). `automation/install.sh` rebuilds it when
  app sources change and installs `comp-render`, `compkit`, `compkit-python` into ~/.local/bin.
- `automation/compkit` is a Python library for writing `.comp` projects (run scripts with `compkit-python`);
  `compkit info|fill|batch|crop|analyze|where` is its CLI. See automation/README.md.
- `automation/skills/` holds the compositor-design/-batch/-photo/-grade/-cull/-toolkit Claude skills, linked into
  ~/.claude/skills by install.sh; edit them here.
- Show pipeline: `compkit cull` (comp-render probe/score/previews on embedded previews) → `compkit grade --list
  picks.txt --preset … --match look.cube` (RAW stage in comp-render/Develop.swift; looks from `compkit learn-look`).
- Always run `comp-render validate` on a project you wrote: the app rejects invalid files without any message.
- Xcode lives at /Applications/Xcode.app; if `xcode-select -p` still points at the Command Line Tools, prefix
  xcodebuild with `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`.
