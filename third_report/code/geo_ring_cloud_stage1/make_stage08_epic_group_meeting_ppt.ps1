param(
  [string]$SummaryRoot,
  [string]$OutDir
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "geo_ring_cloud_path_configuration.ps1")

if (-not $PSBoundParameters.ContainsKey("SummaryRoot")) {
  $SummaryRoot = Join-Path $GeoRingRunsRoot "epic_202403_multisample_summary"
}
if (-not $PSBoundParameters.ContainsKey("OutDir")) {
  $OutDir = Join-Path $SummaryRoot "ppt_group_meeting"
}

function Ensure-Dir([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
  }
}

function Add-TextBox($slide, [string]$Text, [single]$Left, [single]$Top, [single]$Width, [single]$Height, [single]$FontSize = 20, [bool]$Bold = $false) {
  $shape = $slide.Shapes.AddTextbox(1, $Left, $Top, $Width, $Height)
  $shape.TextFrame.TextRange.Text = $Text
  $shape.TextFrame.TextRange.Font.Size = $FontSize
  $shape.TextFrame.TextRange.Font.Name = "Arial"
  $shape.TextFrame.TextRange.Font.Bold = [int]$Bold
  $shape.TextFrame.WordWrap = -1
  return $shape
}

function Add-Title($slide, [string]$Title, [string]$SubTitle = "") {
  Add-TextBox $slide $Title 36 24 880 54 28 $true | Out-Null
  if ($SubTitle.Length -gt 0) {
    Add-TextBox $slide $SubTitle 38 74 860 26 12 $false | Out-Null
  }
  $line = $slide.Shapes.AddShape(1, 36, 108, 888, 2)
  $line.Fill.ForeColor.RGB = 0x5A5A5A
  $line.Line.Visible = 0
}

function Add-Bullets($slide, [string[]]$Items, [single]$Left, [single]$Top, [single]$Width, [single]$Height, [single]$FontSize = 17) {
  $text = ($Items | ForEach-Object { "• " + $_ }) -join "`r`n"
  $shape = Add-TextBox $slide $text $Left $Top $Width $Height $FontSize $false
  $shape.TextFrame.TextRange.ParagraphFormat.SpaceAfter = 5
  return $shape
}

function Add-ImageFit($slide, [string]$Path, [single]$Left, [single]$Top, [single]$BoxW, [single]$BoxH) {
  if (-not (Test-Path -LiteralPath $Path)) {
    Add-TextBox $slide ("Missing image: " + $Path) $Left $Top $BoxW $BoxH 10 $false | Out-Null
    return $null
  }
  $pic = $slide.Shapes.AddPicture($Path, 0, -1, $Left, $Top, -1, -1)
  $scaleW = $BoxW / $pic.Width
  $scaleH = $BoxH / $pic.Height
  $scale = [Math]::Min($scaleW, $scaleH)
  $pic.Width = $pic.Width * $scale
  $pic.Height = $pic.Height * $scale
  $pic.Left = $Left + ($BoxW - $pic.Width) / 2
  $pic.Top = $Top + ($BoxH - $pic.Height) / 2
  return $pic
}

function Add-Foot($slide, [int]$Number) {
  Add-TextBox $slide ("Stage 08 EPIC comparison | " + $Number) 690 512 250 20 9 $false | Out-Null
}

Ensure-Dir $OutDir

$pptxPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting.pptx"
$pdfPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting.pdf"
$notesPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_speaker_notes.md"

$img01 = Join-Path $SummaryRoot "01_sample_level_metrics_cn.png"
$img02 = Join-Path $SummaryRoot "02_group_summary_cn.png"
$img03 = Join-Path $SummaryRoot "03_source_performance_cn.png"
$img04 = Join-Path $SummaryRoot "04_source_fraction_by_sample_cn.png"
$img06 = Join-Path $SummaryRoot "06_group_B_metrics_bar_cn.png"
$plotA = Join-Path $SummaryRoot "plots\agreement_by_sample_A_B.png"
$plotHeat = Join-Path $SummaryRoot "plots\agreement_by_source_heatmap_A.png"
$qlRoot = Join-Path $SummaryRoot "renamed_quicklooks"
$qlEast = Join-Path $qlRoot "20240313_0400_east-asia-fy4b-himawari-priority_Himawari-9_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png"
$qlGoes = Join-Path $qlRoot "20240313_2200_goes-dominant-control_GOES-18_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png"
$qlMet = Join-Path $qlRoot "20240311_1400_meteosat-dominant-control_Meteosat-0deg_B_B_high_confidence_only_epic_vs_georing_cloud_mask.png"

$pp = New-Object -ComObject PowerPoint.Application
$pp.Visible = -1
$pres = $pp.Presentations.Add()
$pres.PageSetup.SlideWidth = 960
$pres.PageSetup.SlideHeight = 540

$blankLayout = 12
$n = 0

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-TextBox $s "Stage 08: EPIC Reference Comparison for GEO-ring Cloud Composite" 48 80 850 88 32 $true | Out-Null
Add-TextBox $s "From single-time visual sanity check to multi-sample semantic and geometric diagnostics" 50 174 820 38 18 $false | Out-Null
Add-Bullets $s @(
  "Research aim: evaluate whether the GEO-ring cloud mosaic is visually and statistically consistent with independent DSCOVR EPIC cloud observations.",
  "Scope: cloud mask comparison, EPIC L2 semantics, source-specific diagnostics, geometry/latitude sensitivity, and implications for fusion v2.",
  "This is an independent-reference validation, not an assumption that EPIC is absolute truth."
) 58 250 825 150 17 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Scientific Motivation" "Why Stage 08 is necessary after GEO-ring fusion"
Add-Bullets $s @(
  "GEO-ring fusion closed the engineering loop: standardized native products, 0.05 degree grid, best-source fusion, and overlap validation.",
  "The remaining question is external consistency: does the fused image look and behave like an independent full-disk observation?",
  "EPIC is useful because it observes the illuminated Earth disk from L1 with independent viewing geometry and cloud retrieval.",
  "The comparison tests three risks: semantic mismatch, geometry/edge bias, and source-boundary artifacts."
) 58 140 830 220 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Data and Workflow" "Engineering the comparison as a reusable Stage 08 pipeline"
Add-Bullets $s @(
  "Inputs: GEO-ring fused products from Stage 01-06 and DSCOVR EPIC L2 Cloud monthly files.",
  "Key scripts: 08c semantic sensitivity, 08e multi-sample summary, 08f geometry/pre-fusion diagnostics, 08g overlap-count diagnostics.",
  "The workflow supports parameterized single-sample and batch runs, avoiding hard-coded times and one-off notebooks.",
  "Outputs include per-sample metrics, group summaries, source diagnostics, quicklooks, and reports."
) 52 134 422 294 17 | Out-Null
Add-ImageFit $s $img04 506 134 390 300 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Sample Design" "Eight completed EPIC samples spanning source-dominant regimes"
Add-Bullets $s @(
  "East Asia priority: tests FY4B/Himawari-dominant regions.",
  "GOES dominant controls: tests relatively strong ABI-dominant regions.",
  "Meteosat dominant controls: tests the weakest suspected source family.",
  "Mixed/boundary sample: tests source boundary behavior and multi-satellite competition."
) 52 136 395 250 17 | Out-Null
Add-ImageFit $s $img01 480 128 420 330 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Comparison Geometry" "What is compared, and what is not compared"
Add-Bullets $s @(
  "Current quantitative mode samples the GEO-ring 0.05 degree lon-lat grid to EPIC L2 pixel latitude/longitude using nearest neighbor.",
  "This avoids visual-only judgment, but it is not a full radiance-level EPIC-view reprojection.",
  "High latitude, disk edge, cloud boundary, and parallax-sensitive pixels can carry representativeness error.",
  "Therefore results are interpreted by latitude, EPIC view angle, valid-count, and pre-fusion source."
) 58 138 820 230 19 | Out-Null
Add-TextBox $s "Important interpretation: EPIC is an independent reference. It is not used as training truth or as a direct fusion constraint in this stage." 66 394 800 52 18 $true | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Cloud Mask Semantics" "Why naive binary comparison was not enough"
Add-Bullets $s @(
  "Different sensors encode cloud mask categories differently: GOES is near-binary, while FY4B/Himawari/Meteosat/EPIC include multi-class confidence or processing states.",
  "Stage 08c introduced semantic policies instead of hard-coded value equality.",
  "Mode A: inclusive cloudy definition; Mode B: high-confidence-only definition; Mode C: three-class uncertainty-aware diagnostic.",
  "The A/B gap measures how much apparent disagreement comes from uncertain cloud classes rather than pure geometry or fusion failure."
) 54 134 820 266 18 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Overall Result" "Agreement improves when semantic uncertainty is handled conservatively"
Add-ImageFit $s $plotA 64 130 800 330 | Out-Null
Add-TextBox $s "Across samples, Mode B is generally higher than Mode A. This indicates that a meaningful part of the mismatch is driven by cloud-mask confidence semantics, not only by geolocation or fusion." 86 468 760 36 13 $false | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Grouped Statistics" "Performance differs strongly by source-dominant regime"
Add-ImageFit $s $img02 64 124 390 330 | Out-Null
Add-Bullets $s @(
  "East Asia FY4B/Himawari priority: A agreement around 0.770, B around 0.821.",
  "GOES dominant control: A around 0.807, B around 0.838.",
  "Meteosat dominant control: A around 0.612, B around 0.622.",
  "Mixed/boundary: A around 0.678, B around 0.694.",
  "Main message: the method is not uniformly bad; the weakness is source-dependent."
) 506 136 386 280 16 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Representative Quicklooks" "The quicklooks expose source-specific visual behavior"
Add-ImageFit $s $qlEast 28 130 295 320 | Out-Null
Add-ImageFit $s $qlGoes 332 130 295 320 | Out-Null
Add-ImageFit $s $qlMet 636 130 295 320 | Out-Null
Add-TextBox $s "East Asia / FY4B-Himawari" 64 456 230 22 12 $true | Out-Null
Add-TextBox $s "GOES dominant" 430 456 150 22 12 $true | Out-Null
Add-TextBox $s "Meteosat dominant" 725 456 160 22 12 $true | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Pre-fusion Source Diagnostics" "A weak fused result can be a weak input-source result"
Add-ImageFit $s $img03 50 126 420 332 | Out-Null
Add-Bullets $s @(
  "Pre-fusion comparisons show that source quality varies strongly before best-source selection.",
  "GOES-18 and Himawari generally perform better than Meteosat CLM in EPIC cloud-mask space.",
  "Meteosat-related cloud-mask agreement is low even before fusion, so this is not only a source-map artifact.",
  "This supports a conservative Meteosat CLM role in future cloud-mask fusion."
) 510 138 368 250 16 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Source Heatmap" "Agreement is structured by satellite source and sample type"
Add-ImageFit $s $plotHeat 80 126 760 330 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Geometry and Latitude Diagnostics" "Edge effects matter, but do not explain everything"
Add-Bullets $s @(
  "All-sample Mode A agreement: about 0.728.",
  "Within |lat| < 60 deg: about 0.733; high latitude 70-90 deg: about 0.448, but with much smaller pixel counts.",
  "EPIC high view-angle bins also degrade, consistent with edge and representativeness effects.",
  "Conclusion: latitude and EPIC disk-edge geometry contribute to disagreement, but cannot alone explain the full source-dependent gap."
) 60 142 818 250 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Overlap-count Diagnostics" "Is mismatch concentrated in overlap or non-overlap regions?"
Add-Bullets $s @(
  "All valid pixels: agreement about 0.728 and F1 about 0.780.",
  "Overlap >= 2 sources: agreement about 0.727 and F1 about 0.777.",
  "Valid-count = 1: agreement about 0.557 and F1 about 0.644.",
  "Interpretation: both single-source margins and multi-source overlap regions matter; the problem is not only at one type of boundary.",
  "This motivates uncertainty-aware fusion rather than just a hard source switch."
) 58 136 820 286 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Why Not Put EPIC into Fusion?" "EPIC is a reference, not an operational GEO-ring input"
Add-Bullets $s @(
  "EPIC is not geostationary; it observes only the sunlit Earth disk from L1.",
  "Its revisit cadence and viewing geometry do not provide continuous GEO-ring coverage.",
  "Using EPIC in fusion would contaminate validation: the independent reference would become part of the product.",
  "Better use: calibration/diagnostic benchmark for cloud-mask semantics, source weighting, uncertainty maps, and regional failure modes."
) 62 142 805 238 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "What the Current Fusion Does" "Stage 06 is a hard best-source baseline"
Add-Bullets $s @(
  "Each grid cell and variable chooses one source using rating = valid * view_weight * time_weight * product_level_weight.",
  "Cloud mask uses fusion_valid_mask to remove off-disc and not-processed pixels.",
  "There is no averaging, no consensus voting, and no boundary feathering in the current baseline.",
  "Therefore the fused cloud binary often resembles the selected pre-fusion source in each region; source boundaries can remain visible."
) 58 138 826 250 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Implication for Fusion v2" "The next improvement should be scientifically constrained"
Add-Bullets $s @(
  "Replace hard binary selection with probability/consensus-aware cloud-mask fusion where multiple sources overlap.",
  "Down-weight sources or classes that have consistently weak EPIC agreement, especially Meteosat CLM under current semantics.",
  "Use EPIC only as an external diagnostic for tuning, not as an input layer to production fusion.",
  "Add boundary uncertainty maps and report valid-count/source-margin alongside fused cloud products.",
  "Keep F1, IoU, agreement, and class-specific confusion metrics; agreement alone can hide cloud/clear imbalance."
) 58 134 824 302 19 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Workload Demonstrated" "This stage is more than a quicklook comparison"
Add-Bullets $s @(
  "EPIC L2 cloud structure inspection and semantic mapping.",
  "Reusable batch runner for multiple EPIC times and source-dominant regimes.",
  "Multi-policy cloud-mask comparison: inclusive, high-confidence, and uncertainty-aware modes.",
  "Pre-fusion source-specific evaluation, geometry stratification, and overlap-count diagnostics.",
  "Automated summaries, plots, quicklook index, and written reports for traceability."
) 66 134 790 300 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Main Conclusions" "Stage 08 interpretation"
Add-Bullets $s @(
  "The GEO-ring cloud product shows useful external consistency in GOES-dominant and FY4B/Himawari-dominant regimes.",
  "Meteosat-dominant cloud mask remains the clearest weakness and should be treated conservatively.",
  "Semantic uncertainty has a measurable impact: Mode B improves agreement relative to Mode A.",
  "High latitude and EPIC edge geometry degrade agreement but do not fully explain the source-dependent differences.",
  "The current Stage 06 product is a valid prototype baseline, but not yet a production-grade cloud-mask fusion."
) 58 134 828 302 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Limitations" "What should not be over-claimed"
Add-Bullets $s @(
  "EPIC CEH/CEP and GEO CTH/CTP are not physically identical variables.",
  "The current quantitative comparison samples GEO lon-lat grid to EPIC pixels, not full EPIC-view radiative reprojection.",
  "Only March 2024 EPIC cloud samples are included in this stage; temporal robustness still needs expansion.",
  "Cloud-mask class semantics remain the central uncertainty, especially for confidence and partly cloudy categories.",
  "A high agreement number is not enough; F1, IoU, source boundaries, and failure maps are required."
) 58 134 828 302 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Next Steps" "A defensible path from prototype to stronger validation"
Add-Bullets $s @(
  "Implement cloud-mask fusion v2: consensus/probability-aware, with source uncertainty and boundary uncertainty.",
  "Run a larger EPIC monthly sample set with balanced source-dominant and mixed regimes.",
  "Add stricter EPIC-view reprojection for selected samples to quantify sampling and parallax effects.",
  "Separate scientific diagnostics from production fusion: EPIC tunes assumptions but does not become the product input.",
  "Use Stage 07 overlap validation and Stage 08 EPIC validation together as complementary evidence."
) 58 134 828 302 20 | Out-Null
Add-Foot $s $n

$n++; $s = $pres.Slides.Add($n, $blankLayout)
Add-Title $s "Take-home Message" "What I would tell the group"
Add-TextBox $s "Stage 08 converts a visual question into a reproducible validation framework." 68 150 812 48 26 $true | Out-Null
Add-Bullets $s @(
  "The result is not simply good or bad; it is structured by cloud-mask semantics, viewing geometry, and source family.",
  "The current GEO-ring prototype is credible as a v0/v1 engineering chain, but its cloud-mask fusion needs a v2 strategy.",
  "The strongest contribution is not one metric; it is the diagnostic framework that identifies where and why the product differs from EPIC."
) 78 232 790 180 20 | Out-Null
Add-Foot $s $n

$pres.SaveAs($pptxPath)
try {
  $pres.SaveAs($pdfPath, 32)
} catch {
  Write-Host "PDF export skipped: $($_.Exception.Message)"
}
$pres.Close()
$pp.Quit()

$notes = @"
# Stage 08 EPIC group-meeting speaker notes

Use these notes with the PPT.

1. The central question is not whether EPIC is truth. It is whether an independent L1-view cloud product sees broadly similar cloud structures and where the GEO-ring product disagrees.
2. Emphasize the engineering work: parameterized 08c/08e/08f/08g pipeline, monthly EPIC sample selection, semantic modes, geometry stratification, pre-fusion source diagnostics, and summary plots.
3. Explain the comparison geometry carefully: GEO-ring is sampled from the 0.05 degree lon-lat grid to EPIC L2 lat/lon points. This is statistically meaningful but not a full EPIC-view reprojection.
4. The most important result is source-dependent behavior. GOES and FY4B/Himawari regimes are reasonable; Meteosat cloud mask is weak and should be conservative.
5. Mode B improving over Mode A means semantic uncertainty matters. The apparent disagreement is partly about category definitions and confidence levels.
6. High latitude and EPIC edge pixels degrade agreement, but they are not enough to explain the source-specific performance gap.
7. Do not propose using EPIC directly in fusion. Use EPIC as an external diagnostic and tuning reference.
8. The next method step is cloud-mask fusion v2: consensus/probability-aware selection, conservative Meteosat weighting, and boundary uncertainty maps.
"@
Set-Content -LiteralPath $notesPath -Value $notes -Encoding UTF8

Write-Host "PPTX=$pptxPath"
Write-Host "PDF=$pdfPath"
Write-Host "NOTES=$notesPath"
