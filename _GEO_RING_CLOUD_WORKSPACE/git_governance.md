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

The first commit is treated as the historical baseline. During that baseline,
legacy naming issues are warnings. After the baseline commit exists, newly added
files with ambiguous stage/step naming become errors.

## Current Enforcement Scope

- Blocks newly added ambiguous stage/step names after the baseline commit.
- Checks Geo Ring Cloud core code for references to archived or non-Geo paths.
- Warns on hard-coded `D:/AAAresearch_paper/...` paths so they can be migrated
  gradually to `path_config.py` or environment overrides.

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
