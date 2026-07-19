# Stage 09d Full-Pixel Diagnostics

Canonical implementation package for the Stage 09d March 2024 full-pixel
diagnostic runner.

## Entrypoint

- `stage_09d_run_full_pixel_diagnostics.py`: builds the Stage 09d sample
  manifest, source-pair metrics, sampling sensitivity, geometry/boundary
  diagnostics, error atlas, summaries, and reports.

Reusable sampling and policy logic lives in
`geo_ring_cloud.diagnostics.full_pixel`; cross-stage workflow support lives in
`geo_ring_cloud.diagnostics.full_pixel_workflow`.

The historical path
`stage09d_full_pixel_diagnostics/run_stage09d_full_pixel_diagnostics.py`
remains an executable compatibility entrypoint.
