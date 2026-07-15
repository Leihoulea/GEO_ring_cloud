from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter

import path_config
from geo_ring_cloud_lineage import write_manifest


SCRIPT_DIR = Path(__file__).resolve().parent
STAGE_ID = "stage_10"
PROJECT_STAGE_ID = "stage_10_claas3"
BANDS = {
    "A_band": "geophysical_data/A-band_Effective_Cloud_Height",
    "B_band": "geophysical_data/B-band_Effective_Cloud_Height",
}
DOMAINS = ("global_common", "replacement_active", "unchanged_control")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_support() -> Any:
    path = SCRIPT_DIR / "stage_10_cth_validation" / "stage_10_run_cth_validation.py"
    spec = importlib.util.spec_from_file_location("stage10_pair_support", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def psf_mean(data: np.ndarray, valid: np.ndarray, radius: int) -> tuple[np.ndarray, np.ndarray]:
    size = 2 * radius + 1
    values = np.where(valid, data, 0.0).astype(np.float32)
    weight = valid.astype(np.float32)
    numerator = uniform_filter(values, size=size, mode="constant", cval=0.0)
    denominator = uniform_filter(weight, size=size, mode="constant", cval=0.0)
    out = np.full(data.shape, np.nan, dtype=np.float32)
    ok = denominator > 0.25
    out[ok] = numerator[ok] / denominator[ok]
    return out, ok


def profile_root(matrix: dict[str, Any], profile: str) -> Path:
    return Path(next(row["profile_root"] for row in matrix["profile_runs"] if row["source_profile"] == profile))


def comparison_domains(common: np.ndarray, base_source: np.ndarray, base_source_valid: np.ndarray, cand_source: np.ndarray, cand_source_valid: np.ndarray, stable_control: np.ndarray | None = None) -> dict[str, np.ndarray]:
    source_common = base_source_valid & cand_source_valid & np.isfinite(base_source) & np.isfinite(cand_source)
    base_id = np.zeros(base_source.shape, dtype=np.int16)
    cand_id = np.zeros(cand_source.shape, dtype=np.int16)
    base_id[source_common] = np.rint(base_source[source_common]).astype(np.int16)
    cand_id[source_common] = np.rint(cand_source[source_common]).astype(np.int16)
    unchanged = common & source_common & (base_id == cand_id) & ~np.isin(base_id, [0, 5, 7])
    if stable_control is not None:
        unchanged &= stable_control
    return {
        "global_common": common,
        "replacement_active": common & source_common & (base_id == 5) & (cand_id == 7),
        "unchanged_control": unchanged,
    }


def evaluate_sample(matrix_path: Path, support: Any, radius: int) -> list[dict[str, Any]]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    epic_path = Path(matrix["common_inputs"]["epic_l2"])
    profile_samples: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    profile_sources: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    profile_source_grids: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    epic_by_band = {band: support.read_epic(epic_path, variable) for band, variable in BANDS.items()}
    reference_epic = epic_by_band["A_band"]
    for profile in ("operational_baseline", "claas3_candidate"):
        root = profile_root(matrix, profile)
        data, valid, _ = support.load_npz_array(root / "fused_best_source" / "fused_cloud_top_height_km.npz")
        averaged, averaged_valid = psf_mean(data.astype(np.float32), valid.astype(bool), radius)
        profile_samples[profile] = support.sample_grid(averaged, averaged_valid, reference_epic["lat"], reference_epic["lon"], support.load_grid(root))
        source, source_valid, _ = support.load_npz_array(root / "fused_best_source" / "source_map_cloud_top_height_km.npz")
        profile_source_grids[profile] = (source, source_valid)
        profile_sources[profile] = support.sample_grid(source, source_valid, reference_epic["lat"], reference_epic["lon"], support.load_grid(root), fill=0.0)
    base_data, base_valid = profile_samples["operational_baseline"]
    cand_data, cand_valid = profile_samples["claas3_candidate"]
    base_source, base_source_valid = profile_sources["operational_baseline"]
    cand_source, cand_source_valid = profile_sources["claas3_candidate"]
    base_source_grid, base_source_grid_valid = profile_source_grids["operational_baseline"]
    cand_source_grid, cand_source_grid_valid = profile_source_grids["claas3_candidate"]
    source_grid_common = base_source_grid_valid & cand_source_grid_valid & np.isfinite(base_source_grid) & np.isfinite(cand_source_grid)
    base_source_id = np.zeros(base_source_grid.shape, dtype=np.int16)
    cand_source_id = np.zeros(cand_source_grid.shape, dtype=np.int16)
    base_source_id[source_grid_common] = np.rint(base_source_grid[source_grid_common]).astype(np.int16)
    cand_source_id[source_grid_common] = np.rint(cand_source_grid[source_grid_common]).astype(np.int16)
    changed_fraction = uniform_filter((~source_grid_common | (base_source_id != cand_source_id)).astype(np.float32), size=2 * radius + 1, mode="constant", cval=1.0)
    stable_grid = changed_fraction <= 1e-8
    stable_sample, stable_sample_valid = support.sample_grid(stable_grid.astype(np.int16), np.ones(stable_grid.shape, dtype=bool), reference_epic["lat"], reference_epic["lon"], support.load_grid(profile_root(matrix, "operational_baseline")), fill=0.0)
    stable_control = stable_sample_valid & (stable_sample == 1)
    rows: list[dict[str, Any]] = []
    for band, epic in epic_by_band.items():
        common = epic["cth_valid"] & base_valid & cand_valid & np.isfinite(base_data) & np.isfinite(cand_data)
        domains = comparison_domains(common, base_source, base_source_valid, cand_source, cand_source_valid, stable_control)
        for domain_name in DOMAINS:
            domain = domains[domain_name]
            base_metrics = support.metrics(epic["cth_km"], base_data, domain)
            cand_metrics = support.metrics(epic["cth_km"], cand_data, domain)
            rows.append({
                "run_id": matrix["run_id"],
                "time_tag": matrix["common_inputs"]["time_tag"],
                "target_time": matrix["common_inputs"]["target_time"],
                "epic_l2": str(epic_path),
                "domain": domain_name,
                "epic_band": band,
                "epic_variable": BANDS[band],
                "common_n": int(np.count_nonzero(domain)),
                "baseline_bias_epic_relative_km": base_metrics.get("bias_km", float("nan")),
                "candidate_bias_epic_relative_km": cand_metrics.get("bias_km", float("nan")),
                "bias_delta_candidate_minus_baseline_km": cand_metrics.get("bias_km", float("nan")) - base_metrics.get("bias_km", float("nan")),
                "baseline_mae_epic_relative_km": base_metrics.get("mae_km", float("nan")),
                "candidate_mae_epic_relative_km": cand_metrics.get("mae_km", float("nan")),
                "mae_delta_candidate_minus_baseline_km": cand_metrics.get("mae_km", float("nan")) - base_metrics.get("mae_km", float("nan")),
                "baseline_rmse_epic_relative_km": base_metrics.get("rmse_km", float("nan")),
                "candidate_rmse_epic_relative_km": cand_metrics.get("rmse_km", float("nan")),
                "rmse_delta_candidate_minus_baseline_km": cand_metrics.get("rmse_km", float("nan")) - base_metrics.get("rmse_km", float("nan")),
                "psf_method": f"box_{2 * radius + 1}x{2 * radius + 1}",
                "psf_radius_target_pixels": radius,
                "interpretation": "EPIC-relative difference, not absolute CTH error",
            })
    return rows


def bootstrap(values: np.ndarray, seed: int = 109, draws: int = 4000) -> tuple[float, float]:
    if values.size < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.mean(values[rng.integers(0, values.size, (draws, values.size))], axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 10 CLAAS-3 versus operational EPIC-relative effective-height evaluation")
    parser.add_argument("--matrix-manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--psf-radius-target-pixels", type=int, default=3)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    support = load_support()
    rows = [row for manifest in args.matrix_manifest for row in evaluate_sample(manifest, support, args.psf_radius_target_pixels)]
    summary: list[dict[str, Any]] = []
    band_signs: list[int] = []
    for domain in DOMAINS:
        for band in BANDS:
            values = np.asarray([row["mae_delta_candidate_minus_baseline_km"] for row in rows if row["epic_band"] == band and row["domain"] == domain], dtype=float)
            values = values[np.isfinite(values)]
            lo, hi = bootstrap(values)
            mean = float(np.mean(values)) if values.size else float("nan")
            if domain == "replacement_active":
                band_signs.append(int(np.sign(mean)) if np.isfinite(mean) else 0)
            if domain == "unchanged_control":
                preference = "control_pass" if values.size >= 5 and float(np.max(np.abs(values))) <= 1e-12 else "control_mismatch"
            else:
                preference = "prefer_claas3" if values.size >= 5 and hi < 0 else ("prefer_operational" if values.size >= 5 and lo > 0 else "unresolved")
            summary.append({
                "domain": domain,
                "epic_band": band,
                "sample_count": int(values.size),
                "mean_mae_delta_candidate_minus_baseline_km": mean,
                "bootstrap_95ci_lower_km": lo,
                "bootstrap_95ci_upper_km": hi,
                "band_preference": preference,
            })
    inconsistent = len({sign for sign in band_signs if sign != 0}) > 1
    replacement_summary = [row for row in summary if row["domain"] == "replacement_active"]
    overall = "unresolved" if inconsistent or any(row["sample_count"] < 5 for row in replacement_summary) else (replacement_summary[0]["band_preference"] if len({row["band_preference"] for row in replacement_summary}) == 1 else "conditional")
    sample_path = args.output_dir / "stage_10_claas3_epic_relative_height_samples.csv"
    summary_path = args.output_dir / "stage_10_claas3_epic_relative_height_summary.csv"
    write_csv(sample_path, rows)
    write_csv(summary_path, summary)
    replacement_rows = [row for row in summary if row["domain"] == "replacement_active"]
    control_rows = [row for row in summary if row["domain"] == "unchanged_control"]
    report = args.output_dir / "stage_10_claas3_epic_relative_height_report.md"
    report.write_text(
        "\n".join([
            "# Stage 10 CLAAS-3 与 EPIC 有效云高双轨评估",
            "",
            f"- 生成时间（UTC）：{utc_now()}",
            f"- 五时次总体判定：**{overall}**。",
            f"- 两轨在相同 EPIC 像元和共同有效域上使用 `box_{2 * args.psf_radius_target_pixels + 1}x{2 * args.psf_radius_target_pixels + 1}` 近似 PSF 聚合。",
            *[
                f"- EPIC {row['epic_band']}：`replacement_active` 域 CLAAS3-candidate 减 baseline 的宏平均 MAE 为 "
                f"{row['mean_mae_delta_candidate_minus_baseline_km']:+.3f} km，时次块 bootstrap 95% CI "
                f"[{row['bootstrap_95ci_lower_km']:+.3f}, {row['bootstrap_95ci_upper_km']:+.3f}] km，判定 `{row['band_preference']}`。"
                for row in replacement_rows
            ],
            f"- 未替换控制域：A/B-band 判定分别为 `{control_rows[0]['band_preference']}` / `{control_rows[1]['band_preference']}`，MAE 差值均为 {control_rows[0]['mean_mae_delta_candidate_minus_baseline_km']:+.1f} km。",
            "- 所有高度统计均为 EPIC-relative difference，不是绝对 CTH 误差；EPIC effective height 与 GEO cloud-top height 的物理定义并不等价。",
            "- A/B-band 排名冲突时必须判为 `unresolved`；即使两者一致，本五时次 pilot 也不会自动触发生产源切换。",
        ]),
        encoding="utf-8",
    )
    write_manifest(
        args.output_dir / "stage_10_claas3_epic_relative_height_manifest.json",
        canonical_stage_id="stage_10",
        run_id="profile_pair_batch",
        source_profile="profile_pair",
        generating_script=Path(__file__),
        input_paths=args.matrix_manifest,
        output_paths=[sample_path, summary_path, report],
        parameters={"psf_radius_target_pixels": args.psf_radius_target_pixels, "psf_method": f"box_{2 * args.psf_radius_target_pixels + 1}x{2 * args.psf_radius_target_pixels + 1}", "domains": DOMAINS, "sample_block_bootstrap": True},
        project_root=path_config.PROJECT_ROOT,
        extra={
            "product_versions": {"CLAAS3": "405"},
            "scientific_boundary": "EPIC effective cloud height is a relative diagnostic and not strict cloud-top truth",
        },
    )
    print(f"stage_10 OK: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
