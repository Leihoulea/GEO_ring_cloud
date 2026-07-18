# GEO-ring Cloud Naming Policy

## Canonical identifiers

- Use `project_id + canonical_stage_id` for every stage decision.
- Main project namespace: `geo_ring_cloud`.
- Stage IDs use lowercase ASCII: `stage_00`, `stage_03_5`, `stage_06c`, `stage_07p_b`, `stage_07v2`, `stage_09d`, `stage_10p2`.
- Do not use `Step` for project-level phases. `Step` may only describe an internal procedure inside a script or report.

## New file and directory names

- Prefix new stage-owned files with the canonical stage ID, for example `stage_09d_full_pixel_diagnostics_report.md`.
- New stage-owned directories must also use the canonical stage ID, for example `stage_10p2_approx_fov_aggregation`.
- Put substep numbers after the stage directory or in report sections, for example `stage_09d/00_sample_manifest`.
- Reusable shared APIs belong in the `geo_ring_cloud` package, use lowercase `snake_case.py`, declare `COMPONENT_ROLE`, and must be registered in `module_registry.md`.
- Executable non-stage utilities at the code root must use `geo_ring_cloud_<role>_<purpose>.py`, declare `COMPONENT_ROLE`, and avoid fake or combined stages. Roles include `runner`, `experiment_runner`, `downloader`, `evidence_pack_builder`, `summary_helper`, and `presentation_builder`.
- Top-level compatibility shims may retain historical names only when registered; they must contain imports and metadata, not implementation logic.
- Generic EO data/product inspections must use `component_role=data_product_audit`; keep legacy `third_report/code/geo_data_audit` paths until references are audited, and index them in `data_product_audits.md`.
- Do not create new `Step*`, `stage10*`, `Stage10*`, `09_stage*`, or numeric-prefix stage names.

## Collision rules

- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09`.
- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09_5`.
- `research_tracker` labels such as `Step9`, `BuildStep9`, and `Stage9` are untrusted legacy inference labels until reviewed in `stage_registry`.

## Migration rule

Historical files are not renamed by default. Rename only after code references, evidence references, and a rollback manifest are checked.

## Enforcement rule

- Newly added non-canonical stage names are errors.
- Existing historical names remain warnings during normal checks.
- Use `python _GEO_RING_CLOUD_INDEX\governance_check.py --all --strict` for strict audit mode.
