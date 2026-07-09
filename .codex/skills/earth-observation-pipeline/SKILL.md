---
name: earth-observation-pipeline
description: Use when inspecting unfamiliar or newly acquired Earth observation products such as CERES, DSCOVR, EPIC, GEO satellite files, HDF5, NetCDF, GRIB, or tabular metadata. Guides Codex to inventory internal structure, variables, attributes, units, coordinates, time fields, quality flags, bit fields, masks, and raw value distributions before writing analysis code.
metadata:
  short-description: New Earth observation product structure and variable inspection
---

# Earth Observation Product Inspection

Use this skill when a task involves reading or understanding a new satellite or
Earth observation data product. The goal is not to build the full downstream
science pipeline first; the goal is to inspect the product deeply enough that
later processing does not rest on guessed variable semantics.

## Core Principles

- Keep raw files immutable.
- Inspect before interpreting. Do not assume semantics from file names or common
  variable names alone.
- Prefer product-structure evidence over generic assumptions.
- Preserve sensor/platform/product/version/time metadata.
- Treat geolocation, projection, time basis, masks, quality flags, packed bits,
  units, fill values, and scale/offset as essential data.
- Reuse existing project readers or audits when available, but verify that their
  assumptions still match the new product.

## Reader Choice

Prefer structured readers:

- NetCDF/CF: `xarray`, `netCDF4`
- HDF5: `h5py`, product-specific readers, `xarray` where supported
- GRIB: `cfgrib` when available
- tabular metadata: `pandas`
- XML/manifest/package metadata: structured XML/ZIP readers where applicable

If multiple readers are possible, compare their reported dimensions, variables,
attributes, mask/scale behavior, and decoded values before choosing one.

## Required Inspection

For each representative file, enumerate and record:

- file path, size, modified time, product/platform hints, and parsed acquisition
  time when available
- groups, subgroups, datasets/variables, dimensions, coordinates, shape, dtype,
  chunking, compression, and storage layout
- global and variable attributes
- `_FillValue`, `missing_value`, `valid_min`, `valid_max`, `valid_range`,
  `scale_factor`, `add_offset`, `units`, `long_name`, `standard_name`,
  `coordinates`, `grid_mapping`, and product-specific code-table attributes
- raw values before automatic mask/scale and values after physical conversion
- sample statistics: valid count, missing/fill count, min, max, mean where
  meaningful, unique counts for categorical variables, and representative
  sample values

Variable discovery must be inclusive. Search for variables including but not limited to:

```text
latitude, longitude, x/y coordinates, scan angle, projection, grid mapping,
time, start/end/nominal/scan time, solar/view/sensor angles, azimuth,
cloud mask, cloud phase, cloud type, cloud top height/temperature/pressure,
radiance, reflectance, brightness temperature, calibration, quality flag,
DQF/QA, confidence, algorithm status, processing flag, valid mask,
land/water/snow/ice/day/night/terminator masks, fill/missing/off-disk masks
```

Also search attribute text for non-standard naming. A variable with an unusual
name may still be a coordinate, quality flag, angle, mask, or science variable
if its attributes, dtype, dimensions, or value distribution indicate that role.

## Bit Fields and Enums

For bit fields, packed quality variables, enums, and categorical masks, decode
explicitly before scientific use:

```text
raw_variable, raw_dtype, raw_unique_values, fill_codes,
bit_numbering_convention, field_name, start_bit, bit_count,
decoded_value_counts, enum_or_bit_meanings, meaning_source
```

If the official code table is unavailable or ambiguous, report the decoded
fields as diagnostic evidence, not as final production semantics.

Do not convert bit fields or enum quality flags into a continuous quality weight
unless a product-specific, documented mapping justifies it.

## Coordinate, Projection, and Time Audit

Before downstream use, establish and report:

```text
coordinate source, coordinate units, longitude convention,
pixel center/edge convention if known, projection/grid mapping,
swath vs grid geometry, valid geolocation mask,
time units/calendar/timezone, nominal vs observation vs scan time
```

Downstream geometric or temporal analysis belongs in a later task. First
preserve the inspection outputs so later methods have a documented basis.

## Existing Project Code

When working in Geo Ring Cloud, check existing readers and audits before writing
a new generic reader:

```text
D:\AAAresearch_paper\third_report\code\FY4B
D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\stage1_common.py
D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\04b_fy4b_dqf_bit_decode_diagnostics.py
D:\AAAresearch_paper\third_report\code\geo_data_audit
```

Use them as references for patterns such as HDF5 traversal, NetCDF variable
inspection, mask/scale handling, DQF/bit decoding, valid-mask construction, and
metadata reporting. Do not assume they are complete for a new product; verify
against the current file structure.

## Output Convention

For Geo Ring Cloud stage outputs, first resolve the correct canonical stage ID
from the project registry. Then use that stage ID as the prefix:

```text
<canonical_stage_id>_<product>_structure_inventory.csv
<canonical_stage_id>_<product>_variable_inventory.csv
<canonical_stage_id>_<product>_quality_flag_decode.csv
<canonical_stage_id>_<product>_sample_stats.csv
<canonical_stage_id>_<product>_inspection_report.md
<canonical_stage_id>_<product>_inspection_manifest.json
```

For general work, use a descriptive project/product prefix.

The manifest should include:

```text
input_files, reader_versions, inspection_script, output_files,
sample_strategy, variables_inspected, warnings, unresolved_semantics
```

Large rasters, arrays, images, HDF5, NetCDF, and other heavy outputs should not
be committed to Git. Index them by directory-level summaries or manifests.

## Verification

Before finishing:

- confirm inspection outputs exist
- confirm every representative input file was opened or explain why not
- report unreadable files, missing expected metadata, and uncertain semantics
- verify that raw and decoded statistics are internally consistent
- run project governance checks if working in a governed project repository
