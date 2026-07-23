# Stage 09H Gate 4B Production Navigation Patch Report

- Generated UTC: `2026-07-23T17:34:52Z`
- Cases: `20240312_1500`
- Final status: `PRODUCTION_NAVIGATION_PATCH_VALIDATED`

## Core Rule

- `cloud_mask` remains identity relative to legacy raw cfgrib values.
- `latitude/longitude` are replaced only for audited Meteosat-0deg CLM MSG3 3712x3712 files.
- EPIC comparison is downstream recovery evidence; native navigation truth is anchored to Gate 4A SEVIRI area controls.

## Gate Statuses

| status_name | status | detail |
| --- | --- | --- |
| MASK_PRESERVATION | PASS | patched cloud_mask equals legacy raw cfgrib values byte-for-byte |
| SCOPE_GUARD | PASS | patch applies only to audited Meteosat-0deg CLM files |
| NATIVE_NAVIGATION_REFERENCE | PASS | patched control points match frozen Gate 3B/4A references; legacy remains a negative control |
| LEGACY_NAV_NEGATIVE_TEST | PASS | legacy cfgrib identity navigation is still grossly inconsistent with raw-storage reference |
| DETERMINISTIC_OUTPUT | PASS | grid spec hash is stable and embedded |
| CACHE_INVALIDATION | PASS | new schema version meteosat_0deg_clm_v2 is present for cache keys |
| EPIC_DOWNSTREAM_RECOVERY | PASS | patched production EPIC metrics reproduce Gate 4A official-area baseline |

## Gate 4A Reproduction

| case_id | patched_agreement | gate4a_agreement | agreement_abs_delta | patched_mcc | gate4a_mcc | mcc_abs_delta | agreement_tolerance | mcc_tolerance | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20240312_1500 | 0.801650 | 0.801647 | 0.000003 | 0.596370 | 0.596364 | 0.000006 | 0.000010 | 0.000010 | PASS |
