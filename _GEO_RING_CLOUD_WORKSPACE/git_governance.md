# Git Governance

This repository uses Git for the lightweight, reviewable parts of the Geo Ring
Cloud project: code, governance scripts, taxonomy/index builders, and workspace
documentation.

Large research artifacts stay outside Git by default. This includes raw data,
time runs, evidence packs, generated SQLite/XLSX indexes, PowerPoint files, and
the non-Geo archive.

## Remote

```text
origin = https://github.com/Leihoulea/GEO_ring_cloud.git
```

## Pre-Commit Check

The repository hook path is configured as:

```text
.githooks
```

Before each commit, Git runs:

```text
python _GEO_RING_CLOUD_INDEX/governance_check.py --staged
```

Before accepting a commit message, Git runs:

```text
python _GEO_RING_CLOUD_INDEX/governance_check.py --commit-msg <message-file>
```

The first commit is treated as the historical baseline. During that baseline,
legacy naming issues are warnings. After the baseline commit exists, newly added
files with ambiguous stage/step naming become errors.

## Current Enforcement Scope

- Blocks newly added ambiguous stage/step names after the baseline commit.
- Blocks newly added stage-owned core scripts whose path and `STAGE_ID` /
  `PROJECT_STAGE_ID` disagree.
- Blocks newly added non-stage core utilities unless their filename starts with
  `geo_ring_cloud_`.
- Checks Geo Ring Cloud core code for references to archived or non-Geo paths.
- Blocks newly staged generated or large artifacts such as SQLite/XLSX, PPTX,
  NPZ, NetCDF/HDF/HDF5, PDF, and image files.
- Blocks commit messages that do not name a canonical stage ID or project
  component role.
- Warns on historical hard-coded `D:/AAAresearch_paper/...` paths so they can
  be migrated gradually to `path_config.py` or environment overrides.

## Naming Direction

New stage-owned files should use canonical stage IDs:

```text
stage_09d_full_pixel_diagnostics_report.md
stage_09d_summary.csv
```

Avoid new project-level names such as:

```text
step9_report.md
Stage09D_result.csv
09_stage09_notes.md
```

## Strict Audit

Normal checks use gradual enforcement: new violations are errors and historical
debt remains warnings. For manual audits, run:

```text
python _GEO_RING_CLOUD_INDEX/governance_check.py --all --strict
```
