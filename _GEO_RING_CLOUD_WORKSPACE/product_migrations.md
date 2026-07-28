# GEO-ring Cloud Product Migrations

This log records physical moves of Geo Ring Cloud outputs and historical source
snapshots. It does not redefine canonical stage identity.

## 2026-07-28

| move_id | source | destination | files | bytes | reference audit | compatibility |
| --- | --- | --- | ---: | ---: | --- | --- |
| product-20260728-01 | `stage_09i_remaining_cloud_reader_audit` | `geo_ring_cloud_stage1_time_runs/stage_09i_remaining_cloud_reader_audit` | 9 | 172003 | The only active code consumer was Stage 09j; its baseline path now uses `RUNS_ROOT` | The historical Stage 09i manifest retains old relative output strings; resolve them through this migration row |
| product-20260728-02 | `geo_ring_cloud_stage1/scripts` | `geo_ring_cloud_stage1_evidence_pack/source_snapshots` | 18 | 526346 | No active code path reads the snapshot directory; writers use the `SCRIPT_DIR` compatibility alias | `SCRIPT_DIR` now resolves to the evidence-pack snapshot directory |

Migration timestamp: `2026-07-28T02:38:29Z`.

The Stage 09i record is an output-only legacy audit. Its manifest states that
the work was performed inline, so there is no recoverable generating script.
Stage 09j may use its baseline CSV, but the Stage 09i run is not fully
reproducible from Git.
