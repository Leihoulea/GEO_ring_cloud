import fs from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const COMPONENT_ROLE = "presentation_builder";
const RELATED_STAGE_IDS = ["stage_00d", "stage_09d", "stage_09e", "stage_09f", "stage_10"];
const ROOT = process.env.GEO_RING_PROJECT_ROOT
  ? path.resolve(process.env.GEO_RING_PROJECT_ROOT)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../../../");
const DEFAULT_RUN_ID = "geo_ring_cloud_group_meeting_20260720_evidence_audited";

const COLORS = {
  ink: "#132238",
  muted: "#5F6B7A",
  rule: "#C9D2DB",
  paper: "#FBFCFD",
  teal: "#138A84",
  blue: "#2E64A7",
  red: "#C04C48",
  amber: "#D59637",
  softTeal: "#E4F3F1",
  softBlue: "#E8F0FA",
  softRed: "#F8E8E7",
  softAmber: "#FBF1DF",
  pale: "#F2F5F7",
};

const assets = {
  stage09Main: ["geo_ring_cloud_stage1_time_runs", "stage_09e_nature_meeting_figures_202403", "figures", "stage_09e_nature_meeting_figures_202403_figure1_main_diagnostic_story.png"],
  stage09Source: ["geo_ring_cloud_stage1_time_runs", "stage_09e_nature_meeting_figures_202403", "figures", "stage_09e_nature_meeting_figures_202403_figure3_source_family_pair_evidence.png"],
  stage09Case: ["geo_ring_cloud_stage1_time_runs", "stage_09f_spatial_story_maps_202403", "figures", "stage_09f_spatial_story_maps_202403_figure1_case_20240308_0400.png"],
  stage10Main: ["geo_ring_cloud_stage1_time_runs", "stage_10_meeting_figures_202403", "figures", "stage_10_group_meeting_fig02_fused_cth_main_metrics.png"],
  stage10Source: ["geo_ring_cloud_stage1_time_runs", "stage_10_meeting_figures_202403", "figures", "stage_10_group_meeting_fig03_source_error_decomposition.png"],
  stage10Boundary: ["geo_ring_cloud_stage1_time_runs", "stage_10_meeting_figures_202403", "figures", "stage_10_group_meeting_fig05_regret_high_cloud_boundary.png"],
};

const evidenceSources = {
  stage09Bootstrap: ["geo_ring_cloud_stage1_time_runs", "stage_09d_claas3_aligned_202403", "claas3_stage0910_march_202403", "stage_09d_claas3_aligned_bootstrap_summary.csv"],
  stage10Bootstrap: ["geo_ring_cloud_stage1_time_runs", "stage_10_claas3_aligned_202403", "claas3_stage0910_march_202403", "stage_10_claas3_aligned_bootstrap_summary.csv"],
};

function assetPath(parts) {
  return path.join(ROOT, ...parts);
}

function relativePath(value) {
  return path.relative(ROOT, value).replaceAll("\\", "/");
}

async function imageBytes(filePath) {
  const bytes = await fs.readFile(filePath);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

async function writeBlob(filePath, blob) {
  await fs.writeFile(filePath, new Uint8Array(await blob.arrayBuffer()));
}

async function readSimpleCsv(filePath) {
  const text = await fs.readFile(filePath, "utf8");
  const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
  const headers = lines.shift().split(",");
  return lines.map((line) => Object.fromEntries(headers.map((header, index) => [header, line.split(",")[index] ?? ""])));
}

function oneRow(rows, criteria, label) {
  const matches = rows.filter((row) => Object.entries(criteria).every(([key, value]) => row[key] === value));
  if (matches.length !== 1) {
    throw new Error(`${label}: expected one CSV row, found ${matches.length}`);
  }
  return matches[0];
}

function finiteMetric(row, key, label) {
  const value = Number(row[key]);
  if (!Number.isFinite(value)) throw new Error(`${label}: invalid ${key}=${row[key]}`);
  return value;
}

async function loadEvidence() {
  const stage09Rows = await readSimpleCsv(assetPath(evidenceSources.stage09Bootstrap));
  const stage10Rows = await readSimpleCsv(assetPath(evidenceSources.stage10Bootstrap));
  const stage09Criteria = {
    domain: "replacement_active",
    policy: "A_inclusive_binary",
    stratum: "source_transition:5->7",
    diagnostic_type: "fused_profile",
  };
  const stage10Criteria = {
    aggregation: "box_7x7",
    policy: "A_inclusive_binary",
    stratum: "all",
    diagnostic_type: "fused_profile",
  };
  const stage09 = {
    main: oneRow(stage09Rows, { ...stage09Criteria, aggregation: "box_7x7" }, "Stage 09 replacement-active"),
    control: oneRow(stage09Rows, {
      aggregation: "box_7x7", domain: "unchanged_control", policy: "A_inclusive_binary", stratum: "all", diagnostic_type: "fused_profile",
    }, "Stage 09 unchanged control"),
    aggregation: ["nearest", "box_3x3", "box_5x5", "box_7x7"].map((aggregation) => oneRow(stage09Rows, { ...stage09Criteria, aggregation }, `Stage 09 ${aggregation}`)),
    boundary: oneRow(stage09Rows, {
      aggregation: "box_7x7", domain: "replacement_active", policy: "A_inclusive_binary", stratum: "boundary_class:near_boundary_1cell", diagnostic_type: "fused_profile",
    }, "Stage 09 boundary"),
    nonBoundary: oneRow(stage09Rows, {
      aggregation: "box_7x7", domain: "replacement_active", policy: "A_inclusive_binary", stratum: "boundary_class:non_boundary", diagnostic_type: "fused_profile",
    }, "Stage 09 non-boundary"),
  };
  const stage10Row = (domain, band) => oneRow(stage10Rows, { ...stage10Criteria, domain, band }, `Stage 10 ${domain} ${band}`);
  return {
    stage09,
    stage10: {
      d0: [stage10Row("D0_common_valid_cth", "A_band"), stage10Row("D0_common_valid_cth", "B_band")],
      d1: [stage10Row("D1_both_cloud", "A_band"), stage10Row("D1_both_cloud", "B_band")],
      d6: [stage10Row("D6_boundary_or_broken_cloud", "A_band"), stage10Row("D6_boundary_or_broken_cloud", "B_band")],
      d7: [stage10Row("D7_high_cloud", "A_band"), stage10Row("D7_high_cloud", "B_band")],
    },
  };
}

function addText(slide, value, x, y, w, h, options = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: options.name ?? "text",
    position: { left: x, top: y, width: w, height: h },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = value;
  shape.text.style = {
    fontSize: options.fontSize ?? 24,
    typeface: options.typeface ?? "Microsoft YaHei",
    color: options.color ?? COLORS.ink,
    bold: options.bold ?? false,
    alignment: options.alignment ?? "left",
    verticalAlignment: options.verticalAlignment ?? "top",
    autoFit: "none",
    wrap: "square",
    insets: { top: 0, right: 0, bottom: 0, left: 0 },
  };
  return shape;
}

function addRect(slide, x, y, w, h, fill, options = {}) {
  return slide.shapes.add({
    geometry: options.geometry ?? "rect",
    name: options.name ?? "shape",
    position: { left: x, top: y, width: w, height: h },
    fill,
    line: { style: "solid", fill: options.line ?? fill, width: options.lineWidth ?? 0 },
    ...(options.borderRadius ? { borderRadius: options.borderRadius } : {}),
  });
}

function addLine(slide, x1, y1, x2, y2, color = COLORS.rule, width = 1) {
  const left = Math.min(x1, x2);
  const top = Math.min(y1, y2);
  return slide.shapes.add({
    geometry: "line",
    name: "rule",
    position: { left, top, width: Math.abs(x2 - x1), height: Math.abs(y2 - y1) },
    fill: "none",
    line: { style: "solid", fill: color, width },
  });
}

async function addImage(slide, filePath, x, y, w, h, alt, fit = "contain") {
  return slide.images.add({
    blob: await imageBytes(filePath),
    contentType: "image/png",
    alt,
    fit,
    position: { left: x, top: y, width: w, height: h },
    geometry: "rect",
  });
}

function addBullet(slide, value, x, y, width, accent = COLORS.teal) {
  addRect(slide, x, y + 8, 6, 6, accent, { geometry: "ellipse" });
  addText(slide, value, x + 18, y, width - 18, 34, { fontSize: 20, color: COLORS.ink });
}

function addMetric(slide, value, label, x, y, width, color) {
  addText(slide, value, x, y, width, 48, { fontSize: 34, color, bold: true, alignment: "center" });
  addText(slide, label, x, y + 52, width, 40, { fontSize: 16, color: COLORS.muted, alignment: "center" });
}

function addEffectForest(slide, rows, x, y, width, height, options = {}) {
  const labelWidth = options.labelWidth ?? 160;
  const values = rows.flatMap((row) => [row.low, row.high, 0]);
  const min = Math.min(...values, options.min ?? 0);
  const max = Math.max(...values, options.max ?? 0.01);
  const pad = (max - min) * 0.13 || 0.1;
  const lo = Math.min(min - pad, 0);
  const hi = max + pad;
  const plotX = x + labelWidth;
  const plotW = width - labelWidth - 22;
  const scale = (value) => plotX + ((value - lo) / (hi - lo)) * plotW;
  const zeroX = scale(0);
  addRect(slide, x, y, width, height, "#FFFFFF", { line: COLORS.rule, lineWidth: 1, borderRadius: "rounded-md" });
  addLine(slide, zeroX, y + 30, zeroX, y + height - 24, COLORS.muted, 1);
  addText(slide, options.axisLabel ?? "Delta", plotX, y + 8, plotW, 18, { fontSize: 12, color: COLORS.muted, alignment: "center" });
  const rowHeight = (height - 46) / rows.length;
  rows.forEach((row, index) => {
    const cy = y + 34 + rowHeight * index + rowHeight / 2;
    addText(slide, row.label, x + 14, cy - 13, labelWidth - 20, 26, { fontSize: 15, color: COLORS.ink, bold: index === 0 && Boolean(options.firstBold) });
    addLine(slide, scale(row.low), cy, scale(row.high), cy, row.color ?? COLORS.teal, 3);
    addLine(slide, scale(row.low), cy - 5, scale(row.low), cy + 5, row.color ?? COLORS.teal, 1.5);
    addLine(slide, scale(row.high), cy - 5, scale(row.high), cy + 5, row.color ?? COLORS.teal, 1.5);
    addRect(slide, scale(row.value) - 4, cy - 4, 8, 8, row.color ?? COLORS.teal, { geometry: "ellipse" });
    addText(slide, row.display ?? `${row.value.toFixed(3)} [${row.low.toFixed(3)}, ${row.high.toFixed(3)}]`, x + width - 125, cy - 13, 112, 26, { fontSize: 13, color: COLORS.muted, alignment: "right" });
  });
}

function addPairedMetricRows(slide, rows, x, y, width, options = {}) {
  const rowHeight = options.rowHeight ?? 86;
  const labelWidth = options.labelWidth ?? 150;
  const valueWidth = (width - labelWidth - 24) / 2;
  addText(slide, options.leftHeader ?? "operational", x + labelWidth, y, valueWidth, 20, { fontSize: 13, color: COLORS.blue, bold: true, alignment: "center" });
  addText(slide, options.rightHeader ?? "CLAAS-3", x + labelWidth + valueWidth + 12, y, valueWidth, 20, { fontSize: 13, color: COLORS.teal, bold: true, alignment: "center" });
  rows.forEach((row, index) => {
    const top = y + 28 + index * rowHeight;
    addText(slide, row.label, x, top + 16, labelWidth - 10, 42, { fontSize: 16, color: COLORS.ink, bold: true });
    addRect(slide, x + labelWidth, top, valueWidth, 58, COLORS.softBlue, { borderRadius: "rounded-md" });
    addRect(slide, x + labelWidth + valueWidth + 12, top, valueWidth, 58, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, row.left, x + labelWidth + 8, top + 17, valueWidth - 16, 25, { fontSize: 20, color: COLORS.blue, bold: true, alignment: "center" });
    addText(slide, row.right, x + labelWidth + valueWidth + 20, top + 17, valueWidth - 16, 25, { fontSize: 20, color: COLORS.teal, bold: true, alignment: "center" });
  });
}

function addFooter(slide, slideNo, source) {
  addLine(slide, 64, 677, 1216, 677, COLORS.rule, 1);
  addText(slide, source, 64, 688, 980, 18, { fontSize: 11, color: COLORS.muted });
  addText(slide, String(slideNo).padStart(2, "0"), 1175, 688, 40, 18, { fontSize: 11, color: COLORS.muted, alignment: "right" });
}

function makeSlide(deck, title, kicker, source, notes) {
  const slide = deck.slides.add();
  slide.background.fill = COLORS.paper;
  addText(slide, kicker, 64, 30, 360, 22, { fontSize: 13, color: COLORS.teal, bold: true });
  addText(slide, title, 64, 61, 1090, 52, { fontSize: 34, color: COLORS.ink, bold: true });
  addLine(slide, 64, 125, 1216, 125, COLORS.ink, 1.5);
  addFooter(slide, deck.slides.items.length, source);
  slide.speakerNotes.textFrame.setText(notes);
  slide.speakerNotes.setVisible(true);
  return slide;
}

function addPipelineNode(slide, label, sublabel, x, y, width, color) {
  addRect(slide, x, y, width, 76, "#FFFFFF", { line: color, lineWidth: 2, borderRadius: "rounded-md" });
  addText(slide, label, x + 12, y + 12, width - 24, 24, { fontSize: 18, color, bold: true, alignment: "center" });
  addText(slide, sublabel, x + 12, y + 42, width - 24, 22, { fontSize: 13, color: COLORS.muted, alignment: "center" });
}

function addDecisionRow(slide, variable, evidence, action, y, color) {
  addText(slide, variable, 92, y, 230, 32, { fontSize: 22, bold: true, color });
  addText(slide, evidence, 350, y, 470, 42, { fontSize: 18, color: COLORS.ink });
  addText(slide, action, 870, y, 280, 42, { fontSize: 18, bold: true, color });
  addLine(slide, 86, y + 56, 1168, y + 56, COLORS.rule, 1);
}

function getCommit() {
  try {
    return execFileSync("git", ["-C", ROOT, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  } catch {
    return "";
  }
}

async function writeSupportFiles(outputDir, finalPptx, allAssets) {
  const terms = `# 术语表\n\n| 规范术语 | 首次定义/使用规则 |\n| --- | --- |\n| GEO-ring Cloud | 本项目的多 GEO 卫星融合云产品。 |\n| operational Meteosat | 当前生产流程中的 Meteosat 处理流。 |\n| CLAAS-3 CMA | CLAAS-3 cloud mask。 |\n| CLAAS CTX | CLAAS-3 cloud-top height。 |\n| EPIC effective height | EPIC A/B-band 的有效高度；不等同于绝对云顶高。 |\n| EPIC-relative difference | 相对于 EPIC 的诊断差异，不表述为绝对真值误差。 |\n| common-valid domain | 两条 profile 同时有效的共同像元域。 |\n| replacement-active | baseline source 5 与 candidate source 7 真正发生替换的像元域。 |\n`;
  const ledger = `# 逐页证据账本（语义复核版）\n\n本账本区分 **operational 基线图** 与 **CLAAS-3 双轨证据**。任何 operational-only 图均不得支持 CLAAS-3 效应或接入结论。\n\n| 页码 | 页面角色 | 证据来源/图像实际对象 | 可支持的主张 | 不可支持的主张 | 状态 |\n| --- | --- | --- | --- | --- | --- |\n| 1 | framing | 无定量图 | 变量级决策问题 | 具体产品优劣 | 通过 |\n| 2 | decision summary | Stage 09d/10 双轨汇总 | CMA 与 CTX 方向不同 | CTX 绝对真值优劣 | 通过 |\n| 3 | framing | 比较设计 | 变量、区域、证据条件需分开 | 整套产品一刀切 | 通过 |\n| 4 | methods map | 项目处理链 | Stage 09/10 建立在既有链路上 | 任一结果数值 | 通过 |\n| 5 | motivation | CLAAS-3 产品边界 | 值得逐变量评估 | 自动精度优势 | 通过 |\n| 6 | scope | CLAAS-3 覆盖与数据契约 | 结论限共同覆盖范围 | 未覆盖区域外推 | 通过 |\n| 7 | methods | 双轨 manifest | 只替换 Meteosat-0deg 分支 | 其他输入发生变化 | 通过 |\n| 8 | methods | 53 登记时次/49 主分析时间块 | bootstrap 以时次为单位 | 像元独立样本推断 | 通过 |\n| 9 | operational baseline | Stage 09e 图：仅 operational Meteosat、VIS/PSF/source/scene | 替换前误差结构与诊断动机 | CLAAS-3 CMA 改善 | 已更正 |\n| 10 | CLAAS-3 dual track | Stage 09d bootstrap CSV：5→7 replacement-active、box 7×7、Policy A | CMA F1 0.612→0.857，delta +0.245 [0.229,0.260] | 其他云变量改善 | 已更正 |\n| 11 | CLAAS-3 dual track | Stage 09d bootstrap CSV：四种聚合核、边界分层 | 聚合核方向一致；近边界区间跨零 | Stage 09e 来源图证明 CLAAS 效应 | 已更正 |\n| 12 | operational baseline | Stage 09f case atlas：当前 operational mask/来源/边界 | 替换前空间结构的解释 | CLAAS-3 空间改善 | 已更正 |\n| 13 | bridge | 方法与定义 | CMA 不能外推 CTX | CMA 成功即 CTX 成功 | 通过 |\n| 14 | CLAAS-3 dual track | Stage 10 bootstrap CSV：D0、A/B bands、box 7×7、Policy A | A/B 相对 EPIC MAE 均增大 | 绝对 CTH 真值更差 | 已更正 |\n| 15 | CLAAS-3 dual track | Stage 10 bootstrap CSV：D1 both-cloud、A/B bands | 双方均判云域中差异仍为正 | prefusion 因果或绝对真值 | 已更正 |\n| 16 | CLAAS-3 dual track | Stage 10 bootstrap CSV：D6 boundary/broken、D7 high-cloud；D5 零样本 | 已展示分层相对差异；D5 unresolved | 绝对 CTH 或未展示高纬度分层外推 | 已更正 |\n| 17 | decision | 第 10–16 页的限定结论 | 混合 profile | 整套 CLAAS-3 切换 | 通过 |\n| 18 | reproducibility | manifests、CSV、case atlas、索引 | 可复跑和可追溯 | 科学结论自动成立 | 通过 |\n| 19 | next step | 当前决策矩阵 | CMA 回归、CTX 补独立垂直参考 | 永久性产品结论 | 通过 |\n| 20 | operational appendix | Stage 09e operational 图 | 替换前诊断 | CLAAS-3 比较结果 | 已更正 |\n| 21 | operational appendix | Stage 10 operational 来源图 | 替换前来源/场景结构 | CLAAS-3 双轨结论 | 已更正 |\n| 22 | operational appendix | Stage 09f operational case atlas | 替换前案例检查 | CLAAS-3 空间改善 | 已更正 |\n| 23 | methods appendix | 数据契约/运行状态 | 时间、空间和运行规则 | 超出契约的外推 | 通过 |\n| 24 | provenance appendix | registry、artifact index、run manifest | 追溯入口 | 数据本身的科学结论 | 通过 |\n`;
  const script = `# 30 分钟中文讲稿\n\n正文 19 页，建议 29–30 分钟；备份页仅用于问答。每页 PowerPoint 均嵌入相同的 speaker notes。本稿用于会前排练，数字和限定语以逐页证据账本为准。\n\n| 页码 | 建议时长 | 讲述要点 | 转场/边界 |\n| --- | ---: | --- | --- |\n| 1 | 0:30 | 本轮不是给 CLAAS-3 整体排名，而是回答每个变量是否能进入 GEO-ring Cloud。 | 先说明结论会按变量给出。 |\n| 2 | 1:10 | 先给结论：CMA 有支持接入的证据；CTX 暂无替代依据；其余变量尚未裁决。 | CTX 指标是 EPIC-relative，不是绝对 CTH 真值。 |\n| 3 | 1:20 | 决策对象是可追溯的变量级融合流程，必须同时说明区域、输入与证据条件。 | 不能用单一总体指标给产品族排名。 |\n| 4 | 1:20 | 回顾 GEO-ring Cloud：各 GEO 产品先标准化、重投影和源选择，再按变量融合。 | 后续比较只替换 Meteosat-0deg 分支。 |\n| 5 | 1:10 | CMA 与 CTX 的物理含义和评估参照不同，因此不能互相外推。 | CMA 的改善不证明 CTX 改善。 |\n| 6 | 1:10 | CLAAS-3 的产品边界、区域与时间覆盖决定了本轮只评价已共同覆盖的范围。 | 未覆盖区域不继承结论。 |\n| 7 | 1:40 | 双轨设计固定 EPIC、非 Meteosat 输入、聚合和统计，仅将 source 5 换成 source 7。 | unchanged control 是设计自检，应接近零差异。 |\n| 8 | 1:15 | 53 个时次均完成配对；主分析使用时间块 bootstrap 和共同有效域。 | 这里的样本数不是把像元当独立样本。 |\n| 9 | 1:30 | Stage 09 先建立诊断基线：VIS 与近似 PSF 聚合只能局部改善，一致性差异仍有来源和场景结构。 | 该页解释为何必须做后续双轨实验。 |\n| 10 | 2:10 | replacement-active 域中，CMA macro F1 由 0.612 增至 0.857，差值 +0.245，95% CI [0.230, 0.260]；控制域为零差异。 | 这是 CMA 的接入证据，不是所有云变量的通行证。 |\n| 11 | 1:20 | 来源族分解说明改善不是单一滤波器造成；空间和来源选择仍需保留。 | 近似矩形 FOV 不是官方 PSF。 |\n| 12 | 1:10 | 用自动登记的真实空间案例把总体统计落回云边界和来源切换。 | 案例用于解释，主结论仍来自全时次统计。 |\n| 13 | 0:55 | 转入 CTX 前重申：高度变量需要独立物理参照与独立判据。 | 不能沿用 CMA 的 F1 逻辑。 |\n| 14 | 2:10 | 相对于 EPIC A/B-band effective height，CLAAS CTX 的 MAE 均增大：A +0.234 km、B +0.457 km，区间均为正。 | 只可说相对 EPIC 偏离更大，不能说绝对云顶高更差。 |\n| 15 | 1:25 | 融合前和双方均判云控制域中，CTX 差异仍在；它不是融合后偶然产物。 | 此证据支持暂保留 operational CTX。 |\n| 16 | 1:15 | 高云、局部域与稀疏样本仍是未决边界，不能由总体方向覆盖。 | 需要独立垂直参考才可升级为绝对高度结论。 |\n| 17 | 1:40 | 最终是混合 profile：CMA 可进入生产回归；CTX 保留；其他变量逐项验证。 | 这是可执行的变量矩阵，不是产品标签。 |\n| 18 | 1:05 | 本轮也把比较变成可复现的工程对象：manifest、共同域定义、时间块统计和证据索引。 | 工程化的作用是让下一次变量验证可追溯。 |\n| 19 | 1:15 | 近期执行 CMA 回归；科学验证补 CALIOP/CloudSat/DARDAR；其余云变量逐项进入同一闭环。 | 收束为“有边界地接入”，不作整体替代。 |\n\n## 问答备份页\n\n- 20：数据契约与 53 时次配对规则。\n- 21：Stage 09 扩展诊断图。\n- 22：Stage 10 来源与分层图。\n- 23：运行、资产和证据索引。\n- 24：结论可说与不可说的边界。\n`;
  const correctedScript = script
    .replace("来源族分解说明改善不是单一滤波器造成；空间和来源选择仍需保留。 | 近似矩形 FOV 不是官方 PSF。", "四种聚合核的 CLAAS-3 双轨差值方向一致；近边界区间跨零。 | 仅使用 Stage 09d 双轨 CSV，不借用 operational 来源图。")
    .replace("用自动登记的真实空间案例把总体统计落回云边界和来源切换。 | 案例用于解释，主结论仍来自全时次统计。", "展示替换前 operational 的自动案例，解释边界与来源结构。 | 本页不含 CLAAS-3，不能用作替换效应证据。")
    .replace("相对于 EPIC A/B-band effective height，CLAAS CTX 的 MAE 均增大：A +0.234 km、B +0.457 km，区间均为正。 | 只可说相对 EPIC 偏离更大，不能说绝对云顶高更差。", "D0 common-valid CTH 中，CLAAS CTX 相对 EPIC 的 MAE 增量为 A +0.234 km、B +0.457 km，区间均为正。 | 只可说相对 EPIC 偏离更大，不能说绝对云顶高更差。")
    .replace("融合前和双方均判云控制域中，CTX 差异仍在；它不是融合后偶然产物。 | 此证据支持暂保留 operational CTX。", "在双方均判云的 D1 控制域，A/B 两波段增量仍为正。 | 不将此写成 prefusion 因果或绝对云高结论。")
    .replace("高云、局部域与稀疏样本仍是未决边界，不能由总体方向覆盖。 | 需要独立垂直参考才可升级为绝对高度结论。", "D6 边界/碎云与 D7 高云由双轨 CSV 重绘；D5 为零样本，保持 unresolved。 | 需要独立垂直参考才可升级为绝对高度结论。");
  const assetsText = allAssets.map((item) => `| ${item.label} | ${relativePath(item.path)} | ${item.slides.join(", ")} | 直接嵌入，保留原图全部必要图例与轴标签 |`).join("\n");
  const assetManifest = `# 图像资产清单\n\n| 资产 | 来源 | 使用页 | 处理 |\n| --- | --- | --- | --- |\n${assetsText}\n`;
  const manifest = {
    project_id: "geo_ring_cloud",
    canonical_stage_id: "",
    component_role: COMPONENT_ROLE,
    related_stage_ids: RELATED_STAGE_IDS,
    run_id: path.basename(outputDir),
    supersedes: "geo_ring_cloud_group_meeting_20260719",
    generating_script: relativePath(fileURLToPath(import.meta.url)),
    inputs: [
      ...allAssets.map((item) => relativePath(item.path)),
      relativePath(assetPath(evidenceSources.stage09Bootstrap)),
      relativePath(assetPath(evidenceSources.stage10Bootstrap)),
    ],
    outputs: ["group_meeting_30min_cn.pptx", "group_meeting_30min_cn.pdf", "speaker_script_cn.md", "terminology_ledger.md", "evidence_ledger.md", "asset_manifest.md", "qa_report.md"],
    parameter_summary: { slide_size: "16:9", main_slides: 19, appendix_slides: 5, language: "Chinese-first", evidence_mode: "operational baselines labelled separately; CLAAS-3 claims rendered from dual-track bootstrap CSV" },
    timestamp_utc: new Date().toISOString(),
    code_commit: getCommit(),
  };
  await fs.writeFile(path.join(outputDir, "terminology_ledger.md"), terms, "utf8");
  await fs.writeFile(path.join(outputDir, "evidence_ledger.md"), ledger, "utf8");
  await fs.writeFile(path.join(outputDir, "speaker_script_cn.md"), correctedScript, "utf8");
  await fs.writeFile(path.join(outputDir, "asset_manifest.md"), assetManifest, "utf8");
  await fs.writeFile(path.join(outputDir, "group_meeting_manifest.json"), JSON.stringify(manifest, null, 2), "utf8");
  await fs.writeFile(path.join(outputDir, "qa_report.md"), `# QA 报告\n\n- PPTX: ${path.basename(finalPptx)}\n- 结构：19 页正文 + 5 页备份页。\n- 证据：仅使用已登记的 Stage 00d/09d/09e/09f/10 运行产物。\n- 待补：由渲染、slides_test 与 PPTX 审计命令回填最终检查结果。\n`, "utf8");
}

export async function buildGroupMeetingDeck({ outputDir = path.join(ROOT, "geo_ring_cloud_stage1_time_runs", "presentations", DEFAULT_RUN_ID) } = {}) {
  await fs.mkdir(outputDir, { recursive: true });
  const deck = Presentation.create({ slideSize: { width: 1280, height: 720 } });
  const source = "数据：2024-03 已登记运行产物；见本次汇报 evidence ledger";
  const evidence = await loadEvidence();
  const stage09Main = assetPath(assets.stage09Main);
  const stage09Case = assetPath(assets.stage09Case);
  const stage10Source = assetPath(assets.stage10Source);
  const allAssets = [
    { label: "Stage 09 主诊断图", path: stage09Main, slides: [9, 20] },
    { label: "Stage 09 空间案例图", path: stage09Case, slides: [12, 22] },
    { label: "Stage 10 operational 来源诊断图", path: stage10Source, slides: [21] },
  ];

  // 1. Cover
  {
    const slide = deck.slides.add();
    slide.background.fill = COLORS.paper;
    addRect(slide, 0, 0, 24, 720, COLORS.teal);
    addText(slide, "Geo Ring Cloud", 78, 148, 620, 56, { fontSize: 48, bold: true, color: COLORS.ink });
    addText(slide, "从多源融合到 CLAAS-3 的变量级决策", 78, 218, 860, 70, { fontSize: 42, bold: true, color: COLORS.ink });
    addText(slide, "Stage 09 云掩膜诊断、Stage 10 有效高度诊断与双轨评估", 80, 314, 760, 36, { fontSize: 23, color: COLORS.muted });
    addLine(slide, 80, 395, 748, 395, COLORS.teal, 3);
    addText(slide, "2026 年 7 月组会 | 邓浩然", 80, 423, 460, 28, { fontSize: 18, color: COLORS.muted });
    addText(slide, "核心问题：不同变量能否由同一条新处理流整体替换？", 80, 558, 840, 42, { fontSize: 28, color: COLORS.teal, bold: true });
    addFooter(slide, 1, source);
    slide.speakerNotes.textFrame.setText("开场先限定范围：这是一个变量级的产品决策问题，而不是简单比较两个产品谁更好。后面所有结果都回到同一条闭环：比较设计是否公平、证据是否足以支持接入决策。");
    slide.speakerNotes.setVisible(true);
  }

  // 2. One-page outcome
  {
    const slide = makeSlide(deck, "云掩膜与云高不能按“整套产品”一起决策", "本轮结论", source, "用这一页建立全场导航。随后分别解释 CMA 和 CTX 为何方向不同，以及为什么控制域是因果归因的关键。");
    addMetric(slide, "53 / 53", "登记时次全部完成", 92, 195, 230, COLORS.teal);
    addMetric(slide, "49", "进入主分析的时次", 372, 195, 180, COLORS.blue);
    addMetric(slide, "+0.245", "CMA replacement 域 F1", 622, 195, 220, COLORS.teal);
    addMetric(slide, "+0.234 / +0.457 km", "CTX 相对 EPIC 的 MAE 差异", 894, 195, 285, COLORS.red);
    addRect(slide, 92, 372, 490, 144, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, "建议进入生产回归", 120, 397, 400, 30, { fontSize: 24, color: COLORS.teal, bold: true });
    addText(slide, "Meteosat-0deg 的 cloud mask 使用 CLAAS-3 CMA。", 120, 442, 410, 40, { fontSize: 20, color: COLORS.ink });
    addRect(slide, 636, 372, 505, 144, COLORS.softRed, { borderRadius: "rounded-md" });
    addText(slide, "暂不替换", 666, 397, 300, 30, { fontSize: 24, color: COLORS.red, bold: true });
    addText(slide, "cloud-top height 暂保留 operational CTH；结果仅是 EPIC-relative 诊断。", 666, 442, 430, 48, { fontSize: 20, color: COLORS.ink });
  }

  // 3. Question
  {
    const slide = makeSlide(deck, "研究对象聚焦为可追溯的变量级融合决策", "研究问题", source, "解释为什么不能用一个总体指标给整个产品族排名。变量、观测几何、来源和场景都不同，因此必须把问题拆成可审计的处理流比较。");
    addText(slide, "问题", 90, 184, 130, 30, { fontSize: 20, color: COLORS.teal, bold: true });
    addText(slide, "当 GEO-ring Cloud 引入 CLAAS-3 时，哪些变量、哪些区域、在什么证据条件下值得接入？", 90, 226, 940, 58, { fontSize: 29, bold: true });
    addText(slide, "约束", 90, 334, 130, 30, { fontSize: 20, color: COLORS.teal, bold: true });
    addBullet(slide, "比较同一 EPIC 时次、同一非 Meteosat 输入与同一空间聚合规则", 90, 378, 980);
    addBullet(slide, "只在共同有效像元域统计；以完整时次做 block bootstrap", 90, 426, 980);
    addBullet(slide, "将 CMA、CTX 与其他变量分开裁决，不继承彼此的结论", 90, 474, 980);
  }

  // 4. Work map
  {
    const slide = makeSlide(deck, "前期基础工作把“多源融合”变成可验证的处理链", "本轮工作地图", source, "这页只用来交代工作承接关系，不逐个复述历史 Stage。重点是前期已经解决了读数、几何、标准化和来源选择，Stage 09/10 才能回答产品决策问题。");
    const nodes = [
      ["数据审计", "产品结构、变量、几何", COLORS.blue],
      ["标准化", "统一坐标与语义", COLORS.teal],
      ["融合", "来源选择与追溯", COLORS.amber],
      ["Stage 09", "云掩膜诊断", COLORS.teal],
      ["Stage 10", "有效高度诊断", COLORS.red],
    ];
    nodes.forEach(([a, b, c], index) => {
      const x = 74 + index * 226;
      addPipelineNode(slide, a, b, x, 280, 174, c);
      if (index < nodes.length - 1) addLine(slide, x + 180, 318, x + 216, 318, COLORS.muted, 2);
    });
    addText(slide, "本轮新增：CLAAS-3 产品可读性审计、双轨 runner、共同域统计、来源分解、状态分层、case atlas 与 lineage manifest。", 90, 485, 1020, 48, { fontSize: 22, color: COLORS.ink });
  }

  // 5. Why CLAAS
  {
    const slide = makeSlide(deck, "CLAAS-3 的价值在于补充变量与长期一致性，而非自动获得精度优势", "为什么引入 CLAAS-3", source, "先把动机和结论分开：CLAAS-3 值得评估，因为它补足处理流和变量体系；但可用性、覆盖和相对一致性都必须逐项验证。");
    addRect(slide, 90, 185, 330, 310, COLORS.softBlue, { borderRadius: "rounded-md" });
    addText(slide, "潜在价值", 118, 215, 240, 30, { fontSize: 25, color: COLORS.blue, bold: true });
    addBullet(slide, "CMA、CTX 与多种云微物理变量", 118, 270, 250, COLORS.blue);
    addBullet(slide, "连续气候数据记录", 118, 324, 250, COLORS.blue);
    addBullet(slide, "变量级的候选处理流", 118, 378, 250, COLORS.blue);
    addRect(slide, 478, 185, 330, 310, COLORS.softAmber, { borderRadius: "rounded-md" });
    addText(slide, "必须先验证", 506, 215, 240, 30, { fontSize: 25, color: COLORS.amber, bold: true });
    addBullet(slide, "产品结构与时间覆盖", 506, 270, 250, COLORS.amber);
    addBullet(slide, "与现有流的可比性", 506, 324, 250, COLORS.amber);
    addBullet(slide, "不同变量的诊断结果", 506, 378, 250, COLORS.amber);
    addRect(slide, 866, 185, 270, 310, COLORS.softRed, { borderRadius: "rounded-md" });
    addText(slide, "不能直接推断", 894, 215, 210, 30, { fontSize: 25, color: COLORS.red, bold: true });
    addBullet(slide, "CMA 好，不等于 CTX 好", 894, 282, 210, COLORS.red);
    addBullet(slide, "EPIC 不是绝对 CTH 真值", 894, 352, 210, COLORS.red);
  }

  // 6. Product boundary
  {
    const slide = makeSlide(deck, "CLAAS-3 先作为可核验的数据处理流接入，而非替换整颗卫星", "产品与覆盖边界", source, "说明本次对象是 Meteosat-0deg 位置的处理流替换。这样可以避免把处理算法差异误写成卫星平台差异。");
    addText(slide, "共享部分", 92, 190, 210, 30, { fontSize: 22, color: COLORS.muted, bold: true });
    addRect(slide, 92, 236, 1060, 74, COLORS.pale, { borderRadius: "rounded-md" });
    addText(slide, "目标时次  |  EPIC 文件  |  非 Meteosat 输入  |  空间聚合  |  掩膜与共同域规则", 120, 260, 990, 28, { fontSize: 22, color: COLORS.ink, alignment: "center" });
    addRect(slide, 92, 365, 480, 148, COLORS.softBlue, { borderRadius: "rounded-md" });
    addText(slide, "Baseline profile", 122, 395, 400, 30, { fontSize: 25, color: COLORS.blue, bold: true });
    addText(slide, "operational CLM / operational CTH", 122, 442, 400, 28, { fontSize: 21, color: COLORS.ink });
    addRect(slide, 672, 365, 480, 148, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, "CLAAS-3 candidate profile", 702, 395, 400, 30, { fontSize: 25, color: COLORS.teal, bold: true });
    addText(slide, "CLAAS-3 CMA / CLAAS CTX", 702, 442, 400, 28, { fontSize: 21, color: COLORS.ink });
  }

  // 7. Dual track
  {
    const slide = makeSlide(deck, "双轨设计把产品差异限制在真正发生替换的 Meteosat-0deg 像元", "公平比较设计", source, "强调 replacement-active 域与 unchanged control 的角色。前者回答替换发生时结果如何，后者检查 runner、缓存和共同域计算没有人为制造差异。");
    addPipelineNode(slide, "共享输入", "EPIC + 非 Meteosat", 92, 215, 190, COLORS.muted);
    addPipelineNode(slide, "Baseline", "source 5", 380, 215, 190, COLORS.blue);
    addPipelineNode(slide, "Candidate", "source 7", 380, 415, 190, COLORS.teal);
    addPipelineNode(slide, "共同域", "same-pixel metrics", 670, 310, 210, COLORS.amber);
    addPipelineNode(slide, "变量级决策", "CMA / CTX", 990, 310, 190, COLORS.red);
    addLine(slide, 282, 253, 370, 253, COLORS.muted, 2);
    addLine(slide, 282, 253, 370, 453, COLORS.muted, 2);
    addLine(slide, 570, 253, 660, 348, COLORS.blue, 2);
    addLine(slide, 570, 453, 660, 348, COLORS.teal, 2);
    addLine(slide, 880, 348, 980, 348, COLORS.amber, 2);
    addText(slide, "replacement-active：source 5 → 7；unchanged control：应为 0 差异。", 92, 570, 940, 28, { fontSize: 21, color: COLORS.ink });
  }

  // 8. Sample/QC
  {
    const slide = makeSlide(deck, "样本完成度、时间门限和 bootstrap 单位在运行前锁定", "样本与质量规则", source, "讲清统计单位是完整时次而非数千万独立像元。49 个时次进入主统计，4 个留作时间偏移敏感性分析；这样避免样本量把微小差异夸大成结论。");
    addMetric(slide, "53", "登记时次", 104, 205, 180, COLORS.blue);
    addMetric(slide, "49", "主分析：时间差 ≤10 min", 358, 205, 230, COLORS.teal);
    addMetric(slide, "4", "仅作时间偏移敏感性", 664, 205, 220, COLORS.amber);
    addMetric(slide, "10,000", "whole-time-block bootstrap", 950, 205, 210, COLORS.red);
    addRect(slide, 100, 392, 1035, 112, COLORS.pale, { borderRadius: "rounded-md" });
    addText(slide, "统计规则", 130, 420, 120, 26, { fontSize: 20, color: COLORS.teal, bold: true });
    addText(slide, "每个时次等权；pooled-pixel 指标只作描述。共同域和状态分层均在比较前固定。", 282, 418, 760, 44, { fontSize: 23, color: COLORS.ink });
  }

  // 9. Stage09 baseline
  {
    const slide = makeSlide(deck, "Stage 09 先确认 operational 基线的误差结构：PSF-like 聚合不能消除来源与场景差异", "Stage 09：替换前基础诊断", "数据：Stage 09e operational baseline figures，2024-03；不含 CLAAS-3", "图中展示的是替换前 operational Meteosat 流的 VIS 控制、近似 PSF 聚合、边界像元与不同来源族；它不比较 CLAAS-3。读图目的只是说明为何需要后续双轨替换实验。");
    await addImage(slide, stage09Main, 68, 154, 850, 460, "Stage 09 主诊断图");
    addRect(slide, 957, 185, 224, 260, COLORS.softBlue, { borderRadius: "rounded-md" });
    addText(slide, "读图结论", 980, 212, 170, 26, { fontSize: 22, color: COLORS.blue, bold: true });
    addText(slide, "VIS 筛选与 PSF-like 聚合只能局部改善 operational 一致性。\n\n来源选择和观测场景仍是主要结构。", 980, 262, 170, 130, { fontSize: 19, color: COLORS.ink });
    addText(slide, "该图不含 CLAAS-3；只为双轨比较提供诊断基线。", 957, 490, 224, 72, { fontSize: 19, color: COLORS.muted });
  }

  // 10. CMA main
  {
    const main = evidence.stage09.main;
    const control = evidence.stage09.control;
    const mainA = finiteMetric(main, "equal_time_macro_A", "Stage 09 main A");
    const mainB = finiteMetric(main, "equal_time_macro_B", "Stage 09 main B");
    const mainDelta = finiteMetric(main, "equal_time_macro_B_minus_A", "Stage 09 main delta");
    const mainLow = finiteMetric(main, "bootstrap_ci95_low", "Stage 09 main CI low");
    const mainHigh = finiteMetric(main, "bootstrap_ci95_high", "Stage 09 main CI high");
    const controlA = finiteMetric(control, "equal_time_macro_A", "Stage 09 control A");
    const slide = makeSlide(deck, "在真正发生替换的共同域内，CLAAS-3 CMA 的相对一致性提高", "Stage 09：CMA 主结果", "数据：Stage 09d CLAAS-3 March 双轨 bootstrap CSV；Policy A，box 7×7", "范围必须说完整：Meteosat-0deg source transition 5→7 的 replacement-active 域，41 个有效时间块。图中数值由 Stage 09d bootstrap CSV 在构建时直接读取；CMA 的改善不延伸到其他变量。");
    addMetric(slide, `${mainA.toFixed(3)} → ${mainB.toFixed(3)}`, "replacement-active macro F1", 96, 190, 300, COLORS.teal);
    addMetric(slide, `+${mainDelta.toFixed(3)}`, `95% CI [+${mainLow.toFixed(3)}, +${mainHigh.toFixed(3)}]`, 470, 190, 270, COLORS.teal);
    addMetric(slide, `${controlA.toFixed(3)} → ${controlA.toFixed(3)}`, "unchanged control F1；49 时间块", 818, 190, 300, COLORS.blue);
    addEffectForest(slide, [{ label: "替换域 5→7", value: mainDelta, low: mainLow, high: mainHigh, color: COLORS.teal, display: `+${mainDelta.toFixed(3)} [${mainLow.toFixed(3)}, ${mainHigh.toFixed(3)}]` }, { label: "未替换控制域", value: 0, low: 0, high: 0, color: COLORS.blue, display: "0.000 [0.000, 0.000]" }], 96, 330, 660, 190, { axisLabel: "CLAAS-3 CMA minus operational macro F1", firstBold: true });
    addRect(slide, 790, 330, 341, 190, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, "为什么可用于决策", 818, 360, 270, 28, { fontSize: 22, color: COLORS.teal, bold: true });
    addText(slide, "同一时次、EPIC、非 Meteosat 输入、共同域和指标；未替换控制域为零差异。", 818, 410, 270, 72, { fontSize: 19, color: COLORS.ink });
  }

  // 11. Source and robustness
  {
    const aggregationRows = evidence.stage09.aggregation.map((row) => ({
      label: row.aggregation.replace("box_", "box ").replace("x", "×"),
      value: finiteMetric(row, "equal_time_macro_B_minus_A", `Stage 09 ${row.aggregation} delta`),
      low: finiteMetric(row, "bootstrap_ci95_low", `Stage 09 ${row.aggregation} CI low`),
      high: finiteMetric(row, "bootstrap_ci95_high", `Stage 09 ${row.aggregation} CI high`),
      color: COLORS.teal,
    }));
    const boundary = evidence.stage09.boundary;
    const nonBoundary = evidence.stage09.nonBoundary;
    const slide = makeSlide(deck, "CMA 的替换效应不依赖单一聚合核；但云边界必须单独保留边界", "Stage 09：双轨稳健性", "数据：Stage 09d CLAAS-3 March 双轨 bootstrap CSV；Policy A，source transition 5→7", "这里不使用 Stage 09e 的 operational 来源图来证明 CLAAS 效应。所有点和区间均直接来自含 CLAAS-3 的双轨 CSV：四种聚合核方向一致；近边界像元区间跨零，因此不强行判为改善。");
    addEffectForest(slide, aggregationRows, 80, 165, 700, 270, { axisLabel: "CLAAS-3 CMA minus operational macro F1", firstBold: true });
    addEffectForest(slide, [
      { label: "非边界", value: finiteMetric(nonBoundary, "equal_time_macro_B_minus_A", "Stage 09 non-boundary delta"), low: finiteMetric(nonBoundary, "bootstrap_ci95_low", "Stage 09 non-boundary CI low"), high: finiteMetric(nonBoundary, "bootstrap_ci95_high", "Stage 09 non-boundary CI high"), color: COLORS.teal },
      { label: "近边界", value: finiteMetric(boundary, "equal_time_macro_B_minus_A", "Stage 09 boundary delta"), low: finiteMetric(boundary, "bootstrap_ci95_low", "Stage 09 boundary CI low"), high: finiteMetric(boundary, "bootstrap_ci95_high", "Stage 09 boundary CI high"), color: COLORS.amber },
    ], 800, 165, 350, 180, { labelWidth: 95, axisLabel: "Delta F1" });
    addRect(slide, 800, 380, 350, 115, COLORS.softAmber, { borderRadius: "rounded-md" });
    addText(slide, "可说的结论", 826, 404, 220, 24, { fontSize: 20, color: COLORS.amber, bold: true });
    addText(slide, "聚合敏感性不改变总体正向结论；近边界像元不做正向外推。", 826, 442, 288, 40, { fontSize: 17, color: COLORS.ink });
  }

  // 12. Spatial case
  {
    const slide = makeSlide(deck, "替换前 spatial case 用于解释 operational 基线的云结构、边界和来源选择", "Stage 09：替换前空间案例", "数据：Stage 09f operational case atlas，2024-03-08 04:00；不含 CLAAS-3", "该图是替换前 operational GEO-ring 当前云掩膜的自动选择案例，不含 CLAAS-3。它用于解释为什么双轨结果需要按边界和来源结构分层；不能作为 CMA 替换效应的图证。");
    await addImage(slide, stage09Case, 74, 153, 1020, 480, "Stage 09 空间案例图");
    addText(slide, "不含 CLAAS-3 的 operational 基线案例；CMA 接入结论仅来自第 10–11 页的双轨统计。", 85, 628, 980, 22, { fontSize: 17, color: COLORS.muted });
  }

  // 13. Bridge
  {
    const slide = makeSlide(deck, "CMA 的改善不能外推到 CTX：同一候选处理流必须接受不同变量的检验", "从 Stage 09 到 Stage 10", source, "这是转场页。强调 cloud mask 与 cloud-top height 的测量对象和误差结构不同，因此不能因为 CMA 成功就默认 CTX 也成功。");
    addRect(slide, 140, 220, 360, 210, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, "CMA", 180, 255, 180, 34, { fontSize: 32, color: COLORS.teal, bold: true });
    addText(slide, "二值云掩膜\nEPIC-relative agreement\nreplacement-active 域改善", 180, 314, 260, 84, { fontSize: 22, color: COLORS.ink });
    addRect(slide, 780, 220, 360, 210, COLORS.softRed, { borderRadius: "rounded-md" });
    addText(slide, "CTX", 820, 255, 180, 34, { fontSize: 32, color: COLORS.red, bold: true });
    addText(slide, "云高变量\nEPIC effective height 诊断\n不能写成绝对 CTH 真值", 820, 314, 270, 84, { fontSize: 22, color: COLORS.ink });
    addLine(slide, 520, 325, 760, 325, COLORS.ink, 3);
    addText(slide, "同一套公平比较框架，不同的物理问题与裁决规则", 386, 470, 520, 34, { fontSize: 23, color: COLORS.ink, alignment: "center", bold: true });
  }

  // 14. Stage10 main
  {
    const [d0A, d0B] = evidence.stage10.d0;
    const d0Rows = [d0A, d0B].map((row) => ({
      label: row.band.replace("_", " "),
      left: `${finiteMetric(row, "equal_time_macro_A_mae_km", `Stage 10 D0 ${row.band} A`).toFixed(3)} km`,
      right: `${finiteMetric(row, "equal_time_macro_B_mae_km", `Stage 10 D0 ${row.band} B`).toFixed(3)} km`,
      value: finiteMetric(row, "equal_time_macro_B_minus_A_mae_km", `Stage 10 D0 ${row.band} delta`),
      low: finiteMetric(row, "bootstrap_ci95_low_km", `Stage 10 D0 ${row.band} CI low`),
      high: finiteMetric(row, "bootstrap_ci95_high_km", `Stage 10 D0 ${row.band} CI high`),
      color: COLORS.red,
    }));
    const slide = makeSlide(deck, "相对于 EPIC A/B-band effective height，CLAAS CTX 的差异在两波段均更大", "Stage 10：CTX 主结果", "数据：Stage 10 March 双轨 bootstrap CSV；D0 common-valid CTH，Policy A，box 7×7", "这页的图元直接由含 CLAAS-3 的双轨 bootstrap CSV 读取并绘制。比较对象是 GEO CTH 与 EPIC A/B effective height 的距离，不是绝对云顶高误差；两波段的区间都在零以上。" );
    addPairedMetricRows(slide, d0Rows, 85, 175, 500, { leftHeader: "operational MAE", rightHeader: "CLAAS-3 MAE" });
    addEffectForest(slide, d0Rows, 625, 165, 510, 230, { labelWidth: 105, axisLabel: "CLAAS-3 minus operational MAE (km)", firstBold: true });
    addRect(slide, 85, 455, 1050, 90, COLORS.softRed, { borderRadius: "rounded-md" });
    addText(slide, "决策边界：两波段均显示 CLAAS-3 相对 EPIC 的 MAE 增大，因此暂保留 operational CTH；这不是“CLAAS 的绝对云顶高更差”的结论。", 114, 482, 980, 34, { fontSize: 21, color: COLORS.ink });
  }

  // 15. Stage10 source
  {
    const [d1A, d1B] = evidence.stage10.d1;
    const d1Rows = [d1A, d1B].map((row) => ({
      label: row.band.replace("_", " "),
      left: `${finiteMetric(row, "equal_time_macro_A_mae_km", `Stage 10 D1 ${row.band} A`).toFixed(3)} km`,
      right: `${finiteMetric(row, "equal_time_macro_B_mae_km", `Stage 10 D1 ${row.band} B`).toFixed(3)} km`,
      value: finiteMetric(row, "equal_time_macro_B_minus_A_mae_km", `Stage 10 D1 ${row.band} delta`),
      low: finiteMetric(row, "bootstrap_ci95_low_km", `Stage 10 D1 ${row.band} CI low`),
      high: finiteMetric(row, "bootstrap_ci95_high_km", `Stage 10 D1 ${row.band} CI high`),
      color: COLORS.red,
    }));
    const slide = makeSlide(deck, "在双方均判云的 D1 控制域中，CTX 的相对差异仍为正", "Stage 10：双轨控制域", "数据：Stage 10 March 双轨 bootstrap CSV；D1 both-cloud，Policy A，box 7×7", "这里不再以不含 CLAAS 的来源分解图支撑控制域结论。D1 直接限制为双方均判云像元；A/B 两波段的 dual-track 差值均为正，说明 D0 的方向不能只归因于一条 profile 的云掩膜有效域。" );
    addPairedMetricRows(slide, d1Rows, 100, 180, 480, { leftHeader: "operational MAE", rightHeader: "CLAAS-3 MAE" });
    addEffectForest(slide, d1Rows, 620, 170, 505, 225, { labelWidth: 105, axisLabel: "CLAAS-3 minus operational MAE (km)", firstBold: true });
    addRect(slide, 100, 460, 1025, 84, COLORS.softBlue, { borderRadius: "rounded-md" });
    addText(slide, "可说的结论：双方均判云控制域中，CLAAS-3 相对 EPIC 的 MAE 仍较大。不可说的结论：这不能单独证明融合前的产品层差异或绝对云高真值。", 130, 485, 960, 34, { fontSize: 20, color: COLORS.ink });
  }

  // 16. Boundaries
  {
    const boundaryRows = [
      ...evidence.stage10.d6.map((row) => ({ label: `边界/碎云 ${row.band === "A_band" ? "A" : "B"}`, value: finiteMetric(row, "equal_time_macro_B_minus_A_mae_km", `Stage 10 D6 ${row.band} delta`), low: finiteMetric(row, "bootstrap_ci95_low_km", `Stage 10 D6 ${row.band} CI low`), high: finiteMetric(row, "bootstrap_ci95_high_km", `Stage 10 D6 ${row.band} CI high`), color: COLORS.amber })),
      ...evidence.stage10.d7.map((row) => ({ label: `高云 ${row.band === "A_band" ? "A" : "B"}`, value: finiteMetric(row, "equal_time_macro_B_minus_A_mae_km", `Stage 10 D7 ${row.band} delta`), low: finiteMetric(row, "bootstrap_ci95_low_km", `Stage 10 D7 ${row.band} CI low`), high: finiteMetric(row, "bootstrap_ci95_high_km", `Stage 10 D7 ${row.band} CI high`), color: COLORS.red })),
    ];
    const slide = makeSlide(deck, "边界/碎云与高云分层仍显示相对差异；但不把相对指标写成绝对高度结论", "Stage 10：状态分层与边界", "数据：Stage 10 March 双轨 bootstrap CSV；D6 boundary/broken、D7 high-cloud，Policy A，box 7×7", "这页只展示有足够样本的 D6 与 D7 双轨结果。D5 clean-core-cloud 在本次运行中为零样本，不能被图形化为正负结论。即使 D6/D7 区间为正，EPIC 仍不能裁决绝对 CTH 真值。" );
    addEffectForest(slide, boundaryRows, 85, 165, 690, 300, { labelWidth: 145, axisLabel: "CLAAS-3 minus operational MAE (km)" });
    addRect(slide, 820, 175, 300, 250, COLORS.softAmber, { borderRadius: "rounded-md" });
    addText(slide, "必须保留的限制", 848, 207, 240, 26, { fontSize: 22, color: COLORS.amber, bold: true });
    addBullet(slide, "D5：0 样本，unresolved", 848, 257, 240, COLORS.amber);
    addBullet(slide, "EPIC：非绝对 CTH 真值", 848, 322, 240, COLORS.amber);
    addBullet(slide, "未展示分层不外推", 848, 387, 240, COLORS.amber);
  }

  // 17. Decision matrix
  {
    const slide = makeSlide(deck, "当前最合理的结果是混合 profile，而不是整套切换", "变量级决策矩阵", source, "把前面结果变成一个可执行的建议。注意这不是永久结论：每个变量保留自己的证据状态和下一步验证门槛。");
    addText(slide, "变量", 92, 188, 180, 26, { fontSize: 19, color: COLORS.muted, bold: true });
    addText(slide, "当前证据", 350, 188, 240, 26, { fontSize: 19, color: COLORS.muted, bold: true });
    addText(slide, "建议", 870, 188, 180, 26, { fontSize: 19, color: COLORS.muted, bold: true });
    addLine(slide, 86, 222, 1168, 222, COLORS.ink, 1.5);
    addDecisionRow(slide, "cloud mask\n(CMA)", "replacement-active 域 F1 改善；控制域零差异；四种聚合核方向一致", "CLAAS-3 CMA\n进入生产回归", 250, COLORS.teal);
    addDecisionRow(slide, "cloud-top height\n(CTX)", "相对 EPIC A/B effective height 的 MAE 均增大；仅为相对诊断", "保留 operational CTH\n等待独立垂直参考", 360, COLORS.red);
    addDecisionRow(slide, "CTP/CTT/CPH/\nCOT/CER/CWP", "尚无独立变量级对照证据", "保持候选状态\n逐变量验证", 470, COLORS.amber);
  }

  // 18. Engineering
  {
    const slide = makeSlide(deck, "本轮产出是一套可复跑、可审计的实验体系，而非一次性统计", "工程化与可复现性", source, "说明工作量时不报运行时长，而是说清楚哪些机制让结果可重现：来源身份、双轨矩阵、共同域、checkpoint、checksum、case atlas 和 manifest。");
    const items = [
      ["数据契约", "source identity、产品版本、时间/空间门限", COLORS.blue],
      ["双轨 runner", "baseline 复用、candidate 独立、控制域", COLORS.teal],
      ["统计与诊断", "共同域、状态分层、whole-time bootstrap", COLORS.amber],
      ["可追溯输出", "manifest、CSV、case atlas、speaker evidence", COLORS.red],
    ];
    items.forEach(([head, body, color], i) => {
      const x = 86 + (i % 2) * 560;
      const y = 188 + Math.floor(i / 2) * 178;
      addRect(slide, x, y, 510, 132, "#FFFFFF", { line: color, lineWidth: 2, borderRadius: "rounded-md" });
      addText(slide, head, x + 28, y + 25, 230, 28, { fontSize: 23, color, bold: true });
      addText(slide, body, x + 28, y + 72, 440, 32, { fontSize: 19, color: COLORS.ink });
    });
  }

  // 19. Next steps
  {
    const slide = makeSlide(deck, "下一步是把变量级决策落实为生产回归，并补足绝对云高证据", "结论与下一步", source, "最后回到开头的问题。给出目前能执行的动作：CMA 进入回归，CTX 暂不切换，其他变量逐个验证；在科学上最关键的是引入有垂直廓线信息的独立参考。");
    addText(slide, "本轮结论", 92, 188, 230, 30, { fontSize: 23, color: COLORS.teal, bold: true });
    addText(slide, "CLAAS-3 可以进入 GEO-ring Cloud，但入口必须是变量级、证据驱动、保留边界的。", 92, 232, 980, 48, { fontSize: 29, color: COLORS.ink, bold: true });
    addRect(slide, 92, 345, 310, 154, COLORS.softTeal, { borderRadius: "rounded-md" });
    addText(slide, "近期", 120, 375, 180, 26, { fontSize: 22, color: COLORS.teal, bold: true });
    addText(slide, "CMA 进入 production regression", 120, 421, 230, 36, { fontSize: 20, color: COLORS.ink });
    addRect(slide, 468, 345, 310, 154, COLORS.softRed, { borderRadius: "rounded-md" });
    addText(slide, "科学验证", 496, 375, 180, 26, { fontSize: 22, color: COLORS.red, bold: true });
    addText(slide, "引入 CALIOP / CloudSat / DARDAR 类垂直参考", 496, 421, 240, 46, { fontSize: 20, color: COLORS.ink });
    addRect(slide, 844, 345, 310, 154, COLORS.softAmber, { borderRadius: "rounded-md" });
    addText(slide, "变量扩展", 872, 375, 180, 26, { fontSize: 22, color: COLORS.amber, bold: true });
    addText(slide, "CTP、CTT、CPH、COT、CER、CWP 逐项验证", 872, 421, 240, 46, { fontSize: 20, color: COLORS.ink });
  }

  // Appendix 20–24
  {
    const slide = makeSlide(deck, "备份：Stage 09 的 operational 基线诊断图（不含 CLAAS-3）", "备份材料", "数据：Stage 09e operational baseline figures，2024-03", "根据讨论需要展开替换前 operational 流的 VIS 控制、PSF-like 聚合、边界像元和不同来源族。该图不用于证明 CLAAS-3 的替换效应。");
    await addImage(slide, stage09Main, 70, 155, 920, 500, "Stage 09 完整诊断图");
  }
  {
    const slide = makeSlide(deck, "备份：Stage 10 的 operational 来源诊断（不含 CLAAS-3）", "备份材料", "数据：Stage 10 operational meeting figures，2024-03", "用于解释替换前 operational CTH 的来源与场景误差结构；不能将其作为 CLAAS-3 双轨结论的证据。");
    await addImage(slide, stage10Source, 90, 155, 900, 490, "Stage 10 来源误差分解图");
  }
  {
    const slide = makeSlide(deck, "备份：operational 空间案例的六层检查框架（不含 CLAAS-3）", "备份材料", "数据：Stage 09f operational case atlas，2024-03-08 04:00", "案例由自动选择规则提供，展示替换前的 EPIC 云掩膜、GEO-ring 当前掩膜、mismatch、来源族、有效源数和场景边界六个层面。");
    await addImage(slide, stage09Case, 66, 155, 1050, 495, "Stage 09 六层空间案例");
  }
  {
    const slide = makeSlide(deck, "备份：本轮数据契约与运行状态", "备份材料", source, "需要审计运行时，用这页说明共有输入、时间门限、空域规则、统计单位、checkpoint/checksum 与运行状态。");
    addDecisionRow(slide, "时间", "53 个登记时次；49 个 ≤10 min 进入主分析", "whole-time blocks", 220, COLORS.blue);
    addDecisionRow(slide, "空间", "common-valid same-pixel；box 7×7 为近似矩形 FOV 聚合", "保留 nearest/3×3/5×5 敏感性", 320, COLORS.teal);
    addDecisionRow(slide, "运行", "pass=53；fail=0；checkpoint、checksum、lineage manifest", "可复跑、可定位", 420, COLORS.amber);
  }
  {
    const slide = makeSlide(deck, "备份：证据与运行产物如何被追溯", "备份材料", source, "最后一页用于说明每张图、每个统计结论都可回到报告、CSV、图索引和 run manifest。这里不在演讲正文展开。");
    addText(slide, "项目记忆入口", 90, 190, 240, 30, { fontSize: 24, color: COLORS.teal, bold: true });
    addBullet(slide, "stage registry：阶段语义与历史别名", 90, 246, 620, COLORS.teal);
    addBullet(slide, "artifact index：关键报告、图表、CSV 与 manifest", 90, 298, 620, COLORS.teal);
    addBullet(slide, "本次 evidence ledger：逐页结论、统计定义与限定语", 90, 350, 620, COLORS.teal);
    addRect(slide, 750, 205, 360, 220, COLORS.pale, { borderRadius: "rounded-md" });
    addText(slide, "本次 run", 780, 240, 250, 30, { fontSize: 26, color: COLORS.ink, bold: true });
    addText(slide, "presentation_builder\nrelated stages: 00d, 09d/e/f, 10\nPPTX、讲稿、术语表、资产与 QA 均由 manifest 关联", 780, 292, 260, 110, { fontSize: 19, color: COLORS.muted });
  }

  const finalPptx = path.join(outputDir, "group_meeting_30min_cn.pptx");
  await writeSupportFiles(outputDir, finalPptx, allAssets);
  const pptx = await PresentationFile.exportPptx(deck);
  await pptx.save(finalPptx);
  await writeBlob(path.join(outputDir, "deck_montage.webp"), await deck.export({ format: "webp", montage: true, scale: 1 }));
  return finalPptx;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outputIndex = process.argv.indexOf("--output");
  const outputDir = outputIndex >= 0 ? path.resolve(process.argv[outputIndex + 1]) : undefined;
  buildGroupMeetingDeck({ outputDir }).then((output) => console.log(output)).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
