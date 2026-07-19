# GEO-ring Cloud Engineering Policy

This document is the enforceable engineering contract for Geo Ring Cloud work.
It applies to humans and AI agents.

## Required workflow

- MUST check `architecture.md`, `engineering_status.md`, `module_registry.md`, `stage_registry.md`, `artifact_index.md`, `data_product_audits.md`, and the SQLite index before creating new code or reports.
- MUST reuse existing scripts, manifests, reports, and products when they already answer the task.
- MUST decide the `project_id + canonical_stage_id` before naming files.
- MUST run `python _GEO_RING_CLOUD_INDEX\build_index.py` after adding or changing stage scripts.
- MUST run `python _GEO_RING_CLOUD_INDEX\governance_check.py --staged` before commit.
- MUST use the checked-in `environment.yml` as the default scientific dependency baseline and run `python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests` for core-code changes.

## Naming and identity

- MUST use canonical stage IDs for new stage-owned files, such as `stage_10p2_approx_fov_report.md`.
- MUST NOT create new `Step*`, `stage10*`, `Stage10*`, or `10_stage*` names.
- MUST use `geo_ring_cloud_<role>_<purpose>.py` for new non-stage core utilities.
- MUST place reusable shared APIs in the `geo_ring_cloud` package and import them through their canonical module names.
- Package adapters and diagnostics MUST NOT import or dynamically load stage scripts; dependencies flow from stages to shared APIs.
- `geo_ring_cloud.pipeline_support` is a transitional compatibility facade. It MUST contain only imports, export metadata, and aliases; active stage/component code MUST NOT import it, and new shared responsibilities MUST use focused package modules.
- Staged code MUST NOT import registered top-level compatibility shims; use canonical `geo_ring_cloud.*` modules.
- Only the dedicated compatibility boundary test may import legacy shims, through the governance allowlist.
- MUST NOT add implementation logic to top-level compatibility shims recorded in `module_registry.md`.
- MUST NOT treat `geo_ring_cloud.stage_09` and `epic_ceres.stage_09` as the same stage.

## Output lineage

- New stage outputs MUST include a manifest with `project_id`, `canonical_stage_id`, generating script, inputs, outputs, parameters, timestamp, and commit when available.
- Non-stage run manifests MUST include `component_role` and `related_stage_ids`; they MUST NOT place a component label in `canonical_stage_id`.
- Reports SHOULD be Chinese-first, with English retained for technical terms and variable names.
- Key outputs SHOULD include concise CSV/Markdown indexes instead of relying only on directory names.
- Generic data/product inspections SHOULD be indexed in `data_product_audits.md`; stage-scoped inspections should keep `related_stage_ids`.

## Path and artifact rules

- Core code MUST use `geo_ring_cloud.paths` or environment-variable overrides for project paths; `path_config.py` is legacy-import compatibility only.
- New core code MUST NOT hard-code `D:\AAAresearch_paper\...` unless explicitly allowlisted.
- Core code MUST NOT depend on `_NON_GEO_ARCHIVE`, `second_report`, `forth`, or EPIC-CERES code/output paths.
- Raw data, time runs, evidence packs, SQLite/XLSX indexes, PPTX, images, NetCDF/HDF/HDF5, NPZ, and other large generated artifacts MUST stay out of Git by default.
- GitHub CI MUST remain independent of local large-data paths; real-data integration tests are explicit local checks.

## Enforcement levels

- New violations are errors in the staged governance check.
- Historical naming and path debt remains warnings unless `--strict` is used.
- Historical warnings should be cleaned in dedicated cleanup work, not opportunistically mixed into scientific changes.
