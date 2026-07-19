# Stage 09d Interpretation

Canonical implementation package for Stage 09d interpretation and follow-up
report generation.

## Entrypoints

- `stage_09d_analyze_geo_visible_filter.py`: visibility-filter sensitivity.
- `stage_09d_answer_questions.py`: follow-up diagnostic questions and tables.
- `stage_09d_audit_meteosat_semantics.py`: Meteosat cloud-mask semantics audit.
- `stage_09d_build_interpretation_package.py`: consolidated interpretation package.

Historical paths under `stage09d_interpretation/` remain executable
compatibility entrypoints. They contain no scientific or report logic.
