# GEO-ring Cloud Naming Policy

## Canonical identifiers

- Use `project_id + canonical_stage_id` for every stage decision.
- Main project namespace: `geo_ring_cloud`.
- Stage IDs use lowercase ASCII: `stage_00`, `stage_03_5`, `stage_06c`, `stage_07p_b`, `stage_07v2`, `stage_09d`.
- Do not use `Step` for project-level phases. `Step` may only describe an internal procedure inside a script or report.

## New file and directory names

- Prefix new stage-owned files with the canonical stage ID, for example `stage_09d_full_pixel_diagnostics_report.md`.
- Put substep numbers after the stage directory or in report sections, for example `stage_09d/00_sample_manifest`.
- Shared utilities should use `component_role`, not a fake stage: `shared_library`, `runner`, `downloader`, `evidence_pack_builder`, `summary_helper`.

## Collision rules

- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09`.
- `geo_ring_cloud.stage_09` is not `epic_ceres.stage_09_5`.
- `research_tracker` labels such as `Step9`, `BuildStep9`, and `Stage9` are untrusted legacy inference labels until reviewed in `stage_registry`.

## Migration rule

Historical files are not renamed by default. Rename only after code references, evidence references, and a rollback manifest are checked.
