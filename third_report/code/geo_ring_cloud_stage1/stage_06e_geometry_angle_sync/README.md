# Stage 06e Geometry Angle Sync

Canonical implementation directory for `geo_ring_cloud.stage_06e`.

## Entrypoints

- `stage_06e_full_geometry_angle_source_sync.py`: synchronize geometry-angle sources and rerun dependent fusion diagnostics.
- `stage_06e_vza_ecef_final_audit.py`: compare current VZA layers with audited ECEF geometry.

Run the canonical modules from the core-code root:

```powershell
python -m stage_06e_geometry_angle_sync.stage_06e_full_geometry_angle_source_sync
python -m stage_06e_geometry_angle_sync.stage_06e_vza_ecef_final_audit
```

The historical top-level paths remain thin compatibility entrypoints. The canonical synchronization module resolves sibling stage commands through `geo_ring_cloud.paths.CODE_ROOT`, so moving this package does not change subprocess lookup behavior.
