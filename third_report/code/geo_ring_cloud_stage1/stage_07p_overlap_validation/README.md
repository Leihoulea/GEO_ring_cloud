# Stage 07p Overlap Validation

Canonical implementation directory for `geo_ring_cloud.stage_07p`.

## Entrypoints

- `stage_07p_overlap_validator.py`: repaired overlap validation, source-boundary metrics, and angle-layer stratification.
- `stage_07p_claas3_profile_pair_evaluation.py`: operational Meteosat versus CLAAS-3 common-domain diagnostics.

Run canonical modules from the core-code root, for example:

```powershell
python -m stage_07p_overlap_validation.stage_07p_claas3_profile_pair_evaluation --help
```

`geo_ring_cloud.stage_07p_b` remains a separate stage and is not part of this package. Historical Stage 07p top-level paths remain governed compatibility entrypoints.
