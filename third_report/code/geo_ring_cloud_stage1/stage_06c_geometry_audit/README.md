# Stage 06c Geometry Audit

Canonical implementation directory for `geo_ring_cloud.stage_06c`.

## Entrypoints

- `stage_06c_geometry_parameter_audit.py`: baseline satellite geometry and VZA method audit.
- `stage_06c_multi_satellite_geometry_metadata_audit.py`: authoritative multi-satellite metadata and reader audit.
- `stage_06c_claas3_geometry_angle_lineage.py`: CLAAS-3 CF projection and derived-angle lineage gate.

Run canonical modules from the core-code root with `python -m`, for example:

```powershell
python -m stage_06c_geometry_audit.stage_06c_claas3_geometry_angle_lineage --help
```

Historical top-level paths remain governed compatibility entrypoints. Shared configuration, lineage, and source registry APIs are imported only from the `geo_ring_cloud` package.
