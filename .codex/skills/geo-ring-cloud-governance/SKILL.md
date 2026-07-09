---
name: geo-ring-cloud-governance
description: Use when working in D:\AAAresearch_paper on the Geo Ring Cloud project, especially before adding code, reports, artifacts, or commits. Guides Codex to query the project memory index, reuse existing stage scripts/results, follow canonical stage naming, run governance checks, and commit cleanly.
metadata:
  short-description: Geo Ring Cloud project memory and governance workflow
---

# Geo Ring Cloud Governance

Use this skill for any task in `D:\AAAresearch_paper` that touches Geo Ring
Cloud code, reports, artifacts, stage naming, indexing, Git commits, or project
organization.

## Operating Rule

Do not start by writing new code. First identify whether the project already has
usable code, reports, artifacts, or stage definitions.

## Canonical Entry Points

Read these first, in this order, only as much as needed:

1. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\README.md`
2. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\stage_registry.md`
3. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\artifact_index.md`
4. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\naming_policy.md`
5. `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE\git_governance.md`

The canonical SQLite memory database is:

```text
D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX\geo_ring_cloud_index.sqlite
```

Prefer precise SQLite queries over broad filesystem scans.

## Search Order

1. Check `stage_registry` for the canonical stage.
2. Check `artifact_index` for existing reports, CSV/JSON/XLSX, scripts, and key directories.
3. Check `scripts` for reusable stage scripts or component-role utilities.
4. Use `rg` only in focused directories:
   - `D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1`
   - `D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX`
   - `D:\AAAresearch_paper\_GEO_RING_CLOUD_WORKSPACE`

Do not scan raw data, time-run outputs, evidence packs, or `_NON_GEO_ARCHIVE`
unless the task specifically requires it.

## Stage Identity

Use `project_id + canonical_stage_id`; never globally merge similar labels.

Examples:

```text
geo_ring_cloud.stage_09
geo_ring_cloud.stage_09d
epic_ceres.stage_09
```

`geo_ring_cloud.stage_09` is not the same thing as `epic_ceres.stage_09`.

Historical labels such as `Step9`, `Stage09`, `stage09`, and `09_stage09` are
legacy aliases or naming violations unless the registry explicitly maps them.

## Naming Rules

New stage-owned files must use canonical stage IDs:

```text
stage_09d_visible_filter_audit.py
stage_09d_visible_filter_summary.csv
stage_09d_visible_filter_report.md
stage_09d_visible_filter_manifest.json
```

Avoid new names such as:

```text
step9_report.md
Stage09D_result.csv
09_stage09_notes.md
```

Shared utilities should use a project/component name, not a fake stage:

```text
geo_ring_cloud_path_audit.py
geo_ring_cloud_artifact_lineage.py
```

## Path Rules

Geo Ring Cloud code should use `path_config.py` or environment-variable
overrides for project paths. Do not add new hard-coded absolute paths unless
there is a clear, documented reason.

Do not make Geo Ring Cloud core code depend on `_NON_GEO_ARCHIVE`.

## Commit Workflow

Before commit:

```powershell
python D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX\governance_check.py --staged
```

Commit messages should name the stage or component and the purpose:

```text
Add stage_09d visible-filter diagnostics
Update geo_ring_cloud artifact index governance
```

If the check reports historical warnings only, do not rewrite history just to
silence them. Fix new violations introduced by the current task.
