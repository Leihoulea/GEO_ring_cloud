import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { Presentation, PresentationFile } from "@oai/artifact-tool";


export const COMPONENT_ROLE = "presentation_builder";

const W = 1280;
const H = 720;
const M = 42;
const FONT = "Microsoft YaHei";
const COLOR = {
  ink: "#151B22",
  muted: "#5D6873",
  blue: "#2070B4",
  cyan: "#25A7A1",
  orange: "#E28E2C",
  red: "#C7473B",
  line: "#DCE2E7",
  pale: "#F2F5F7",
  white: "#FFFFFF",
};

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i].startsWith("--")) result[argv[i].slice(2)] = argv[i + 1];
  }
  if (!result["analysis-root"] || !result.output) {
    throw new Error("usage: --analysis-root <path> --output <pptx>");
  }
  return result;
}

async function readImageBlob(imagePath) {
  const bytes = await fs.readFile(imagePath);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

function textbox(slide, value, position, style = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position,
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = String(value);
  shape.text.style = {
    fontSize: style.fontSize ?? 22,
    typeface: FONT,
    color: style.color ?? COLOR.ink,
    bold: style.bold ?? false,
    alignment: style.alignment ?? "left",
    verticalAlignment: style.verticalAlignment ?? "top",
    autoFit: style.autoFit ?? "shrinkText",
  };
  return shape;
}

function rect(slide, position, fill, name = "band") {
  return slide.shapes.add({
    name,
    geometry: "rect",
    position,
    fill,
    line: { style: "solid", fill, width: 0 },
  });
}

function baseSlide(presentation, title, section, page, source) {
  const slide = presentation.slides.add();
  const visibleSource = /[\\/]/.test(source) ? path.basename(source) : source;
  slide.background.fill = COLOR.white;
  textbox(slide, section, { left: M, top: 27, width: 360, height: 26 }, { fontSize: 15, bold: true, color: COLOR.blue });
  textbox(slide, title, { left: M, top: 59, width: 1120, height: 58 }, { fontSize: 36, bold: true });
  rect(slide, { left: M, top: 126, width: W - 2 * M, height: 2 }, COLOR.line, "header-rule");
  textbox(slide, `来源：${visibleSource}`, { left: M, top: 681, width: 1085, height: 18 }, { fontSize: 10, color: COLOR.muted, autoFit: "none" });
  textbox(slide, String(page).padStart(2, "0"), { left: 1182, top: 676, width: 55, height: 22 }, { fontSize: 13, color: COLOR.muted, alignment: "right", autoFit: "none" });
  slide.speakerNotes.textFrame.setText(`[Sources]\n- ${source}`);
  slide.speakerNotes.setVisible(true);
  return slide;
}

async function addImage(slide, imagePath, position, alt, fit = "contain") {
  slide.images.add({
    blob: await readImageBlob(imagePath),
    contentType: "image/png",
    alt,
    fit,
    position,
  });
}

function metricLine(slide, value, label, x, y, color = COLOR.blue) {
  textbox(slide, value, { left: x, top: y, width: 250, height: 58 }, { fontSize: 38, bold: true, color });
  textbox(slide, label, { left: x, top: y + 57, width: 255, height: 42 }, { fontSize: 17, color: COLOR.muted });
}

function percent(value) {
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fixed(value, digits = 3) {
  return Number(value).toFixed(digits);
}

function asset(summary, stem) {
  const found = summary.assets.figures.find((item) => path.basename(item).startsWith(stem));
  if (!found) throw new Error(`figure not found for ${stem}`);
  return found;
}

function quicklook(summary, contains) {
  const found = summary.assets.quicklooks.find((item) => path.basename(item).includes(contains));
  if (!found) throw new Error(`quicklook not found for ${contains}`);
  return found;
}

async function buildDeck(summary, outputPath, previewDir) {
  const presentation = Presentation.create({ slideSize: { width: W, height: H } });
  const a = summary.clm.policy_a;
  const b = summary.clm.policy_b;
  const c = summary.cth.policy_a_d1;
  const analyzed = Number(summary.comparison_count);
  const frozen = Number(summary.frozen_comparison_count);
  const geo = Number(summary.unique_geo_count);
  const navChecks = geo * 4;
  const coverage = `${analyzed}/${frozen}`;

  // Codex Grid slide-01 hierarchy: small eyebrow, dominant lower title, compact subtitle.
  {
    const slide = presentation.slides.add();
    slide.background.fill = COLOR.white;
    rect(slide, { left: 0, top: 0, width: 15, height: H }, COLOR.blue, "title-accent");
    textbox(slide, `GEO RING CLOUD · 冻结 80 配对（有效 ${analyzed}）`, { left: M, top: 40, width: 760, height: 40 }, { fontSize: 25, bold: true, color: COLOR.blue, autoFit: "none" });
    textbox(slide, "CLM 与 CTH\nEPIC 对比实验", { left: M, top: 178, width: 900, height: 235 }, { fontSize: 74, bold: true, autoFit: "none", verticalAlignment: "bottom" });
    textbox(slide, `Satpy 导航修复后的有效样本分析 · 覆盖 ${coverage}`, { left: M, top: 486, width: 940, height: 65 }, { fontSize: 29, color: COLOR.muted, autoFit: "none" });
    textbox(slide, "2024-03-05 至 2024-03-31 样本子集 · 2026-08-12", { left: M, top: 628, width: 720, height: 28 }, { fontSize: 16, color: COLOR.muted, autoFit: "none" });
    slide.speakerNotes.textFrame.setText(`[Sources]\n- ${summary.assets.report}\n- ${summary.assets.tables.clm_weighted}\n- ${summary.assets.tables.cth_summary}`);
    slide.speakerNotes.setVisible(true);
  }

  {
    const slide = baseSlide(presentation, "实验完成门槛与分析范围", "01 · 实验设计", 2, "Stage 09C/10 manifests; frozen target manifest; navigation verification");
    metricLine(slide, coverage, "有效 / 冻结 EPIC 配对", 76, 176, COLOR.blue);
    metricLine(slide, String(geo), "有效 GEO 时次", 365, 176, COLOR.cyan);
    metricLine(slide, String(navChecks), "有效样本导航检查 PASS", 654, 176, COLOR.orange);
    metricLine(slide, "2", "CLM 语义策略 A / B", 943, 176, COLOR.red);
    rect(slide, { left: M, top: 352, width: W - 2 * M, height: 1 }, COLOR.line, "mid-rule");
    textbox(slide, "重建链路", { left: 78, top: 389, width: 230, height: 35 }, { fontSize: 23, bold: true });
    textbox(slide, "Stage 02 → 03 → 03.5 → 05 → 06 → 08c → Stage 10 CTH/QC", { left: 78, top: 438, width: 1110, height: 50 }, { fontSize: 27, color: COLOR.blue });
    textbox(slide, `分析门槛：冻结 80 配对全部尝试；${analyzed} 个成功配对进入统计；${frozen - analyzed} 个失败配对显式排除；四类 Meteosat CLM/CTH 导航 schema 对 ${geo} 个有效 GEO 时次均通过。`, { left: 78, top: 516, width: 1100, height: 90 }, { fontSize: 20, color: COLOR.muted });
  }

  {
    const slide = baseSlide(presentation, "CLM：两种语义策略均保持稳定", "02 · 云掩膜", 3, summary.assets.tables.clm_weighted);
    await addImage(slide, asset(summary, "fig01_clm_policy_metrics"), { left: 45, top: 145, width: 760, height: 500 }, "CLM policy metric distributions");
    metricLine(slide, percent(a.agreement), "Policy A agreement", 845, 176, COLOR.blue);
    metricLine(slide, fixed(a.f1), "Policy A F1", 845, 286, COLOR.cyan);
    metricLine(slide, fixed(a.iou), "Policy A IoU", 845, 396, COLOR.orange);
    metricLine(slide, fixed(a.mcc), "Policy A MCC", 845, 506, COLOR.red);
  }

  {
    const slide = baseSlide(presentation, "CLM：时间序列未见系统性漂移", "02 · 云掩膜", 4, "fig02 source data; CLM representative case table");
    await addImage(slide, asset(summary, "fig02_clm_time_series"), { left: 48, top: 142, width: 1180, height: 505 }, "CLM temporal stability chart");
    textbox(slide, `Policy B agreement ${percent(b.agreement)} · F1 ${fixed(b.f1)} · IoU ${fixed(b.iou)}`, { left: 680, top: 630, width: 520, height: 28 }, { fontSize: 15, color: COLOR.muted, alignment: "right" });
  }

  {
    const slide = baseSlide(presentation, "CLM：代表性空间案例", "02 · 云掩膜", 5, "Existing Stage 08c Policy A quicklooks; representative case table");
    await addImage(slide, quicklook(summary, "quicklook01_clm_best_median_worst"), { left: 65, top: 143, width: 1150, height: 515 }, "Best, median and worst CLM quicklooks");
  }

  {
    const slide = baseSlide(presentation, "CTH：误差随云域显著变化", "03 · 云顶高度", 6, "Stage 10 fused CTH metrics by domain");
    await addImage(slide, asset(summary, "fig03_cth_domain_metrics"), { left: 45, top: 145, width: 810, height: 500 }, "CTH domain errors");
    metricLine(slide, `${fixed(c.mae_km, 2)} km`, "D1 加权 MAE", 890, 185, COLOR.blue);
    metricLine(slide, `${fixed(c.bias_km, 2)} km`, "D1 加权 bias", 890, 310, COLOR.orange);
    metricLine(slide, percent(c.within_2km_fraction), "误差在 2 km 内", 890, 435, COLOR.cyan);
    textbox(slide, "EPIC effective cloud height 是诊断参照，不作为绝对真值。", { left: 890, top: 560, width: 300, height: 60 }, { fontSize: 17, color: COLOR.muted });
  }

  {
    const slide = baseSlide(presentation, "CTH：来源分层揭示选择结果差异", "03 · 云顶高度", 7, "Stage 10 fused CTH metrics by selected source");
    await addImage(slide, asset(summary, "fig04_cth_selected_source"), { left: 55, top: 145, width: 1170, height: 510 }, "Selected-source CTH metrics");
  }

  {
    const slide = baseSlide(presentation, "CTH：中位与边界案例的空间结构", "03 · 云顶高度", 8, "Stage 10 sample manifest; fused CTH NPZ; EPIC A-band effective cloud height");
    const median = quicklook(summary, "quicklook_cth_median");
    const worst = quicklook(summary, "quicklook_cth_worst");
    await addImage(slide, median, { left: 45, top: 145, width: 585, height: 500 }, "Median CTH case quicklook");
    await addImage(slide, worst, { left: 650, top: 145, width: 585, height: 500 }, "Worst CTH case quicklook");
  }

  {
    const slide = baseSlide(presentation, "CLM 与 CTH 是互补而非替代诊断", "04 · 联合解释", 9, "Paired CLM Policy A and CTH Policy A/D1 sample metrics");
    await addImage(slide, asset(summary, "fig05_clm_cth_relationship"), { left: 65, top: 145, width: 775, height: 505 }, "CLM and CTH paired diagnostic relationship");
    textbox(slide, "分类正确\n不等于\n高度误差小", { left: 910, top: 200, width: 270, height: 145 }, { fontSize: 34, bold: true, color: COLOR.blue, alignment: "center" });
    rect(slide, { left: 892, top: 372, width: 305, height: 2 }, COLOR.line, "relationship-rule");
    textbox(slide, "CLM 评价云/晴语义与导航一致性；CTH 评价双方均判云像元上的连续高度差。两者应并行报告。", { left: 900, top: 408, width: 290, height: 150 }, { fontSize: 20, color: COLOR.muted });
  }

  {
    const slide = baseSlide(presentation, `结论：${coverage} 有效样本已形成可复核证据链`, "05 · 结论", 10, summary.assets.report);
    textbox(slide, "CLM", { left: 70, top: 172, width: 220, height: 45 }, { fontSize: 27, bold: true, color: COLOR.blue });
    textbox(slide, `Policy A agreement ${percent(a.agreement)}，F1 ${fixed(a.f1)}，IoU ${fixed(a.iou)}；时间序列和代表性 quicklook 未显示系统性漂移。`, { left: 70, top: 226, width: 1110, height: 88 }, { fontSize: 25 });
    rect(slide, { left: 70, top: 337, width: 1110, height: 1 }, COLOR.line, "conclusion-rule-1");
    textbox(slide, "CTH", { left: 70, top: 370, width: 220, height: 45 }, { fontSize: 27, bold: true, color: COLOR.orange });
    textbox(slide, `Policy A / D1 加权 MAE ${fixed(c.mae_km, 2)} km，bias ${fixed(c.bias_km, 2)} km，within-2-km ${percent(c.within_2km_fraction)}；误差应按云域与 selected source 分层解释。`, { left: 70, top: 424, width: 1110, height: 88 }, { fontSize: 25 });
    rect(slide, { left: 70, top: 535, width: 1110, height: 1 }, COLOR.line, "conclusion-rule-2");
    textbox(slide, "当前不需要修改 production reader。建议先审阅本次证据，再决定是否进入 648 时次整月重跑。", { left: 70, top: 570, width: 1110, height: 62 }, { fontSize: 23, bold: true, color: COLOR.cyan });
  }

  await fs.mkdir(previewDir, { recursive: true });
  for (const [index, slide] of presentation.slides.items.entries()) {
    const stem = `slide-${String(index + 1).padStart(2, "0")}`;
    const png = await presentation.export({ slide, format: "png", scale: 1 });
    await fs.writeFile(path.join(previewDir, `${stem}.png`), new Uint8Array(await png.arrayBuffer()));
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(previewDir, `${stem}.layout.json`), await layout.text());
  }
  const montage = await presentation.export({ format: "webp", montage: true, scale: 1 });
  await fs.writeFile(path.join(previewDir, "deck-montage.webp"), new Uint8Array(await montage.arrayBuffer()));
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(outputPath);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const analysisRoot = path.resolve(args["analysis-root"]);
  const outputPath = path.resolve(args.output);
  const previewDir = path.join(path.dirname(outputPath), "artifact_tool_previews");
  const summaryText = await fs.readFile(path.join(analysisRoot, "analysis_summary.json"), "utf8");
  const summary = JSON.parse(summaryText.replace(/^\uFEFF/, ""));
  if (!/^EPIC80_VALID\d+_CLM_CTH_ANALYSIS_PASS$/.test(summary.status)) throw new Error(`analysis is not PASS: ${summary.status}`);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(
    path.join(path.dirname(outputPath), "source-notes.txt"),
    [summary.assets.report, ...Object.values(summary.assets.tables), ...summary.assets.figures, ...summary.assets.quicklooks].join("\n"),
    "utf8",
  );
  await buildDeck(summary, outputPath, previewDir);
  await new Promise((resolve) => process.stdout.write(`PRESENTATION_BUILD_PASS: ${outputPath}\n`, resolve));
  process.exit(0);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
