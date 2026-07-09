---
name: research-data-analysis
description: Use for scientific data analysis tasks involving cleaning, unit conversion, statistical summaries/tests, anomaly/outlier handling, reproducible tables, figures, manifests, and audit-ready result outputs.
metadata:
  short-description: Reproducible research data cleaning and statistics workflow
---

# Research Data Analysis

Use this skill for research data analysis tasks where correctness,
reproducibility, units, statistics, and result tables matter.

## Core Principles

- Never overwrite raw data. Write cleaned or derived data to a new path.
- Preserve row counts and key identifiers through each transformation.
- Make unit conversions explicit and auditable.
- Treat missing values, fill values, masks, and quality flags as first-class
  analysis inputs, not as incidental cleanup.
- Prefer deterministic scripts/notebooks over manual spreadsheet edits.
- Save outputs with enough metadata to reproduce them.

## Required Workflow

1. Identify inputs, expected units, coordinate/time columns, and quality fields.
2. Load using structured readers (`pandas`, `xarray`, `h5py`, `netCDF4`,
   `openpyxl`) rather than ad hoc text parsing when possible.
3. Normalize schema: column names, dtypes, units, timestamps, categorical labels.
4. Validate:
   - row counts
   - duplicate keys
   - missing values
   - impossible physical ranges
   - coordinate and time bounds
5. Apply cleaning rules with an explicit reason for each exclusion or mutation.
6. Compute statistics with stated sample size, filters, and uncertainty where
   appropriate.
7. Export machine-readable tables plus a short human-readable report.

## Unit Handling

Record every conversion:

```text
source_variable, source_unit, target_unit, formula, affected_rows
```

Do not mix temperature scales, angular units, cloud fractions, pressure units,
or radiance/reflectance conventions without explicit conversion.

## Outliers and Anomalies

Do not silently delete outliers. Classify them:

```text
invalid_physical_range
quality_flagged
statistical_outlier
duplicate_or_near_duplicate
time_or_location_mismatch
sensor_or_processing_artifact
unknown_requires_review
```

For each class, report count, percentage, examples, and whether rows were kept,
masked, winsorized, or excluded.

## Statistical Outputs

For comparisons, include:

- `n`
- mean/median
- standard deviation or robust spread
- bias/error metrics when relevant
- confidence interval or uncertainty method when appropriate
- exact filters and grouping keys

Use statistical tests only when their assumptions are plausible; otherwise use
robust summaries or non-parametric alternatives and state the limitation.

## Output Convention

For a stage-owned Geo Ring Cloud analysis, first resolve the correct canonical
stage ID from the project registry. Then use that stage ID as the prefix:

```text
<canonical_stage_id>_<purpose>_cleaned.csv
<canonical_stage_id>_<purpose>_summary.csv
<canonical_stage_id>_<purpose>_report.md
<canonical_stage_id>_<purpose>_manifest.json
```

For general analysis, use a descriptive project/component prefix.

The manifest should include:

```text
input_paths, output_paths, script_path, run_time, filters, unit_conversions,
row_counts, exclusions, warnings
```

## Verification

Before finishing, check that output files exist, row counts match expectations,
and report/table names match the applicable project naming policy.
