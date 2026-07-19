# GEO-ring Cloud Workspace

Generated: `2026-07-19T08:38:41Z`

This folder is a lightweight control surface for the GEO-ring Cloud project. It intentionally does not copy large data products.

## Source of Truth

- Main code: `D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1`
- Stage1 products: `D:\AAAresearch_paper\geo_ring_cloud_stage1`
- Time-run products: `D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs`
- Evidence pack: `D:\AAAresearch_paper\geo_ring_cloud_stage1_evidence_pack`
- Index database: `D:\AAAresearch_paper\_GEO_RING_CLOUD_INDEX\geo_ring_cloud_index.sqlite`

## Files Here

- `directory_classification.md`: relevance, existence, path risk, and archive candidacy.
- `architecture.md`: authoritative module boundaries and target physical structure.
- `engineering_status.md`: generated engineering-health snapshot and prioritized debt.
- `script_inventory.md`: current GEO-ring Cloud stage scripts and non-stage components.
- `module_registry.md`: canonical Python modules, compatibility shims, public APIs, and migration evidence.
- `code_migrations.md`: physical stage-code moves, retained compatibility paths, verification, and rollback instructions.
- `pipeline_stages.md`: stage-level inputs, outputs, and evidence directories.
- `path_mapping.md`: code/data path dependencies and override strategy.
- `archive_manifest_dry_run.csv`: dry-run archive candidates generated before physical moves.
- `stage_registry.md`: canonical stage taxonomy and collision guards.
- `artifact_index.md`: compact project-memory view of directory summaries and high-value reports/manifests; query SQLite/XLSX for complete artifact rows.
- `data_product_audits.md`: horizontal index of generic and stage-scoped EO product inspections.
- `legacy_aliases.md`: legacy labels mapped to canonical stage IDs.
- `naming_policy.md`: naming rules for new work and known non-canonical labels.
- `engineering_policy.md`: enforceable engineering contract for humans and AI agents.
- Reproducible environment: `D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\environment.yml`
- Local/CI quality gate: `python _GEO_RING_CLOUD_INDEX\ci_check.py`
