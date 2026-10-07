# Handoff — corrected GEO inputs, 77-comparison CNN experiment

## Purpose and data boundary

The experiment asks whether GEO-ring spatial information improves recovery
of the native EPIC cloud mask after EPIC is **synthetically** averaged to
50 km. The target is the EPIC product, not independent meteorological truth.
EPIC `Cloud_Mask` 1–2 is clear (0), 3–4 cloudy (1), other codes invalid.
`C` is 50 km cell cloud fraction copied back to native EPIC pixels; `G` is
existing corrected GEO fusion sampled to those pixels; `Z` is shared geometry.
Compare deterministic nearest-cell, CNN-C, CNN-G, CNN-CG and CNN-CG with a
coarse-cell consistency (`closure`) loss. Seeds: 42, 43, 44. Parameters are
frozen in `config/cnn.yaml` and `config/coarsening.yaml`.

The source is the completed Stage 09c corrected-data run, **not** the old
53-scene CNN input set. Its frozen manifest has 80 EPIC comparisons at 79
GEO times. Three failed FY4B cases are explicitly excluded, leaving **77
EPIC comparisons at 76 GEO times**. Two comparisons share one GEO time.
Whole-day splits are train 41, validation 9, test 18, buffer 3+6. The old
53-scene set and this 77-comparison set have zero overlapping GEO times and
zero overlapping EPIC files, so do not call any v01/v03 metric difference a
paired navigation-correction effect.

## Corrected-source evidence and code

Source run directory, relative to the project's existing time-run storage:
`stage_09c_epic_80_satpy_navigation_rerun_202403/runs`.
The sibling `00_control/stage_10_epic_80_sample_manifest.csv` freezes all 80
comparison IDs and raw EPIC filenames. Its
`navigation_schema_verification.csv` records passed source-reader versions.
This entry point independently checks every retained GEO run's native
`Meteosat-0deg` CLM schema `meteosat_0deg_clm_v2` and `Meteosat-IODC` CLM
schema `meteosat_iodc_clm_satpy_v1`, both fusion participants, creation
order, source maps and target-grid file. On the original computer **76/76**
retained GEO runs passed. GEO source products remain read-only and are never
copied. Only derived EPIC-grid tensors, training outputs, metrics and figures
are written to a separate v03 output root.

Code: GitHub `Leihoulea/GEO_ring_cloud`, branch
`codex/epic-geo-navfixed-202403`; this prototype is at
`reconstruction/experiments/epic_geo_sr_202403`. On a new machine, clone that
branch, transfer or mount the completed Stage 09c run root **including its
`00_control` sibling**, transfer the 77 needed raw EPIC L2 CLOUD_03 files,
and install the CPU Python dependencies in `requirements.txt`. Merely cloning
Git does not provide satellite data. Set paths with environment variables;
do not edit the source manifest's historical drive letters.

PowerShell example (replace paths; choose a **new empty** output directory):

```powershell
git clone --branch codex/epic-geo-navfixed-202403 https://github.com/Leihoulea/GEO_ring_cloud.git
cd GEO_ring_cloud\reconstruction\experiments\epic_geo_sr_202403
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
$env:EPIC_GEO_SR_DATA_CONFIG = 'data_corrected77.yaml'
$env:EPIC_GEO_SR_ROOT = '<new empty v03 output directory>'
$env:GEO_RING_CORRECTED_RUNS_ROOT = '<completed Stage 09c directory>\runs'
$env:GEO_RING_EXTERNAL_EPIC_L2_ROOT = '<directory containing March EPIC L2 files>'
.\.venv\Scripts\python.exe src\rerun_from_corrected.py --preflight-only
.\.venv\Scripts\python.exe src\rerun_from_corrected.py
```

Preflight must report 80 source comparisons, exactly the three configured
exclusions, 77 accepted comparisons and 76 verified GEO runs. It creates a
new `inputs/scene_manifest.csv` in the v03 output root. `--max-stages 1`
runs only the first audit as a smoke test. The complete run performs input
audit, split freeze, 50 km operator/validation, GEO feature sampling, sample
preparation, deterministic baseline, then 4 CNN variants × 3 seeds and
held-out evaluation/bootstrap/figures. It never runs GEO Stage 02–08c.

## Outputs, interpretation, lessons

Read `<output root>/logs/rerun_corrected77_status.json` and `logs/stages/`.
Outputs are in `data/`, `checkpoints/`, `predictions/`, `metrics/`, `figures/`
and `reports/`. Brier, high-pass RMSE and closure MAE: **lower is better**;
F1, masked SSIM, boundary F1 and high-pass correlation: **higher is better**.
The paired bootstrap resamples whole held-out scenes 10,000 times, not pixels.
No corrected CNN conclusion should be claimed until this v03 run finishes.

Lessons: search existing versioned runs and their manifests before rebuilding
large satellite products; a corrected reader does not retroactively repair
old fused arrays. Keep comparison IDs distinct from GEO time IDs; two EPIC
comparisons can use one GEO time. Missing scenes must cause an explicit
exclusion/count check, never silently shrink the dataset. `G`'s so-called
`geo_cloud_probability` is presently a 0/1 Policy-A indicator, not a
calibrated cloud probability. The local `geo_boundary_fraction` channel is
the same 3×3 standard deviation as `geo_cloud_fraction_std`; it is not an
independent boundary measurement. March-only and 18 held-out comparisons
cannot establish all-season generalisation.
