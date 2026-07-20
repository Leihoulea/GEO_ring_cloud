# GEO-ring Cloud Scientific Figure Workflow

Purpose: turn stage diagnostics into defensible, reproducible, presentation-ready and manuscript-clean figures. This workflow is generalized from the Stage 09E/09F meeting-figure work, especially the Stage 09F spatial story maps, and should be followed for future GEO-ring Cloud plotting stages.

This is a plotting workflow, not a science-result workflow. It must not change production algorithms, fusion logic, cloud-mask semantics, sampling definitions, or reference-data interpretation unless the stage objective explicitly requires a new analysis stage.

## 0. Scope And Identity Gate

Before making any figure, decide whether the task is:

- stage-owned plotting: outputs belong to one canonical stage such as `stage_09f` or `stage_10`;
- cross-stage presentation: outputs are a `component_role` such as `presentation_builder`, not a fake combined stage;
- exploratory scratch work: may use a temporary smoke output, but must not replace canonical results.

Mandatory identity fields:

| Field | Rule |
| --- | --- |
| `project_id` | Use `geo_ring_cloud` unless the registry says otherwise. |
| `canonical_stage_id` | Use the exact registry ID, for example `stage_09f`; do not invent `stage09f`, `Stage09F`, or combined labels. |
| `run_id` | Use a stable stage-prefixed output ID, for example `stage_09f_spatial_story_maps_202403`. |
| `script_path` | Prefer a canonical stage path under `third_report/code/geo_ring_cloud_stage1/<stage_dir>/`. |
| `output_root` | Write generated products under the stage run area, not beside raw inputs or source code. |

Required project-memory checks before new code or reports:

1. `_GEO_RING_CLOUD_WORKSPACE/README.md`
2. `_GEO_RING_CLOUD_WORKSPACE/engineering_policy.md`
3. `_GEO_RING_CLOUD_WORKSPACE/architecture.md`
4. `_GEO_RING_CLOUD_WORKSPACE/engineering_status.md`
5. `_GEO_RING_CLOUD_WORKSPACE/module_registry.md`
6. `_GEO_RING_CLOUD_WORKSPACE/code_migrations.md`
7. `_GEO_RING_CLOUD_WORKSPACE/stage_registry.md`
8. `_GEO_RING_CLOUD_WORKSPACE/artifact_index.md`
9. `_GEO_RING_CLOUD_WORKSPACE/data_product_audits.md`
10. `_GEO_RING_CLOUD_INDEX/geo_ring_cloud_index.sqlite` for precise lookup

Use `rg` for focused code search after the index checks. Do not scan raw data or large time-run products unless the figure requires those artifacts.

## 1. Figure Contract Before Code

Every figure starts from a written contract. Do this before opening matplotlib.

| Contract item | Required answer |
| --- | --- |
| Core conclusion | What one sentence should the figure help defend? |
| Evidence chain | Which panel supports which part of the conclusion? |
| Audience | Group meeting, manuscript, QA audit, or internal debugging? |
| Archetype | Quantitative grid, spatial map, image plate plus quant, or mixed evidence panel. |
| Reference stance | If EPIC or another product is used, state whether it is a diagnostic reference or an absolute truth. |
| Risk | What could a reviewer or advisor misunderstand from this plot? |
| Export target | Slide-readable 16:9, Nature-style vector figure, or both. |

Drop panels that do not carry unique evidence. A pretty panel that cannot be explained from source data should not be included.

## 2. Reuse And Input Inventory

Start from existing stage products whenever possible. For a plotting-only stage, do not rerun pixel-level diagnostics if existing CSV/source tables already contain the needed evidence.

Minimum input inventory:

| Check | Requirement |
| --- | --- |
| Existing scripts | Search for prior figure scripts in the same stage and adjacent stages. |
| Existing CSVs | List the source CSVs used by each planned figure. |
| Row counts | Record row counts before filtering. |
| Units | Preserve units in labels, source CSV columns, and report text. |
| Key identifiers | Keep `sample_id`, time, source names, policy names, mask names, bin labels, or coordinates needed for tracing. |
| Missing variables | Write warning rows and skip affected panels; do not crash the whole run. |
| Semantic assumptions | State policy definitions, class encodings, masks, and validity criteria. |

For Earth-observation spatial plots, also check:

- coordinate fields and angular units;
- latitude orientation, longitude convention, and dateline handling;
- projection or map proxy used;
- whether a displayed coverage mask is a physical field-of-view boundary or only a valid-data proxy;
- whether sample count is sufficient for the stated interpretation.

## 3. Output Directory Contract

Use a repeatable four-folder structure:

```text
<output_root>/
  figures/
  source_data/
  reports/
  logs/
```

Required files:

| Location | Required content |
| --- | --- |
| `figures/` | `png`, `svg`, and `pdf` for every final figure. Add `tiff` when the figure is intended as manuscript submission art. |
| `source_data/` | One clean source CSV per figure or per figure family. Every plotted value must trace to one of these CSVs. |
| `logs/figure_index.csv` | One row per figure with paths to figure files and the source CSV. |
| `logs/manifest.json` | Run identity, inputs, outputs, parameters, row counts, warnings count, and verification summary. |
| `logs/warnings.csv` | Structured warning rows. It must exist even when there are zero warnings. |
| `reports/<run_id>_report_cn.md` | Chinese-first figure report explaining what was plotted, how to read it, limitations, and source-data links. |

Recommended `figure_index.csv` columns:

```text
figure_id,title,source_csv,png,svg,pdf,tiff,created_utc,notes
```

Recommended `warnings.csv` columns:

```text
level,source,sample_id,figure_id,message,traceback
```

Recommended manifest keys:

```text
project_id,canonical_stage_id,run_id,script_path,created_utc,
input_paths,output_paths,parameters,row_counts,figures,verification,constraints
```

## 4. Source Data Contract

Never let a figure be the only output. Each plotted category, count, point, bin, map pixel, or summary value must be recoverable from CSV.

For quantitative summary figures, source CSV should include:

- group keys;
- metric names;
- metric values;
- units;
- sample size `n`;
- policy or mask names;
- uncertainty columns when used;
- exact denominator used for fractions.

For spatial maps, source CSV should include:

- `sample_id`;
- original latitude and longitude, with units in column names or metadata;
- plotted variable code and label;
- projection or display coordinates if projection is applied;
- validity mask or denominator flag;
- any center longitude/latitude used for map display;
- stride or aggregation rule if the rendered map is downsampled.

For categorical maps, maintain a legend table or source rows containing:

```text
variable,code,label,color,hatch_or_symbol,definition
```

This prevents the common failure mode where a PNG has colors but no auditable explanation.

## 5. Script Structure

A stage plotting script should be deterministic and resumable.

Recommended top-level structure:

```text
constants:
  PROJECT_ID, STAGE_ID, RUN_ID, default input paths, default output root

style:
  matplotlib rcParams, palette, colormaps, legend definitions

io:
  ensure_dirs, read_csv/read_json, write_source, write_manifest, write_warnings

data preparation:
  load existing CSVs or existing stage products
  normalize schema and labels
  compute only plotting-specific summaries

plot helpers:
  save_figure
  add_panel_label
  add_map_legend
  add_colorbar
  projection/orientation helpers where needed

figure functions:
  one function per figure or figure family

verification:
  export checks
  source CSV checks
  warnings checks
  dimensions/aspect checks

main:
  parse CLI
  run smoke or full
  write figure_index, manifest, report, warnings
```

The script should accept CLI options for:

- `--output-dir`;
- sample limit or representative sample count;
- plot stride or rendering resolution;
- max aggregate samples when a full run may be slow;
- smoke/full mode when applicable.

## 6. Visual Design Rules

Use the selected plotting backend consistently. Current project default for Nature-style figures is Python/matplotlib.

Matplotlib requirements:

```python
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "DejaVu Sans", "Liberation Sans"],
    "svg.fonttype": "none",
    "pdf.fonttype": 42,
    "axes.spines.top": False,
    "axes.spines.right": False,
})
```

General design rules:

- white background for scientific plots and maps;
- restrained palette with stable semantics across figures;
- no rainbow/jet/hsv colormaps;
- readable labels at final size;
- direct labels where possible;
- legends inside the figure or in a dedicated legend panel when the figure must stand alone in PPT;
- colorbars must include variable name and units;
- fractions must state denominators;
- percentage-point changes should be labeled as `pp`, not `%`, when they mean absolute fraction differences;
- avoid misleading axes, truncated bars, or hidden zero baselines unless explicitly justified.

For slide-readable maps:

- keep the figure at or below 16:9;
- label sample time and display center longitude when orientation matters;
- keep north up and south down unless the projection explicitly says otherwise;
- state whether the view is a geodetic projection, raw image coordinates, or another display proxy;
- use color plus text/symbol legends, not text-only explanations outside the image.

## 7. Export Rules

Minimum export bundle:

| Format | Purpose |
| --- | --- |
| `svg` | Editable vector text and shapes for PowerPoint/Illustrator-like editing. |
| `pdf` | Vector backup and print-safe export. |
| `png` | Slide preview and fast visual inspection; use at least 300 dpi for final preview. |
| `tiff` | Add for journal submission raster requirements, usually 600 dpi. |

Do not use HTML as the only presentation output when the user needs editable PPT figures. HTML is useful for interactive inspection, but it is not a replacement for editable SVG/PDF.

## 8. Smoke Run

Before a full run, execute a small smoke run into a separate smoke output directory.

Smoke run should verify:

- the script starts from existing inputs without downloading data;
- at least one figure of each family is produced;
- source CSVs are non-empty;
- `figure_index.csv`, `manifest.json`, `warnings.csv`, and report are written;
- warnings are recorded as rows, not swallowed or printed only to console;
- representative visual previews are readable.

Smoke output may be indexed for traceability, but do not treat it as the formal result unless the user explicitly asks.

## 9. Visual QA

Open or inspect representative PNG previews before delivery. Automated checks cannot catch all visual mistakes.

Mandatory visual checks:

| Check | Failure example |
| --- | --- |
| Orientation | Earth disk rotated 90 degrees; selected-family zones appear top/bottom when they should be east/west. |
| Aspect ratio | Figure too tall for PPT; exceeds 16:9 when user needs slides. |
| Legend | Missing, text-only, or located outside cropped canvas. |
| Label clarity | Time, center longitude, units, policy, and mask names absent. |
| Text overlap | Titles, legends, colorbars, or footer notes cover data. |
| Color semantics | Same color means different things across panels without warning. |
| Scale | Colorbar lacks units or map categories lack code definitions. |
| Sampling | Downsampling or binning not stated. |
| Interpretation | Plot suggests causality or truth status not supported by the diagnostic design. |

For spatial EO maps, randomly audit at least two samples:

- plotted pixel/point count versus source CSV count after stride or mask;
- latitude/longitude range and projection visibility;
- source-family or coverage categories against source CSV code counts.

## 10. Automated QA

Run these checks before final delivery:

```powershell
python -m py_compile <plotting_script.py>
python <nature_figure_skill>/scripts/validate_figure.py <plotting_script.py>
```

Also run a file-level output check:

```text
for every figure_index row:
  source_csv exists
  source_csv has nonzero rows
  png/svg/pdf exist
  tiff exists if required
  png aspect ratio matches intended target
warnings.csv exists
manifest.json exists
report exists
```

Acceptable Nature-style validator warnings must be documented. Example: a 16:9 PPT figure can warn that its width is not a Nature single-column or double-column width; that is acceptable for group meeting slides, but not for manuscript submission.

All `FAIL` results must be fixed before delivery unless the user explicitly accepts the risk.

## 11. Formal Run And Report

After smoke and QA, run the full output into the canonical output directory.

The report should include:

- figure list and source-data files;
- how to read each panel;
- key limitations;
- warning summary;
- statement that EPIC or any other comparison product is a diagnostic reference if that is the stage design;
- explicit definitions for policies, masks, source families, kernels, bins, and any abbreviations;
- notes on projection, aggregation, and sampling.

Do not write "see CSV" as the only answer. The report must directly state the conclusions and then point to the CSV for audit.

## 12. Governance And Git

For new or changed stage plotting scripts:

```powershell
python _GEO_RING_CLOUD_INDEX\build_index.py
git add <script> <necessary workspace index markdown>
python _GEO_RING_CLOUD_INDEX\governance_check.py --staged
git commit -m "<canonical_stage_id> <short plotting change>"
```

For documentation-only workflow changes, still run the governance check before commit. If the documentation changes project-memory outputs or index summaries, stage the regenerated Markdown files required by the check.

Do not commit large generated figures, time-run products, SQLite/XLSX indexes, PPTX files, or raw satellite products unless the repository policy is explicitly changed.

## 13. Required Delivery Summary

When reporting back to the user, include:

- what files or scripts changed;
- where the formal outputs are;
- figure count and export formats;
- source CSV count and whether non-empty;
- warnings count;
- QA checks run and their result;
- governance check result;
- commit hash if committed.

Keep the final response concise, but never omit failed checks.

## 14. Stage 09F Lessons Generalized

The Stage 09F plotting cycle exposed several reusable lessons:

1. A map that looks like an image can be scientifically wrong if orientation is inherited from raw array order. For globe-style figures, project from latitude/longitude rather than assuming row/column display is a normal Earth view.
2. A figure may have a separate legend guide and still fail as a PPT figure. Each slide-used figure should be self-contained with in-figure legends.
3. Center longitude matters for disk maps; show it when different samples use different disk centers.
4. A figure can pass file existence checks but fail visual communication. Always open representative PNGs.
5. Source-family or coverage maps must say whether they show strict sensor field of view or valid-data/selected-source proxies.
6. PNG previews should be at least 300 dpi for slide delivery; use 600 dpi TIFF when manuscript raster output is required.
7. A warning row is better than a crashed run; missing variables should degrade a panel, not destroy the whole figure set.
8. Every figure must trace to CSV. If the figure needs a legend, the legend codes should be machine-readable too.

## 15. Minimal Checklist

Use this checklist before saying a figure stage is complete:

```text
[ ] canonical project_id and stage_id confirmed
[ ] existing scripts and outputs searched
[ ] figure contract written
[ ] inputs inventoried with row counts, units, and key identifiers
[ ] output root uses figures/source_data/reports/logs
[ ] every figure has source CSV
[ ] figure_index.csv written
[ ] manifest.json written
[ ] warnings.csv written
[ ] report_cn.md written
[ ] PNG/SVG/PDF exported
[ ] TIFF exported if manuscript raster is required
[ ] legends/colorbars/units/policies/masks visible
[ ] visual QA done on representative PNGs
[ ] source CSV non-empty checks passed
[ ] Nature/static figure preflight run where applicable
[ ] build_index.py run if stage scripts changed
[ ] governance_check.py --staged passed before commit
[ ] generated large artifacts kept out of Git unless explicitly allowed
```
