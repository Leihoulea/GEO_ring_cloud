param(
  [string]$SummaryRoot = "D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_multisample_summary",
  [string]$SlideJson = "D:\AAAresearch_paper\third_report\code\geo_ring_cloud_stage1\stage08_epic_group_meeting_slides_cn.json",
  [string]$OutDir = "D:\AAAresearch_paper\geo_ring_cloud_stage1_time_runs\epic_202403_multisample_summary\ppt_group_meeting"
)

$ErrorActionPreference = "Stop"

function Ensure-Dir([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
  }
}

function Add-TextBox($slide, [string]$Text, [single]$Left, [single]$Top, [single]$Width, [single]$Height, [single]$FontSize = 20, [bool]$Bold = $false) {
  $shape = $slide.Shapes.AddTextbox(1, $Left, $Top, $Width, $Height)
  $shape.TextFrame.TextRange.Text = $Text
  $shape.TextFrame.TextRange.Font.Size = $FontSize
  $shape.TextFrame.TextRange.Font.NameFarEast = "Microsoft YaHei"
  $shape.TextFrame.TextRange.Font.Name = "Microsoft YaHei"
  $shape.TextFrame.TextRange.Font.Bold = [int]$Bold
  $shape.TextFrame.WordWrap = -1
  return $shape
}

function Add-Title($slide, [string]$Title, [string]$SubTitle = "") {
  Add-TextBox $slide $Title 36 22 888 58 25 $true | Out-Null
  if ($SubTitle.Length -gt 0) {
    Add-TextBox $slide $SubTitle 38 78 872 28 12 $false | Out-Null
  }
  $line = $slide.Shapes.AddShape(1, 36, 112, 888, 2)
  $line.Fill.ForeColor.RGB = 0x666666
  $line.Line.Visible = 0
}

function Add-Bullets($slide, $Items, [single]$Left, [single]$Top, [single]$Width, [single]$Height, [single]$FontSize = 17) {
  $parts = @()
  foreach ($item in $Items) { $parts += ("- " + [string]$item) }
  $text = $parts -join "`r`n"
  $shape = Add-TextBox $slide $text $Left $Top $Width $Height $FontSize $false
  $shape.TextFrame.TextRange.ParagraphFormat.SpaceAfter = 4
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

function Resolve-Img([string]$Root, [string]$Rel) {
  if ([System.IO.Path]::IsPathRooted($Rel)) { return $Rel }
  return (Join-Path $Root $Rel)
}

function Add-Foot($slide, [int]$Number) {
  Add-TextBox $slide ("Stage 08 EPIC comparison | " + $Number) 700 512 230 18 9 $false | Out-Null
}

Ensure-Dir $OutDir

$pptxPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting_CN.pptx"
$pdfPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_group_meeting_CN.pdf"
$notesPath = Join-Path $OutDir "Stage08_EPIC_GEO_ring_cloud_comparison_CN_readme.md"

$slides = Get-Content -LiteralPath $SlideJson -Encoding UTF8 -Raw | ConvertFrom-Json

$pp = New-Object -ComObject PowerPoint.Application
$pp.Visible = -1
$pres = $pp.Presentations.Add()
$pres.PageSetup.SlideWidth = 960
$pres.PageSetup.SlideHeight = 540
$blankLayout = 12
$n = 0

foreach ($spec in $slides) {
  $n += 1
  $s = $pres.Slides.Add($n, $blankLayout)
  if ($spec.kind -eq "title") {
    Add-TextBox $s $spec.title 48 62 860 70 29 $true | Out-Null
    Add-TextBox $s $spec.subtitle 52 142 830 34 16 $false | Out-Null
    Add-Bullets $s $spec.bullets 68 238 790 170 18 | Out-Null
  } elseif ($spec.kind -eq "bullets") {
    Add-Title $s $spec.title $spec.subtitle
    Add-Bullets $s $spec.bullets 58 140 830 310 19 | Out-Null
  } elseif ($spec.kind -eq "image_bullets") {
    Add-Title $s $spec.title $spec.subtitle
    Add-ImageFit $s (Resolve-Img $SummaryRoot $spec.image) 56 132 405 318 | Out-Null
    Add-Bullets $s $spec.bullets 500 136 390 300 15 | Out-Null
  } elseif ($spec.kind -eq "image_only") {
    Add-Title $s $spec.title $spec.subtitle
    Add-ImageFit $s (Resolve-Img $SummaryRoot $spec.image) 68 128 804 318 | Out-Null
    if ($spec.note) { Add-TextBox $s $spec.note 86 466 760 36 12 $false | Out-Null }
  } elseif ($spec.kind -eq "triptych") {
    Add-Title $s $spec.title $spec.subtitle
    Add-ImageFit $s (Resolve-Img $SummaryRoot $spec.images[0]) 28 132 295 318 | Out-Null
    Add-ImageFit $s (Resolve-Img $SummaryRoot $spec.images[1]) 332 132 295 318 | Out-Null
    Add-ImageFit $s (Resolve-Img $SummaryRoot $spec.images[2]) 636 132 295 318 | Out-Null
  } else {
    Add-Title $s $spec.title $spec.subtitle
    Add-Bullets $s $spec.bullets 58 140 830 310 18 | Out-Null
  }
  Add-Foot $s $n
}

$pres.SaveAs($pptxPath)
try {
  $pres.SaveAs($pdfPath, 32)
} catch {
  Write-Host "PDF export skipped: $($_.Exception.Message)"
}
$pres.Close()
$pp.Quit()

$readme = @"
# Stage 08 EPIC group meeting PPT

Generated files:

- $pptxPath
- $pdfPath

Source slide JSON:

- $SlideJson

The deck focuses on Stage 08 only: EPIC L2 cloud comparison, cloud-mask semantics, multi-sample results, geometry diagnostics, pre-fusion source diagnostics, and fusion-v2 implications.
"@
Set-Content -LiteralPath $notesPath -Value $readme -Encoding UTF8

Write-Host "PPTX=$pptxPath"
Write-Host "PDF=$pdfPath"
Write-Host "README=$notesPath"
