---
name: earth-observation-pipeline
description: Use for Earth observation data pipelines involving CERES, DSCOVR, EPIC, GEO satellites, HDF5/NetCDF/GRIB, geolocation, projection, time-space matching, quality flags, cache directories, and reproducible remote-sensing outputs.
metadata:
  short-description: Earth observation data ingestion, projection, matching, and QC workflow
---

# Earth Observation Pipeline

Use this skill for satellite and Earth observation workflows, including CERES,
DSCOVR/EPIC, GEO imagers, HDF5, NetCDF, GRIB, projection, spatiotemporal
matching, and quality control.

## Core Principles

- Keep raw files immutable.
- Separate raw data, intermediate cache, derived products, reports, and manifests.
- Preserve sensor/platform/product/version/time metadata.
- Treat geolocation, projection, time basis, viewing geometry, and quality flags
  as essential scientific data.
- Avoid silent resampling or unit conversion.

## Data Reading

Prefer structured readers:

- NetCDF/CF: `xarray`, `netCDF4`
- HDF5: `h5py`, product-specific readers, `xarray` where supported
- GRIB: `cfgrib` when available
- Tabular metadata: `pandas`

When opening a product, inspect and record:

```text
dimensions, coordinates, variables, units, fill values, scale/offset,
quality flags, projection/geolocation variables, time encoding
```

Do not assume variable semantics from names alone; check attributes and product
documentation or existing project mappings.

## Product-Level Deep Inspection

For an unfamiliar or scientifically important satellite product, do not stop at
loading named arrays. Perform a product-structure audit before using the data:

- enumerate groups, datasets/variables, dimensions, dtypes, chunking, compression,
  attributes, global metadata, and coordinate/projection metadata
- record `_FillValue`, `missing_value`, `valid_min`, `valid_max`, `valid_range`,
  `scale_factor`, `add_offset`, `units`, `long_name`, `standard_name`, and
  product-specific code-table attributes
- inspect raw values before automatic mask/scale and after physical conversion
- preserve the raw variable, decoded variable, valid mask, and quality mask when
  the product semantics are not fully settled
- compare actual dtype/value ranges against product documentation and record any
  mismatch
- sample representative pixels/scan lines and report min/max/unique counts for
  categorical variables

For bit fields or packed quality variables, decode explicitly:

```text
raw_dtype, raw_unique_values, fill_codes, bit_numbering_convention,
field_name, start_bit, bit_count, decoded_value_counts, meaning_source
```

Do not convert bit fields or enum quality flags into a continuous quality weight
unless a product-specific, documented mapping justifies it. If the mapping is
uncertain, label the result as a diagnostic interpretation rather than a
production semantic variable.

When product-specific code already exists in the project, reuse it before
inventing a generic reader. For Geo Ring Cloud, check for existing FY4B/GEO
logic such as:

```text
D:\AAAresearch_paper\third_report\code\FY4B
D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\stage1_common.py
D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\04b_fy4b_dqf_bit_decode_diagnostics.py
D:\AAAresearch_paper\third_report\code\geo_data_audit
```

## Time Handling

- Normalize internal timestamps to UTC.
- Preserve original product time fields.
- Record time tolerance for matching.
- Distinguish observation time, file nominal time, scan time, and processing time.

## Geolocation and Projection

Before reprojection or matching, establish:

```text
source CRS or swath geometry
target grid
pixel center vs edge convention
longitude convention (-180..180 or 0..360)
valid latitude/longitude mask
viewing/solar geometry fields
```

Use proven libraries (`pyproj`, `rasterio`, `xarray`, `scipy`, product readers)
instead of hand-rolled projection math unless the project already has a verified
implementation.

## Matching Workflow

For CERES/DSCOVR/EPIC/GEO matching:

1. Select candidate files by time window.
2. Validate product version and required variables.
3. Apply quality masks before scientific comparison unless deliberately auditing
   raw semantics.
4. Reproject or collocate with explicit method and tolerance.
5. Record match counts, rejected counts, and reasons.
6. Save a manifest with inputs, parameters, outputs, and warnings.

## Quality Control

Always report:

- missing variables
- invalid geolocation
- fill values
- quality-flag exclusions
- time mismatch
- projection or interpolation failures
- sensor-specific semantic ambiguity

Do not collapse unknown/ambiguous conditions into valid clear/cloudy categories.

## Cache and Output Discipline

Use stable cache directories and avoid recomputing expensive intermediates when
the input file hash, parameters, and code version match.

For Geo Ring Cloud stage outputs, first resolve the correct canonical stage ID
from the project registry. Then use that stage ID as the prefix:

```text
<canonical_stage_id>_<purpose>_manifest.json
<canonical_stage_id>_<purpose>_matches.csv
<canonical_stage_id>_<purpose>_qc_report.md
```

For large rasters, arrays, PNGs, HDF5, or NetCDF outputs, index directory-level
summaries rather than committing them to Git.

## Verification

Before finishing:

- confirm input/output paths exist
- check shape, coordinate bounds, and valid-pixel counts
- inspect a small sample numerically
- save key tables/reports
- run project governance checks if working in a governed project repository
