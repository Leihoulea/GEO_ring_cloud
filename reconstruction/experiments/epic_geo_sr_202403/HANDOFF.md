# Handoff — EPIC–GEO 2024-03 navigation-fixed prototype

## Purpose and logic

The frozen set has 53 matched EPIC/GEO scenes from 5–31 March 2024. EPIC
`Cloud_Mask` codes 1–2 become clear (0), 3–4 cloudy (1), other codes invalid.
Native EPIC is averaged within 50 km cells; the cell cloud fraction is copied
back to native pixels (`C`). GEO-ring's fused cloud mask is mapped to EPIC
pixels and converted by Policy A (GEO 0–1 clear; 2–3 cloudy), with validity,
source-count and local-texture features (`G`). Geometry/common masks are `Z`.
The target is EPIC's native binary cloud mask, **not independent cloud truth**.
Deterministic nearest-cell reconstruction and CNN-C, CNN-G, CNN-CG and
CNN-CG-with-closure are compared on whole held-out scenes. Closure penalises
disagreement between reconstructed cell means and coarse EPIC cell means.
The three seeds are 42/43/44. Splits, architecture and training parameters
are frozen in `config/`.

## Critical defect and what changed

Historical v01 fused GEO files were built **before** the Meteosat-0deg and
Meteosat-IODC CLM navigation fixes. Thus v01 CNN/Brier/SSIM results are
historical comparison only, **not valid navigation-corrected estimates**.
v02 rebuilds pairings from raw GEO with the repaired core reader and refuses
any scene lacking native metadata with `meteosat_0deg_clm_v2` and
`meteosat_iodc_clm_satpy_v1`. It also checks both fused participants, fused
time, source identity, and required arrays. A test against v01 failed closed
at its first missing native provenance file. Do not recycle old figures or
the v01-specific conclusion generator; it is deliberately excluded from v02.

## Fresh-machine setup

Clone `https://github.com/Leihoulea/GEO_ring_cloud.git`, branch
`codex/epic-geo-navfixed-202403`. Prototype code is in
`reconstruction/experiments/epic_geo_sr_202403`; the upstream repaired reader
and single-sample pipeline are in `third_report/code/geo_ring_cloud_stage1`.
Required **external** data are not in Git: March-2024 raw GOES-16/18,
Himawari-9, FY4B, Meteosat-0deg/IODC products; the 53 EPIC L2 CLOUD_03
files; and stage-01 `time_index/core_time_index.csv` with valid raw GEO paths.
The checked-in `inputs/scene_manifest.csv` freezes times/IDs. Its historical
absolute EPIC paths are rebound by filename under the EPIC environment root.
On a new machine, restore raw data and build the stage-01 source index from
the core code and parsed source metadata, or transfer/rebind a valid index.
Check every `product_files_json` path in the index exists before running.
The upstream conda spec is
`third_report/code/geo_ring_cloud_stage1/environment.yml` (Satpy 0.60).
Use a **separate CPU Python** with this prototype's `requirements.txt` for
training. The original Windows `pytorch` env had a broken torch DLL/OpenMP
stack; the project-local `.venv` passed import/tensor preflight.

Example PowerShell (replace all paths; choose a **new empty** output root):

```powershell
git clone --branch codex/epic-geo-navfixed-202403 https://github.com/Leihoulea/GEO_ring_cloud.git
cd GEO_ring_cloud\reconstruction\experiments\epic_geo_sr_202403
conda env create -f ..\..\..\third_report\code\geo_ring_cloud_stage1\environment.yml
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
$env:GEO_RING_EXTERNAL_EPIC_L2_ROOT = 'E:\GEO_Cloud_2024\DSCOVR_EPIC_L2_CLOUD_03_2024.03'
$env:EPIC_GEO_SR_ROOT = 'D:\EPIC_GEO_SR_202403_v02_navfixed'
& <UPSTREAM_CONDA_PYTHON> src\rerun_navfixed.py --upstream-python <UPSTREAM_CONDA_PYTHON> --model-python .\.venv\Scripts\python.exe --base-stage-root <GEO_RING_STAGE01_ROOT>
```

The entry point rebuilds/checks all 53 pairings, audits inputs, freezes
whole-day splits, simulates coarsening, builds features/patches, runs the
deterministic baseline, trains four CNN variants × three seeds, and computes
held-out metrics/figures. `--max-scenes 1` smoke-tests one pairing and stops
before training. Successful, validated pairings are skipped on a resumed run.
Full upstream intermediate files are ~3 GB per scene. By default the runner
keeps only source CLM provenance, required fused cloud arrays, grid definition
and optional cloud-top height; generated v02 scratch is removed **only after**
the retained copy passes validation. `--keep-scratch` needs ~160 GB. The old
`D:\EPIC_GEO_SR_202403` directory is never modified.

## Progress, outputs, interpretation

In the new output root, read `logs/rerun_navfixed_status.json` for per-scene
progress; `logs/upstream/<scene>.log` and `logs/stages/` for failures.
Products are in `data/`, checkpoints in `checkpoints/`, figures in `figures/`,
and metrics in `metrics/`. `model_comparison.csv` includes Brier, high-pass
RMSE and closure MAE (**lower is better**) plus F1, masked SSIM, boundary F1
and high-pass correlation (**higher is better**). `paired_scene_bootstrap.csv`
uses 10,000 paired resamples of **whole test scenes**, not pixels.
Only after v02 finishes may new numerical conclusions be drawn; v01 numbers
must be labelled navigation-invalid. No corrected result existed at handoff.

## Lessons and limits

- Fused timestamps/participant lists do not prove source navigation; check
  the **native** reader schema and tie it to the fused artifact.
- Never silently drop missing scenes; all 53 must pass. Keep code paths
  independent of the output root. Never overwrite v01 when repeating work.
- `geo_cloud_probability` is currently a 0/1 Policy-A indicator, **not** a
  calibrated probability. `geo_boundary_fraction` duplicates the local 3×3
  standard-deviation feature rather than an independent boundary estimate.
- GEO and EPIC both have classification errors, temporal/viewing differences
  and navigation uncertainty. The test has only nine held-out scenes in one
  month, so it does not establish global or seasonal generalisation.
