# GEO-ring Cloud Engineering Policy

This document is the enforceable engineering contract for Geo Ring Cloud work.
It applies to humans and AI agents.

## Required workflow

- MUST check `architecture.md`, `engineering_status.md`, `module_registry.md`, `code_migrations.md`, `stage_registry.md`, `artifact_index.md`, `data_product_audits.md`, and the SQLite index before creating new code or reports.
- MUST reuse existing scripts, manifests, reports, and products when they already answer the task.
- MUST decide the `project_id + canonical_stage_id` before naming files.
- MUST run `python _GEO_RING_CLOUD_INDEX\build_index.py` after adding or changing stage scripts.
- MUST commit `index_build_manifest.json`; its governed-source fingerprint must match the submitted source tree.
- Existing-stage refactors MUST stage refreshed `artifact_index.md` when artifact semantics change; otherwise refreshed `engineering_status.md` is acceptable. New stages MUST stage the full stage/artifact/audit index set.
- MUST run `python _GEO_RING_CLOUD_INDEX\governance_check.py --staged` before commit.
- MUST use the checked-in `environment.yml` as the default scientific dependency baseline and run `python _GEO_RING_CLOUD_INDEX\ci_check.py --scientific-tests` for core-code changes.
- MUST run long-lived downloads, uploads, and scientific experiments from a clean commit or a dedicated worktree. Active run code MUST NOT be edited in place.
- MUST preserve the exact generating-script hash and dirty Git state when an exceptional exploratory run cannot use a clean commit; never claim that repository HEAD represents that script.

## Naming and identity

- MUST use canonical stage IDs for new stage-owned files, such as `stage_10p2_approx_fov_report.md`.
- MUST NOT create new `Step*`, `stage10*`, `Stage10*`, or `10_stage*` names.
- MUST use `geo_ring_cloud_<role>_<purpose>.py` for new non-stage core utilities.
- MUST place reusable shared APIs in the `geo_ring_cloud` package and import them through their canonical module names.
- Package adapters and diagnostics MUST NOT import or dynamically load stage scripts; dependencies flow from stages to shared APIs.
- Stage scripts MUST NOT dynamically load one another to reuse implementation; extract shared logic into a registered `geo_ring_cloud.*` module and use a normal import. Registered historical loaders are migration warnings only.
- `geo_ring_cloud.pipeline_support` is a transitional compatibility facade. It MUST contain only imports, export metadata, and aliases; active stage/component code MUST NOT import it, and new shared responsibilities MUST use focused package modules.
- Staged code MUST NOT import registered top-level compatibility shims; use canonical `geo_ring_cloud.*` modules.
- Only the dedicated compatibility boundary test may import legacy shims, through the governance allowlist.
- MUST NOT add implementation logic to top-level compatibility shims recorded in `module_registry.md`.
- Historical stage paths may remain only as registered `compatibility_entrypoint` files: import the expected canonical stage module, declare the matching `STAGE_ID`, and contain no scientific or orchestration implementation.
- MUST NOT treat `geo_ring_cloud.stage_09` and `epic_ceres.stage_09` as the same stage.

## Output lineage

- New stage outputs MUST use `geo_ring_cloud.lineage.write_manifest` and include `project_id`, `canonical_stage_id`, generating script, inputs, outputs, parameters, timestamp, and commit when available.
- `code_commit` identifies repository HEAD at manifest-write time; it is not proof that HEAD contains the executed script. The manifest MUST also record the script SHA-256, Git state, worktree/commit blobs, and `commit_represents_script`.
- A manifest with `commit_represents_script=false` remains usable evidence only when its lineage warning is retained and the exact script content is preserved separately.
- Non-stage run manifests MUST include `component_role` and `related_stage_ids`; they MUST NOT place a component label in `canonical_stage_id`.
- Reports SHOULD be Chinese-first, with English retained for technical terms and variable names.
- Key outputs SHOULD include concise CSV/Markdown indexes instead of relying only on directory names.
- Generic data/product inspections SHOULD be indexed in `data_product_audits.md`; stage-scoped inspections should keep `related_stage_ids`.
- Long-lived component status and run manifests MUST record `code_commit_scope`, `generating_script_state`, and lineage warnings using the same semantics as `geo_ring_cloud.lineage`.

## Path and artifact rules

- Python code MUST use `geo_ring_cloud.paths`; PowerShell orchestration MUST dot-source `geo_ring_cloud_path_configuration.ps1` or use the same `GEO_RING_*` environment-variable contract.
- Active project code MUST NOT hard-code any machine-local drive path unless it is one of the two explicitly allowlisted canonical path-configuration files.
- Core code MUST NOT depend on `_NON_GEO_ARCHIVE`, `second_report`, `forth`, or EPIC-CERES code/output paths.
- New stage code MUST live below `third_report/code/geo_ring_cloud_stage1` and new stage outputs below `geo_ring_cloud_stage1_time_runs/<canonical-stage-run>`. Stage-owned directories at repository root are forbidden.
- `geo_ring_cloud_stage1/reports` is a frozen legacy shared report pool. New stages MUST NOT write there; use a stage-specific directory under `RUNS_ROOT`.
- Historical source snapshots belong under `geo_ring_cloud_stage1_evidence_pack/source_snapshots`; new code MUST NOT recreate `geo_ring_cloud_stage1/scripts`.
- Raw data, time runs, evidence packs, SQLite/XLSX indexes, PPTX, images, NetCDF/HDF/HDF5, NPZ, and other large generated artifacts MUST stay out of Git by default.
- GitHub CI MUST remain independent of local large-data paths; real-data integration tests are explicit local checks.

## Enforcement levels

- New violations are errors in the staged governance check.
- Historical naming and path debt remains warnings unless `--strict` is used.
- Historical warnings should be cleaned in dedicated cleanup work, not opportunistically mixed into scientific changes.
