# Stage 06f Data Asset Audit

Canonical implementation directory for `geo_ring_cloud.stage_06f`.

## Entrypoints

- `stage_06f_unknown_aware_data_asset_audit.py`: full data-asset scan and export.
- `stage_06f_reexport_with_obitype_patch.py`: re-export an existing audit database after semantic correction.
- `stage_06f_report_sync.py`: rebuild the human-readable report from audited tables.

Run canonical modules from the core-code root with `python -m`, for example:

```powershell
python -m stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit --help
```

The historical top-level paths remain compatibility entrypoints and delegate to these modules.
