---
name: geo-ring-cloud-governance
description: Enforce Geo Ring Cloud project governance in D:\AAAresearch_paper. Use before adding, editing, moving, naming, indexing, committing, or reviewing Geo Ring Cloud code, scripts, reports, manifests, artifacts, stage taxonomy, paths, or Git changes. Requires querying the project memory index, reusing existing work, following canonical stage IDs, writing run/artifact lineage, and passing governance checks.
---

# Geo Ring Cloud Governance

Use this skill for any Geo Ring Cloud work in `D:\AAAresearch_paper`.
Treat it as an engineering contract, not guidance.

## Required Order

MUST do these steps before writing or moving files:

1. Read `_GEO_RING_CLOUD_WORKSPACE/README.md`.
2. Read `_GEO_RING_CLOUD_WORKSPACE/engineering_policy.md`.
3. Read `_GEO_RING_CLOUD_WORKSPACE/architecture.md` and `_GEO_RING_CLOUD_WORKSPACE/engineering_status.md`.
4. Check `_GEO_RING_CLOUD_WORKSPACE/module_registry.md` before creating or duplicating shared code.
5. Check `_GEO_RING_CLOUD_WORKSPACE/stage_registry.md`.
6. Check `_GEO_RING_CLOUD_WORKSPACE/artifact_index.md`.
7. Check `_GEO_RING_CLOUD_WORKSPACE/data_product_audits.md` for generic and stage-scoped EO product inspections.
8. Query `_GEO_RING_CLOUD_INDEX/geo_ring_cloud_index.sqlite` when a precise lookup is cheaper than broad file search.
9. Search focused code paths with `rg` only after the index/workspace checks.

MUST NOT scan raw data, time-run outputs, evidence packs, or `_NON_GEO_ARCHIVE`
unless the task explicitly requires those artifacts.

## Stage Identity

MUST use `project_id + canonical_stage_id`.

Valid examples:

```text
geo_ring_cloud.stage_09d
geo_ring_cloud.stage_10
geo_ring_cloud.stage_10p2
epic_ceres.stage_09
```

MUST NOT globally merge labels such as `Step9`, `Stage09`, `stage09`, and
`09_stage09`. Treat them as legacy aliases or violations unless the registry
explicitly maps them.

## Naming Contract

MUST name new stage-owned files and directories with canonical stage IDs:

```text
stage_10p2_approx_fov_report.md
stage_10p2_approx_fov_manifest.json
stage_10p2_approx_fov_matches.csv
```

MUST NOT create new names such as:

```text
step10_report.md
stage10p2_result.csv
10_stage10_notes.md
```

New non-stage utilities in Geo Ring Cloud core code MUST use:

```text
geo_ring_cloud_<role>_<purpose>.py
```

They MUST declare `COMPONENT_ROLE`. Cross-stage manifests MUST leave
`canonical_stage_id` empty and record both `component_role` and
`related_stage_ids`; never invent a combined stage such as `stage0910`.

Do not invent fake stages for runners, downloaders, evidence-pack builders,
summaries, or shared helpers. Use `component_role` for those.

Reusable shared APIs MUST live in the `geo_ring_cloud` package, use lowercase
`snake_case.py`, declare `COMPONENT_ROLE`, and be registered in
`module_registry.md`. New code MUST import canonical package modules. Top-level
legacy modules registered as compatibility shims MUST contain no implementation
logic.

Package adapters and diagnostics MUST NOT import or dynamically load stage
scripts. Dependency direction is one-way: stage scripts may call shared package
APIs; shared package APIs may depend only on lower-level package modules and
declared third-party libraries.

`geo_ring_cloud.pipeline_support` is a registered transitional facade for
legacy Stage 1 APIs. It MUST contain only imports, export metadata, and aliases;
MUST NOT contain functions, classes, or implementation logic. Active stage and
component code MUST NOT import it. New layout, semantics, adapter, diagnostic,
visualization, or artifact behavior MUST go in a focused canonical package
module and be added to `module_registry.md`.
Staged code MUST NOT import registered top-level compatibility shims such as
`stage1_common`, `path_config`, or `geo_ring_cloud_source_registry`; import the
canonical `geo_ring_cloud.*` module instead.
Only the dedicated compatibility boundary test may import legacy shims, via
the explicit governance allowlist; do not add broad directory exemptions.

Generic data/product inspections SHOULD use `component_role=data_product_audit`.
If an inspection supports a downstream stage, keep the generic audit role and
record the linked stage in `related_stage_ids` / `canonical_stage_id`.

## Output Contract

For new stage outputs, MUST create a run or artifact manifest containing at
minimum:

- `project_id`
- `canonical_stage_id`
- generating script path
- input paths or artifact IDs
- output paths
- parameter summary
- timestamp
- code commit when available

MUST produce human-readable reports primarily in Chinese, with English retained
for technical names such as `CPD`, `COT`, `CTH`, `PSF`, and variable names.

MUST NOT commit raw data, time-run products, generated SQLite/XLSX, PowerPoint
files, quicklook images, NetCDF/HDF/HDF5, NPZ, or other large generated
artifacts unless a narrow allowlist is explicitly added.

## Path Contract

Geo Ring Cloud core code MUST use `path_config.py` or environment-variable
overrides for project paths.

MUST NOT add new hard-coded `D:\AAAresearch_paper\...` paths outside the
allowlist.

MUST NOT make Geo Ring Cloud core code depend on `_NON_GEO_ARCHIVE`,
`second_report`, `forth`, `third_report/code/epic_ceres`, or
`third_report/outputs/epic_ceres*`.

## Index And Git Contract

For core-code changes, MUST use the checked-in dependency baseline and run:

```powershell
python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests
```

Use `--integration-tests` only when the configured local real-data roots are
available. GitHub CI MUST remain independent of local large-data paths.

After adding or changing stage scripts, MUST run:

```powershell
python _GEO_RING_CLOUD_INDEX\build_index.py
```

MUST stage updated workspace Markdown when stage code changes, especially:

```text
_GEO_RING_CLOUD_WORKSPACE/stage_registry.md
_GEO_RING_CLOUD_WORKSPACE/artifact_index.md
```

Before committing, MUST run:

```powershell
python _GEO_RING_CLOUD_INDEX\governance_check.py --staged
```

Commit messages MUST include a canonical stage ID or component role, for
example:

```text
Add stage_10p2 FOV aggregation diagnostics
Update geo_ring_cloud governance enforcement
```

Historical warnings are not a reason to rewrite legacy files. Fix only the
new violations introduced by the current task unless the user asks for a
dedicated cleanup.
