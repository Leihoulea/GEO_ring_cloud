# EPIC–GEO cloud-mask reconstruction (2024-03)

Current path: **corrected existing-data v03 (77 EPIC comparisons / 76 GEO runs)**.
Read [HANDOFF_CORRECTED77.md](HANDOFF_CORRECTED77.md) and run
`src/rerun_from_corrected.py`. This uses repaired Stage 09c GEO products
read-only, with no raw GEO reprocessing or large GEO product copies. The
v02 raw-rebuild path below is retained only for historical/recovery use.

**Read [HANDOFF.md](HANDOFF.md) first.** Historical v01 GEO pairings predate
the Meteosat navigation repair and cannot be used to train/evaluate v02.
The complete entry point is `src/rerun_navfixed.py`; it rebuilds 53 GEO
pairings from raw files, validates source navigation versions, performs all
coarsening/baseline stages, then trains/evaluates the CNN ablations. Set
`EPIC_GEO_SR_ROOT` to a new empty output directory; code/config remain here.
The old `D:\EPIC_GEO_SR_202403` directory is historical evidence only and
must not be modified.

This tests whether GEO-ring spatial structure helps reconstruct the native
EPIC cloud mask after synthetic 50 km coarsening. It is not independent cloud
truth, and it does not train CEH, U-Net, or Transformer models.
