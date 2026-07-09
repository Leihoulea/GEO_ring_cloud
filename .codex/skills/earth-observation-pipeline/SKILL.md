---
name: earth-observation-pipeline
description: Use when inspecting unfamiliar or newly acquired Earth observation products such as CERES, DSCOVR, EPIC, GEO satellite files, HDF5, NetCDF, GRIB, or tabular metadata. Guides Codex to inventory internal structure, variables, attributes, units, coordinates, time fields, quality flags, bit fields, masks, quicklooks, and raw value distributions before writing analysis code.
metadata:
  short-description: New Earth observation product structure and variable inspection
---

# Earth Observation Product Inspection

Use this skill when a task involves reading or understanding a new satellite or
Earth observation data product. The immediate goal is not downstream matching,
fusion, or science analysis. The goal is to inspect the product deeply enough
that later processing does not rest on guessed variable semantics.

## Core Principles

- Keep raw files immutable.
- Inspect before interpreting. Do not assume semantics from file names or common
  variable names alone.
- Prefer product-structure evidence over generic assumptions.
- Preserve sensor/platform/product/version/time metadata.
- Treat geolocation, projection, time basis, masks, quality flags, packed bits,
  units, fill values, special category codes, and scale/offset as essential data.
- If project-specific readers or prior audits exist, use them as engineering
  patterns, but verify that their assumptions match the current product.

## Report Language And Encoding

Inspection reports should be Chinese-first:

- use Chinese for narrative, warnings, conclusions, anomaly explanations, and
  next-step recommendations
- keep English for variable names, product names, filenames, units, code-table
  labels, and technical terms such as `CPD`, `COT`, `CER`, `DQF`, `QA`,
  `scale_factor`, and `add_offset`
- keep CSV/JSON column names machine-readable in English
- write Markdown/CSV/JSON as UTF-8 or UTF-8 with BOM when Excel compatibility is
  important
- verify that generated Chinese text is not mojibake before finishing

## Reader Choice

Prefer structured readers:

- NetCDF/CF: `xarray`, `netCDF4`
- HDF5: `h5py`, product-specific readers, `xarray` where supported
- GRIB: `cfgrib` when available
- tabular metadata: `pandas`
- XML/manifest/package metadata: structured XML/ZIP readers where applicable

If multiple readers are possible, compare their reported dimensions, variables,
attributes, mask/scale behavior, raw values, decoded values, and reader-version
metadata before choosing one. If the preferred reader is unavailable and a
fallback reader is used, record the limitation in the manifest and report.

## Required Inventories

For representative files, produce these inventories when possible:

- file inventory: path, size, modified time, parsed platform/product/time,
  version, and open status
- structure inventory: group tree, dataset/variable count, dimension count,
  global attribute count, and a stable structure signature/hash
- dimension inventory: dimension names, lengths, unlimited status, dimension
  scale relationships, and coordinate links
- global attribute inventory: one row per global attribute, not only one large
  JSON blob
- variable inventory: variable path/name, role guess, shape, dtype, chunks,
  compression, dimensions, coordinates, grid mapping, units, long name,
  standard name, fill values, valid ranges, scale/offset, code-table attributes,
  and all attributes
- variable-attribute inventory: one row per variable attribute for auditability
- sample statistics: raw and physical statistics with explicit sample scope
- anomaly table: unreadable files, missing expected metadata, unit/value
  contradictions, uncertain semantics, and reader limitations

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
if its attributes, dtype, dimensions, links, or value distribution indicate that
role.

## Values, Units, And Masks

For numeric variables, inspect both raw storage values and interpreted physical
values:

- record fill/missing values separately from documented category/sentinel codes
  such as space, clear, nighttime, off-disk, no retrieval, or not processed
- compute statistics for raw values, fill-masked values, and physical-only
  values when category/sentinel codes are embedded in the numeric array
- record whether statistics are full-array, sampled, or downsampled
- flag unit/value contradictions, for example a geostationary satellite height
  value that looks like meters while the unit attribute claims kilometers
- do not silently apply `scale_factor` or `add_offset`; record values before and
  after conversion

## Bit Fields, Enums, And Code Tables

For bit fields, packed quality variables, enums, and categorical masks, decode
explicitly before scientific use.

Required outputs include:

```text
raw_variable, raw_dtype, raw_unique_values, fill_codes,
flag_values, flag_masks, flag_meanings,
bit_numbering_convention, bit_index, bit_mask_decimal, bit_mask_hex,
field_name, start_bit, bit_count, decoded_value_counts,
observed_code_or_combination, decoded_meanings, meaning_source
```

If the official code table is unavailable or ambiguous, report decoded fields as
diagnostic evidence, not final production semantics.

Do not convert bit fields or enum quality flags into a continuous quality weight
unless a product-specific, documented mapping justifies it.

## Coordinate, Projection, And Time Audit

Before downstream use, establish and report:

```text
coordinate source, coordinate units, longitude convention,
pixel center/edge convention if known, projection/grid mapping,
swath vs grid geometry, valid geolocation mask,
time units/calendar/timezone, nominal vs observation vs scan time
```

If per-pixel latitude/longitude are absent, say so explicitly and identify what
navigation metadata exists instead. Do not imply geolocation is solved merely
because projection x/y or sub-satellite metadata are present.

Downstream geometric or temporal analysis belongs in a later task. First
preserve the inspection outputs so later methods have a documented basis.

## Cross-File Consistency

When more than one file is available, report consistency across files:

- file count, time coverage, cadence, missing or duplicate time slots
- structure signature counts and any files with divergent signatures
- variable presence/absence by file
- shape/dtype/unit/fill/scale/offset consistency by variable
- value-distribution changes for representative first/middle/last samples
- product-version or processing-version changes

## Inspection Quicklooks

Generate quicklook PNGs when the product contains meaningful 2D numeric,
categorical, mask, or decoded quality variables. Quicklooks are part of product
inspection, not final publication figures.

Quicklook requirements:

- generate quicklooks for representative times, not for every file by default
- default to at most 10 quicklook PNGs per inspection task unless the user
  explicitly asks for more
- choose representative times deliberately, such as first/middle/last, day/night
  contrast, high-missing fraction, unusual quality-flag distribution, or a file
  with a divergent structure signature
- cover multiple relevant variable classes within the 10-image budget: primary
  science fields, cloud variables, coordinates/angles when present, masks,
  DQF/QA raw codes, decoded bit fields, and suspicious variables found during
  inspection
- do not use the first 2D array alone unless it is clearly the only meaningful
  variable to visualize
- plot physical fields with fill and category/sentinel codes masked out
- when category/sentinel codes are scientifically important, create separate
  categorical quicklooks for those masks
- use categorical colors and explicit tick labels for enum, mask, and bit-field
  outputs
- use robust percentile scaling for continuous fields, with fill/missing values
  masked to a neutral background
- include readable title, product/time/variable context, colorbar, units where
  known, and a note when semantics are uncertain
- downsample large arrays for plotting without changing CSV statistics
- write a quicklook index with `plot_path`, `source_variable`, `source_file`,
  `representative_reason`, `selected_for_plot`, `not_plotted_reason`, `reader`,
  `scaling`, `colormap`, `units`, `valid_mask_rule`, and `meaning_note`

Use Satpy when it is the best available way to obtain calibrated or geolocated
display data for that product, but do not use Satpy as a substitute for raw
structure inspection. If Satpy output is used, also record the raw source
variable and reader/calibration mode.

## Output Convention

For Geo Ring Cloud stage outputs, first resolve the correct canonical stage ID
from the project registry. Then use that stage ID as the prefix:

```text
<canonical_stage_id>_<product>_file_inventory.csv
<canonical_stage_id>_<product>_structure_inventory.csv
<canonical_stage_id>_<product>_dimension_inventory.csv
<canonical_stage_id>_<product>_global_attributes.csv
<canonical_stage_id>_<product>_variable_inventory.csv
<canonical_stage_id>_<product>_variable_attributes.csv
<canonical_stage_id>_<product>_code_table.csv
<canonical_stage_id>_<product>_bitfield_diagnostics.csv
<canonical_stage_id>_<product>_observed_code_combinations.csv
<canonical_stage_id>_<product>_sample_stats.csv
<canonical_stage_id>_<product>_quicklook_index.csv
<canonical_stage_id>_<product>_anomalies.csv
<canonical_stage_id>_<product>_inspection_report.md
<canonical_stage_id>_<product>_inspection_manifest.json
```

For general work, use a descriptive project/product prefix.

The manifest should include:

```text
input_files, reader_versions, inspection_script, output_files,
sample_strategy, variables_inspected, structure_signatures, quicklooks,
warnings, unresolved_semantics
```

Large rasters, arrays, images, HDF5, NetCDF, and other heavy outputs should not
be committed to Git. Index them by directory-level summaries or manifests.

## Verification

Before finishing:

- confirm required inventories exist, or explain why a table is not applicable
- confirm quicklooks exist, or explain why no meaningful 2D variable was plotted
- confirm every representative input file was opened or explain why not
- report unreadable files, missing expected metadata, unit contradictions, and
  uncertain semantics
- verify that raw, masked, physical, decoded, and quicklook statistics are
  internally consistent
- verify Chinese report text renders correctly
- run project governance checks if working in a governed project repository
