from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from geo_ring_cloud.diagnostics import cth_validation as base


STAGE_ID = "stage_10"
A_BAND = "geophysical_data/A-band_Effective_Cloud_Height"
B_BAND = "geophysical_data/B-band_Effective_Cloud_Height"
QC_DIRNAME = "10_qc"


@dataclass
class MetricAccum:
    n: int = 0
    sum_ref: float = 0.0
    sum_pred: float = 0.0
    sum_diff: float = 0.0
    sum_abs: float = 0.0
    sum_sq: float = 0.0
    within_1: int = 0
    within_2: int = 0
    within_3: int = 0
    class_agree: int = 0

    def add(self, ref: np.ndarray, pred: np.ndarray, mask: np.ndarray) -> None:
        valid = mask & np.isfinite(ref) & np.isfinite(pred)
        n = int(np.count_nonzero(valid))
        if n == 0:
            return
        r = ref[valid].astype(np.float64)
        p = pred[valid].astype(np.float64)
        d = p - r
        ad = np.abs(d)
        self.n += n
        self.sum_ref += float(np.sum(r))
        self.sum_pred += float(np.sum(p))
        self.sum_diff += float(np.sum(d))
        self.sum_abs += float(np.sum(ad))
        self.sum_sq += float(np.sum(d * d))
        self.within_1 += int(np.count_nonzero(ad <= 1.0))
        self.within_2 += int(np.count_nonzero(ad <= 2.0))
        self.within_3 += int(np.count_nonzero(ad <= 3.0))
        self.class_agree += int(np.count_nonzero(base.cth_class(r) == base.cth_class(p)))

    def row(self) -> dict[str, Any]:
        if self.n == 0:
            return {
                "n_valid_cth": 0,
                "reference_mean_km": math.nan,
                "test_mean_km": math.nan,
                "bias_km": math.nan,
                "mae_km": math.nan,
                "rmse_km": math.nan,
                "within_1km_fraction": math.nan,
                "within_2km_fraction": math.nan,
                "within_3km_fraction": math.nan,
                "low_mid_high_class_agreement": math.nan,
            }
        return {
            "n_valid_cth": self.n,
            "reference_mean_km": self.sum_ref / self.n,
            "test_mean_km": self.sum_pred / self.n,
            "bias_km": self.sum_diff / self.n,
            "mae_km": self.sum_abs / self.n,
            "rmse_km": math.sqrt(self.sum_sq / self.n),
            "within_1km_fraction": self.within_1 / self.n,
            "within_2km_fraction": self.within_2 / self.n,
            "within_3km_fraction": self.within_3 / self.n,
            "low_mid_high_class_agreement": self.class_agree / self.n,
        }


@dataclass
class RegretAccum:
    n: int = 0
    sum_current_abs: float = 0.0
    sum_best_abs: float = 0.0
    selected_best: int = 0

    def add(
        self,
        current_err: np.ndarray,
        best_err: np.ndarray,
        selected_is_best: np.ndarray,
        mask: np.ndarray,
    ) -> None:
        valid = mask & np.isfinite(current_err) & np.isfinite(best_err)
        n = int(np.count_nonzero(valid))
        if n == 0:
            return
        self.n += n
        self.sum_current_abs += float(np.sum(current_err[valid].astype(np.float64)))
        self.sum_best_abs += float(np.sum(best_err[valid].astype(np.float64)))
        self.selected_best += int(np.count_nonzero(selected_is_best[valid]))

    def row(self) -> dict[str, Any]:
        if self.n == 0:
            return {
                "n_valid_cth": 0,
                "current_selected_mae_km": math.nan,
                "best_available_mae_km": math.nan,
                "selection_regret_mae_km": math.nan,
                "selected_is_best_fraction": math.nan,
            }
        current = self.sum_current_abs / self.n
        best = self.sum_best_abs / self.n
        return {
            "n_valid_cth": self.n,
            "current_selected_mae_km": current,
            "best_available_mae_km": best,
            "selection_regret_mae_km": current - best,
            "selected_is_best_fraction": self.selected_best / self.n,
        }


def write_df(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(rows)).to_csv(path, index=False, encoding="utf-8-sig")


def add_metric(acc: dict[tuple[Any, ...], MetricAccum], key: tuple[Any, ...], ref: np.ndarray, pred: np.ndarray, mask: np.ndarray) -> None:
    acc[key].add(ref, pred, mask)


def add_counts(rows: list[dict[str, Any]], sample_meta: dict[str, Any], group: str, dimension: str, values: np.ndarray, mask: np.ndarray) -> None:
    total = int(np.count_nonzero(mask))
    if total == 0:
        return
    for value in sorted([v for v in pd.unique(values[mask].ravel()) if str(v) != "missing"]):
        count = int(np.count_nonzero(mask & (values == value)))
        rows.append(
            {
                **sample_meta,
                "qc_group": group,
                "dimension": dimension,
                "value": value,
                "pixel_count": count,
                "pixel_fraction": count / total,
            }
        )


def finite_class(values: np.ndarray) -> np.ndarray:
    cls = base.cth_class(values)
    cls[~np.isfinite(values)] = "missing"
    return cls


def make_semantic_rows() -> list[dict[str, Any]]:
    return [
        {
            "product_family": "EPIC_L2_CLOUD",
            "source_name": "EPIC",
            "variable_name": A_BAND,
            "standardized_variable": "epic_a_band_effective_cloud_height_km",
            "semantic_role": "effective_cloud_height",
            "is_strict_cloud_top_height": False,
            "units_original": "m",
            "units_standardized": "km",
            "evidence": "EPIC variable name and inventory long_name: Retrieved effective cloud height from Oxygen A-band.",
            "stage10_use": "primary diagnostic reference, not truth",
            "limitation": "Oxygen A-band retrieval senses an effective photon-path cloud height; it is CTH-like but not identical to geometric cloud top.",
        },
        {
            "product_family": "EPIC_L2_CLOUD",
            "source_name": "EPIC",
            "variable_name": B_BAND,
            "standardized_variable": "epic_b_band_effective_cloud_height_km",
            "semantic_role": "effective_cloud_height",
            "is_strict_cloud_top_height": False,
            "units_original": "m",
            "units_standardized": "km",
            "evidence": "EPIC variable name and inventory long_name: Retrieved effective cloud height from Oxygen B-band.",
            "stage10_use": "sensitivity reference",
            "limitation": "B-band has different oxygen absorption sensitivity, so A/B differences are semantic and retrieval-sensitivity evidence.",
        },
        {
            "product_family": "GEO-ring fused",
            "source_name": "GEO-ring",
            "variable_name": "fused_cloud_top_height_km",
            "standardized_variable": "fused_cloud_top_height_km",
            "semantic_role": "cloud_top_height",
            "is_strict_cloud_top_height": True,
            "units_original": "km",
            "units_standardized": "km",
            "evidence": "Stage 06 fused CTH product and Stage10 inventory.",
            "stage10_use": "test product",
            "limitation": "Fused value inherits source-specific retrieval limits and selected-source mechanism.",
        },
        {
            "product_family": "GOES ABI L2 ACHAF",
            "source_name": "GOES-16/GOES-18",
            "variable_name": "HT",
            "standardized_variable": "cloud_top_height_km",
            "semantic_role": "cloud_top_height",
            "is_strict_cloud_top_height": True,
            "units_original": "m",
            "units_standardized": "km",
            "evidence": "Local NPZ metadata: product=ACHAF, global_title=ABI L2 Cloud Top Height, long_name=ABI L2+ Cloud Top Height, standard_name=geopotential_height_at_cloud_top.",
            "stage10_use": "GEO prefusion source CTH",
            "limitation": "Height is source retrieval CTH; CTP/CTT consistency is a separate source-product audit, not proven by the fused grid alone.",
        },
        {
            "product_family": "FY4B AGRI L2 CTH",
            "source_name": "FY4B",
            "variable_name": "CTH",
            "standardized_variable": "cloud_top_height_km",
            "semantic_role": "cloud_top_height",
            "is_strict_cloud_top_height": True,
            "units_original": "m or km by source audit",
            "units_standardized": "km",
            "evidence": "manual_variable_mapping_by_product.yaml and Stage10 source inventory.",
            "stage10_use": "GEO prefusion source CTH",
            "limitation": "Needs source-level CTP/CTT cross-check before treating as physically interchangeable with all other GEO CTH products.",
        },
        {
            "product_family": "Himawari AHI L2 CHGT",
            "source_name": "Himawari-9",
            "variable_name": "CldTopHght",
            "standardized_variable": "cloud_top_height_km",
            "semantic_role": "cloud_top_height",
            "is_strict_cloud_top_height": True,
            "units_original": "m",
            "units_standardized": "km",
            "evidence": "manual variable mapping and Stage10 source inventory.",
            "stage10_use": "GEO prefusion source CTH",
            "limitation": "Projection and retrieval semantics remain source-specific.",
        },
        {
            "product_family": "Meteosat L2 CTH",
            "source_name": "Meteosat-0deg/Meteosat-IODC",
            "variable_name": "ctoph",
            "standardized_variable": "cloud_top_height_km",
            "semantic_role": "cloud_top_height",
            "is_strict_cloud_top_height": True,
            "units_original": "m",
            "units_standardized": "km",
            "evidence": "manual variable mapping and Stage10 source inventory.",
            "stage10_use": "GEO prefusion source CTH",
            "limitation": "Stage10 finds higher EPIC-referenced errors in Meteosat-selected regions; this is not by itself a source truth ranking.",
        },
    ]


def process_sample(row: pd.Series, accum: dict[str, Any]) -> None:
    sample_id = str(row["sample_id"])
    run_dir = Path(str(row["stage_run_dir"]))
    epic_path = Path(str(row["epic_file"]))
    grid = base.load_grid(run_dir)
    epic_a = base.read_epic(epic_path, A_BAND)
    epic_b = base.read_epic(epic_path, B_BAND)
    fused_dir = run_dir / "fused_best_source"
    fused_cth, fused_cth_valid, _ = base.load_npz_array(fused_dir / "fused_cloud_top_height_km.npz")
    fused_cm, fused_cm_valid, _ = base.load_npz_array(fused_dir / "fused_cloud_mask.npz")
    source_map, source_valid, _ = base.load_npz_array(fused_dir / "source_map_cloud_top_height_km.npz")
    valid_count, valid_count_valid, _ = base.load_npz_array(fused_dir / "valid_count_map_cloud_top_height_km.npz")

    fused_on, fused_on_valid = base.sample_grid(fused_cth, fused_cth_valid & (fused_cth >= 0) & (fused_cth <= 25), epic_a["lat"], epic_a["lon"], grid)
    fused_cm_on, fused_cm_on_valid = base.sample_grid(fused_cm, fused_cm_valid, epic_a["lat"], epic_a["lon"], grid)
    src_on, src_on_valid = base.sample_grid(source_map, source_valid, epic_a["lat"], epic_a["lon"], grid)
    vc_on, vc_on_valid = base.sample_grid(valid_count, valid_count_valid, epic_a["lat"], epic_a["lon"], grid)
    src_int = np.zeros(src_on.shape, dtype=np.int16)
    src_finite = np.isfinite(src_on)
    src_int[src_finite] = src_on[src_finite].astype(np.int16)
    src_names = base.source_name_array(src_int)

    sample_meta = {
        "sample_id": sample_id,
        "epic_time_utc": row.get("epic_time_utc", ""),
        "nearest_georing_time_utc": row.get("nearest_georing_time_utc", ""),
        "time_diff_min": row.get("time_diff_min", ""),
        "candidate_group": row.get("candidate_group", ""),
        "dominant_source": row.get("dominant_source", ""),
    }
    common_geo = fused_on_valid & np.isfinite(fused_on) & (fused_on >= 0) & (fused_on <= 25)
    common_a = common_geo & epic_a["cth_valid"]
    common_b = common_geo & epic_b["cth_valid"]
    common_ab = common_a & epic_b["cth_valid"]

    prefusion_sampled: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for source in base.SOURCES:
        path = base.source_cth_file(run_dir, source)
        if path is None:
            continue
        data, valid, _ = base.load_npz_array(path)
        sampled, sampled_valid = base.sample_grid(data, valid & (data >= 0) & (data <= 25), epic_a["lat"], epic_a["lon"], grid)
        prefusion_sampled[source] = (sampled, sampled_valid)

    source_stack_names = list(prefusion_sampled.keys())
    best_err = np.full(epic_a["cth_km"].shape, np.nan, dtype=np.float32)
    selected_is_best = np.zeros(epic_a["cth_km"].shape, dtype=bool)
    source_pair_disagreement = np.full(epic_a["cth_km"].shape, np.nan, dtype=np.float32)
    if prefusion_sampled:
        vals = np.stack([prefusion_sampled[s][0] for s in source_stack_names], axis=0)
        valids = np.stack([prefusion_sampled[s][1] for s in source_stack_names], axis=0)
        errors = np.abs(vals - epic_a["cth_km"][None, :, :])
        errors[~valids] = np.nan
        finite_any = np.any(np.isfinite(errors), axis=0)
        best_err[finite_any] = np.nanmin(errors[:, finite_any], axis=0).astype(np.float32)
        best_idx = np.argmin(np.where(np.isfinite(errors), errors, np.inf), axis=0)
        best_names = np.asarray(source_stack_names, dtype=object)[best_idx]
        selected_is_best = finite_any & (best_names == src_names)
        masked_vals = np.where(valids, vals, np.nan)
        maxv = np.full(epic_a["cth_km"].shape, np.nan, dtype=np.float32)
        minv = np.full(epic_a["cth_km"].shape, np.nan, dtype=np.float32)
        maxv[finite_any] = np.nanmax(masked_vals[:, finite_any], axis=0).astype(np.float32)
        minv[finite_any] = np.nanmin(masked_vals[:, finite_any], axis=0).astype(np.float32)
        source_pair_disagreement[finite_any] = maxv[finite_any] - minv[finite_any]

    for policy_name, policy in base.POLICIES.items():
        ecls, ev = base.apply_policy(epic_a["cloud_mask"], policy["epic"])
        gcls, gv = base.apply_policy(fused_cm_on, policy["geo"])
        policy_valid = ev & gv & fused_cm_on_valid
        d1_a = common_a & policy_valid & (ecls == 1) & (gcls == 1)
        d1_ab = common_ab & policy_valid & (ecls == 1) & (gcls == 1)
        local_cloud = base.local_fraction(d1_a, 2)
        boundary = d1_a & (local_cloud > 0.05) & (local_cloud < 0.95)
        clean_core = d1_a & (local_cloud >= 0.95) & (np.abs(epic_a["lat"]) < 60) & (epic_a["epic_vza"] < 60) & (epic_a["sza"] < 70)
        high_cloud = d1_a & ((epic_a["cth_km"] >= 7) | (fused_on >= 7))
        fused_high = d1_a & (fused_on >= 7)

        add_metric(accum["ab_metrics"], (policy_name, "D1_both_cloud_common_AB", "B_minus_A_effective_height"), epic_a["cth_km"], epic_b["cth_km"], d1_ab)
        add_metric(accum["ref_metrics"], (policy_name, "D1_both_cloud", "A_band", "ALL"), epic_a["cth_km"], fused_on, d1_a)
        add_metric(accum["ref_metrics"], (policy_name, "D1_both_cloud", "B_band", "ALL"), epic_b["cth_km"], fused_on, d1_ab)
        add_metric(accum["ref_metrics"], (policy_name, "D7_high_cloud", "A_band", "ALL"), epic_a["cth_km"], fused_on, high_cloud)
        add_metric(accum["ref_metrics"], (policy_name, "D7_high_cloud", "B_band", "ALL"), epic_b["cth_km"], fused_on, high_cloud & epic_b["cth_valid"])

        for selected in sorted([v for v in pd.unique(src_names[d1_a].ravel()) if str(v) != "missing"]):
            m_a = d1_a & (src_names == selected)
            m_b = d1_ab & (src_names == selected)
            add_metric(accum["ref_metrics"], (policy_name, "D1_both_cloud", "A_band", f"selected_source={selected}"), epic_a["cth_km"], fused_on, m_a)
            add_metric(accum["ref_metrics"], (policy_name, "D1_both_cloud", "B_band", f"selected_source={selected}"), epic_b["cth_km"], fused_on, m_b)

        clean_groups = {
            "clean_core": clean_core,
            "boundary_or_broken_cloud": boundary,
            "non_boundary": d1_a & ~boundary,
        }
        for group, mask in clean_groups.items():
            add_counts(accum["clean_composition"], sample_meta, group, "selected_source", src_names, mask)
            add_counts(accum["clean_composition"], sample_meta, group, "fused_cth_class", finite_class(fused_on), mask)
            add_counts(accum["clean_composition"], sample_meta, group, "epic_cth_class", finite_class(epic_a["cth_km"]), mask)
            meteo = np.where(np.isin(src_names, ["Meteosat-0deg", "Meteosat-IODC"]), "Meteosat_selected", "non_Meteosat_selected")
            add_counts(accum["clean_composition"], sample_meta, group, "meteosat_selected_flag", meteo, mask)
            add_metric(accum["clean_metrics"], (policy_name, group, "ALL", "ALL"), epic_a["cth_km"], fused_on, mask)

        for selected in sorted([v for v in pd.unique(src_names[clean_core].ravel()) if str(v) != "missing"]):
            for basis_name, basis_values in [("fused_cth_class", finite_class(fused_on)), ("epic_cth_class", finite_class(epic_a["cth_km"]))]:
                for height_class in ["low_cloud", "mid_cloud", "high_cloud"]:
                    m = clean_core & (src_names == selected) & (basis_values == height_class)
                    add_metric(accum["clean_source_height"], (policy_name, selected, basis_name, height_class), epic_a["cth_km"], fused_on, m)

        current_err = np.abs(fused_on - epic_a["cth_km"])
        regret_groups = {
            "ALL_VALID_CTH": d1_a,
            "valid_source_count_1": d1_a & (vc_on == 1),
            "valid_source_count_2": d1_a & (vc_on == 2),
            "valid_source_count_3": d1_a & (vc_on == 3),
            "valid_source_count_ge4": d1_a & (vc_on >= 4),
            "selected_Meteosat0deg": d1_a & (src_on == base.SOURCE_TO_ID["Meteosat-0deg"]),
            "selected_MeteosatIODC": d1_a & (src_on == base.SOURCE_TO_ID["Meteosat-IODC"]),
            "boundary_or_broken_cloud": boundary,
            "clean_core": clean_core,
            "high_cloud": high_cloud,
        }
        for group, mask in regret_groups.items():
            accum["regret"][(policy_name, group)].add(current_err, best_err, selected_is_best, mask)

        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "epic_cth_class", finite_class(epic_a["cth_km"]), fused_high)
        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "selected_source", src_names, fused_high)
        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "meteosat_selected_flag", np.where(np.isin(src_names, ["Meteosat-0deg", "Meteosat-IODC"]), "Meteosat_selected", "non_Meteosat_selected"), fused_high)
        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "abs_lat_bin", base.bin_numeric(np.abs(epic_a["lat"]), [("abs_lat_0_30", 0, 30), ("abs_lat_30_60", 30, 60), ("abs_lat_ge60", 60, 91)]), fused_high)
        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "EPIC_VZA_bin", base.bin_numeric(epic_a["epic_vza"], [("EPIC_VZA_0_30", 0, 30), ("EPIC_VZA_30_60", 30, 60), ("EPIC_VZA_ge60", 60, 181)]), fused_high)
        add_counts(accum["high_composition"], sample_meta, "fused_high_cloud", "SZA_bin", base.bin_numeric(epic_a["sza"], [("SZA_0_50", 0, 50), ("SZA_50_70", 50, 70), ("SZA_ge70", 70, 181)]), fused_high)
        add_metric(accum["high_metrics"], (policy_name, "fused_high_cloud", "ALL"), epic_a["cth_km"], fused_on, fused_high)

        n_high = int(np.count_nonzero(fused_high))
        if policy_name == "A_inclusive_binary" and n_high:
            m = base.metrics(epic_a["cth_km"], fused_on, fused_high)
            accum["high_cases"].append(
                {
                    **sample_meta,
                    "policy": policy_name,
                    "n_fused_high_cloud": n_high,
                    "mae_km": m.get("mae_km", math.nan),
                    "bias_km": m.get("bias_km", math.nan),
                    "epic_low_fraction": float(np.mean(finite_class(epic_a["cth_km"])[fused_high] == "low_cloud")),
                    "epic_mid_fraction": float(np.mean(finite_class(epic_a["cth_km"])[fused_high] == "mid_cloud")),
                    "epic_high_fraction": float(np.mean(finite_class(epic_a["cth_km"])[fused_high] == "high_cloud")),
                    "meteosat_selected_fraction": float(np.mean(np.isin(src_names[fused_high], ["Meteosat-0deg", "Meteosat-IODC"]))),
                    "source_disagreement_mean_abs_km": float(np.nanmean(source_pair_disagreement[fused_high])),
                    "mean_epic_vza": float(np.nanmean(epic_a["epic_vza"][fused_high])),
                    "mean_sza": float(np.nanmean(epic_a["sza"][fused_high])),
                    "mean_abs_lat": float(np.nanmean(np.abs(epic_a["lat"][fused_high]))),
                }
            )

        for source_a, source_b in base.SOURCE_PAIR_LIST:
            if source_a not in prefusion_sampled or source_b not in prefusion_sampled:
                continue
            a, av = prefusion_sampled[source_a]
            b, bv = prefusion_sampled[source_b]
            m = fused_high & av & bv
            n = int(np.count_nonzero(m))
            if n == 0:
                continue
            disagree = np.abs(a[m] - b[m]).astype(np.float64)
            f_err = np.abs(fused_on[m] - epic_a["cth_km"][m]).astype(np.float64)
            accum["high_pair_disagreement"].append(
                {
                    **sample_meta,
                    "policy": policy_name,
                    "source_A": source_a,
                    "source_B": source_b,
                    "n_common_fused_high_cloud": n,
                    "source_cth_disagreement_mean_abs_km": float(np.mean(disagree)),
                    "source_cth_disagreement_p90_abs_km": float(np.percentile(disagree, 90)),
                    "fused_abs_error_mean_km": float(np.mean(f_err)),
                    "fused_abs_error_p90_km": float(np.percentile(f_err, 90)),
                }
            )


def metric_rows(acc: dict[tuple[Any, ...], MetricAccum], columns: list[str]) -> list[dict[str, Any]]:
    rows = []
    for key, value in sorted(acc.items()):
        row = {col: key[i] for i, col in enumerate(columns)}
        row.update(value.row())
        rows.append(row)
    return rows


def regret_rows(acc: dict[tuple[Any, ...], RegretAccum]) -> list[dict[str, Any]]:
    rows = []
    for (policy, group), value in sorted(acc.items()):
        row = {"policy": policy, "pixel_group": group}
        row.update(value.row())
        row["best_available_definition"] = "EPIC-referenced retrospective diagnostic oracle among same-pixel available prefusion GEO CTH sources; not a production rule and not absolute truth."
        rows.append(row)
    return rows


def summarize_composition(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    df = pd.DataFrame(rows)
    if df.empty:
        return []
    group_cols = ["qc_group", "dimension", "value"]
    out = df.groupby(group_cols, dropna=False, as_index=False)["pixel_count"].sum()
    totals = out.groupby(["qc_group", "dimension"])["pixel_count"].transform("sum")
    out["pixel_fraction"] = out["pixel_count"] / totals
    return out.sort_values(group_cols).to_dict("records")


def build_sensitivity_rows(ref_rows: list[dict[str, Any]], ab_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    df = pd.DataFrame(ref_rows)
    rows: list[dict[str, Any]] = []
    for policy in sorted(df["policy"].dropna().unique()):
        for domain in sorted(df["domain"].dropna().unique()):
            sub = df[(df["policy"] == policy) & (df["domain"] == domain)]
            for group in sorted(sub["group"].dropna().unique()):
                a = sub[(sub["reference_band"] == "A_band") & (sub["group"] == group)]
                b = sub[(sub["reference_band"] == "B_band") & (sub["group"] == group)]
                if a.empty or b.empty:
                    continue
                ar = a.iloc[0]
                br = b.iloc[0]
                rows.append(
                    {
                        "policy": policy,
                        "domain": domain,
                        "group": group,
                        "a_band_n_valid_cth": int(ar["n_valid_cth"]),
                        "b_band_n_valid_cth": int(br["n_valid_cth"]),
                        "a_band_mae_km": ar["mae_km"],
                        "b_band_mae_km": br["mae_km"],
                        "b_minus_a_mae_km": br["mae_km"] - ar["mae_km"],
                        "a_band_bias_km": ar["bias_km"],
                        "b_band_bias_km": br["bias_km"],
                        "b_minus_a_bias_km": br["bias_km"] - ar["bias_km"],
                    }
                )
    rank_rows = []
    selected = df[df["group"].astype(str).str.startswith("selected_source=")].copy()
    if not selected.empty:
        for (policy, band), g in selected.groupby(["policy", "reference_band"]):
            gg = g.sort_values("mae_km").reset_index(drop=True)
            for i, (_, r) in enumerate(gg.iterrows(), 1):
                rank_rows.append({"policy": policy, "reference_band": band, "group": r["group"], "mae_rank": i, "mae_km": r["mae_km"]})
    ranks = pd.DataFrame(rank_rows)
    if not ranks.empty:
        wide = ranks.pivot_table(index=["policy", "group"], columns="reference_band", values=["mae_rank", "mae_km"], aggfunc="first").reset_index()
        wide.columns = ["_".join([str(c) for c in col if c]) for col in wide.columns.to_flat_index()]
        for _, r in wide.iterrows():
            if "mae_rank_A_band" in r and "mae_rank_B_band" in r and pd.notna(r.get("mae_rank_A_band")) and pd.notna(r.get("mae_rank_B_band")):
                rows.append(
                    {
                        "policy": r["policy"],
                        "domain": "D1_both_cloud",
                        "group": r["group"],
                        "a_band_mae_rank": int(r["mae_rank_A_band"]),
                        "b_band_mae_rank": int(r["mae_rank_B_band"]),
                        "b_minus_a_rank": int(r["mae_rank_B_band"] - r["mae_rank_A_band"]),
                        "a_band_mae_km": r.get("mae_km_A_band", math.nan),
                        "b_band_mae_km": r.get("mae_km_B_band", math.nan),
                        "b_minus_a_mae_km": r.get("mae_km_B_band", math.nan) - r.get("mae_km_A_band", math.nan),
                        "sensitivity_type": "selected_source_ranking_change",
                    }
                )
    for row in rows:
        row.setdefault("sensitivity_type", "reference_band_metric_delta")
    for row in ab_rows:
        rows.append({**row, "sensitivity_type": "epic_b_minus_a_effective_height"})
    return rows


def write_report(out_dir: Path, outputs: dict[str, Path], summary: dict[str, Any]) -> None:
    lines = [
        "# Stage 10-QC CTH validation audit",
        "",
        f"Generated: `{base.utc_now()}`",
        "",
        "## 定位",
        "",
        "本 QC 是 `geo_ring_cloud.stage_10` 的补充审计，只读取既有 Stage 06 fused CTH、Stage 09D 53 样本清单、本地 EPIC L2 Cloud 和 Stage10 输出；不新增样本、不重跑 fusion、不修改生产规则。",
        "",
        "## 关键结论",
        "",
        f"- A/B-band common both-cloud 的 B-A effective height 均值为 `{summary.get('ab_bias_km', math.nan):.3f}` km，MAE 为 `{summary.get('ab_mae_km', math.nan):.3f}` km。",
        f"- Policy A 使用 B-band 替代 A-band 时，D1 both-cloud fused MAE 变化为 `{summary.get('policy_a_b_minus_a_mae_km', math.nan):.3f}` km；fused high-cloud MAE 变化为 `{summary.get('policy_a_high_b_minus_a_mae_km', math.nan):.3f}` km。",
        f"- pixel-weighted regret 已重算：Policy A ALL_VALID_CTH 像元总数 `{summary.get('regret_policy_a_n', 0)}`，current selected MAE `{summary.get('regret_policy_a_current_mae', math.nan):.3f}` km，best-available oracle MAE `{summary.get('regret_policy_a_best_mae', math.nan):.3f}` km，regret `{summary.get('regret_policy_a_regret', math.nan):.3f}` km。",
        f"- clean-core MAE 为 `{summary.get('clean_core_mae', math.nan):.3f}` km，boundary/broken-cloud MAE 为 `{summary.get('boundary_mae', math.nan):.3f}` km。若 clean-core 高于 boundary，优先解释为高度语义、区域源选择和高云组成差异，而不是云边界单因素。",
        f"- GOES-16/18 的 Stage10 本地证据指向 ABI L2 `ACHAF` 的 `HT` cloud top height，不是 EPIC 的 Oxygen-band effective cloud height。",
        "",
        "## 方法说明",
        "",
        "Selection regret 中的 `best_available` 是 EPIC-referenced retrospective diagnostic oracle：在同一 EPIC 像元、同一时刻可用的 prefusion GEO CTH 源里，事后选择相对 EPIC A-band effective height 绝对误差最小者。它用于机制诊断，不是生产可用规则，也不是真实 CTH 排名。",
        "",
        "EPIC A/B-band 都是 effective cloud height。Stage10 的 fused/source GEO 变量按本地代码和元数据审计为 cloud top height。二者可做诊断对照，但报告中必须写作“相对 EPIC effective height 的偏离”，不能写作绝对 CTH 误差。",
        "",
        "## 输出索引",
        "",
    ]
    for label, path in outputs.items():
        lines.append(f"- {label}: `{path}`")
    path = out_dir / "stage_10_qc_report_cn.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 10 QC audit for GEO-ring fused CTH vs EPIC effective cloud height.")
    parser.add_argument("--sample-manifest", type=Path, default=base.RUNS_ROOT / "stage09d_full_pixel_diagnostics_202403" / "00_sample_manifest" / "stage09d_53_sample_manifest.csv")
    parser.add_argument("--stage10-output-dir", type=Path, default=base.RUNS_ROOT / "stage_10_cth_fused_product_validation_202403")
    parser.add_argument("--limit-samples", type=int, default=0)
    args = parser.parse_args()

    out_dir = args.stage10_output_dir / QC_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = base.read_manifest(args.sample_manifest)
    if args.limit_samples:
        manifest = manifest.head(args.limit_samples).copy()

    accum: dict[str, Any] = {
        "ab_metrics": defaultdict(MetricAccum),
        "ref_metrics": defaultdict(MetricAccum),
        "clean_metrics": defaultdict(MetricAccum),
        "clean_source_height": defaultdict(MetricAccum),
        "clean_composition": [],
        "regret": defaultdict(RegretAccum),
        "high_composition": [],
        "high_metrics": defaultdict(MetricAccum),
        "high_cases": [],
        "high_pair_disagreement": [],
    }

    for i, (_, sample) in enumerate(manifest.iterrows(), 1):
        print(f"[stage_10_qc] {i}/{len(manifest)} {sample['sample_id']}", flush=True)
        process_sample(sample, accum)

    ab_rows = metric_rows(accum["ab_metrics"], ["policy", "domain", "comparison"])
    ref_rows = metric_rows(accum["ref_metrics"], ["policy", "domain", "reference_band", "group"])
    clean_metric_rows = metric_rows(accum["clean_metrics"], ["policy", "clean_group", "source_group", "height_group"])
    clean_source_height_rows = metric_rows(accum["clean_source_height"], ["policy", "selected_source", "height_basis", "height_class"])
    high_metric_rows = metric_rows(accum["high_metrics"], ["policy", "qc_group", "group"])
    regret_metric_rows = regret_rows(accum["regret"])
    clean_comp_rows = summarize_composition(accum["clean_composition"])
    high_comp_rows = summarize_composition(accum["high_composition"])
    sensitivity_rows = build_sensitivity_rows(ref_rows, ab_rows)
    high_cases = sorted(accum["high_cases"], key=lambda r: (r.get("mae_km", -1), r.get("n_fused_high_cloud", -1)), reverse=True)

    outputs = {
        "A/B-band sensitivity": out_dir / "stage_10_qc_a_band_vs_b_band_sensitivity.csv",
        "reference metrics A vs B": out_dir / "stage_10_qc_reference_metrics_a_vs_b.csv",
        "clean-core composition": out_dir / "stage_10_qc_clean_core_composition.csv",
        "clean-core metrics": out_dir / "stage_10_qc_clean_core_metrics.csv",
        "clean-core source-height metrics": out_dir / "stage_10_qc_clean_core_metrics_by_source_height.csv",
        "selection regret pixel-weighted": out_dir / "stage_10_qc_selection_regret_pixel_weighted.csv",
        "fused high-cloud composition": out_dir / "stage_10_qc_fused_high_cloud_composition.csv",
        "fused high-cloud metrics": out_dir / "stage_10_qc_fused_high_cloud_metrics.csv",
        "fused high-cloud cases": out_dir / "stage_10_qc_fused_high_cloud_cases.csv",
        "fused high-cloud source-pair disagreement": out_dir / "stage_10_qc_fused_high_cloud_source_pair_disagreement.csv",
        "semantic variable audit": out_dir / "stage_10_qc_semantic_variable_audit.csv",
        "manifest": out_dir / "stage_10_qc_manifest.json",
    }
    write_df(outputs["A/B-band sensitivity"], sensitivity_rows)
    write_df(outputs["reference metrics A vs B"], ref_rows)
    write_df(outputs["clean-core composition"], clean_comp_rows)
    write_df(outputs["clean-core metrics"], clean_metric_rows)
    write_df(outputs["clean-core source-height metrics"], clean_source_height_rows)
    write_df(outputs["selection regret pixel-weighted"], regret_metric_rows)
    write_df(outputs["fused high-cloud composition"], high_comp_rows)
    write_df(outputs["fused high-cloud metrics"], high_metric_rows)
    write_df(outputs["fused high-cloud cases"], high_cases[:25])
    write_df(outputs["fused high-cloud source-pair disagreement"], accum["high_pair_disagreement"])
    write_df(outputs["semantic variable audit"], make_semantic_rows())

    ref_df = pd.DataFrame(ref_rows)
    ab_df = pd.DataFrame(ab_rows)
    regret_df = pd.DataFrame(regret_metric_rows)
    clean_df = pd.DataFrame(clean_metric_rows)
    summary = {
        "sample_count": int(len(manifest)),
        "ab_bias_km": float(ab_df.loc[(ab_df["policy"] == "A_inclusive_binary") & (ab_df["comparison"] == "B_minus_A_effective_height"), "bias_km"].iloc[0]) if not ab_df.empty else math.nan,
        "ab_mae_km": float(ab_df.loc[(ab_df["policy"] == "A_inclusive_binary") & (ab_df["comparison"] == "B_minus_A_effective_height"), "mae_km"].iloc[0]) if not ab_df.empty else math.nan,
    }
    def metric_value(df: pd.DataFrame, filt: pd.Series, col: str) -> float:
        sub = df[filt]
        return float(sub[col].iloc[0]) if not sub.empty and col in sub else math.nan

    a_all = (ref_df["policy"] == "A_inclusive_binary") & (ref_df["domain"] == "D1_both_cloud") & (ref_df["reference_band"] == "A_band") & (ref_df["group"] == "ALL")
    b_all = (ref_df["policy"] == "A_inclusive_binary") & (ref_df["domain"] == "D1_both_cloud") & (ref_df["reference_band"] == "B_band") & (ref_df["group"] == "ALL")
    a_high = (ref_df["policy"] == "A_inclusive_binary") & (ref_df["domain"] == "D7_high_cloud") & (ref_df["reference_band"] == "A_band") & (ref_df["group"] == "ALL")
    b_high = (ref_df["policy"] == "A_inclusive_binary") & (ref_df["domain"] == "D7_high_cloud") & (ref_df["reference_band"] == "B_band") & (ref_df["group"] == "ALL")
    summary["policy_a_b_minus_a_mae_km"] = metric_value(ref_df, b_all, "mae_km") - metric_value(ref_df, a_all, "mae_km")
    summary["policy_a_high_b_minus_a_mae_km"] = metric_value(ref_df, b_high, "mae_km") - metric_value(ref_df, a_high, "mae_km")

    regret_mask = (regret_df["policy"] == "A_inclusive_binary") & (regret_df["pixel_group"] == "ALL_VALID_CTH")
    if not regret_df[regret_mask].empty:
        r = regret_df[regret_mask].iloc[0]
        summary.update(
            {
                "regret_policy_a_n": int(r["n_valid_cth"]),
                "regret_policy_a_current_mae": float(r["current_selected_mae_km"]),
                "regret_policy_a_best_mae": float(r["best_available_mae_km"]),
                "regret_policy_a_regret": float(r["selection_regret_mae_km"]),
            }
        )
    summary["clean_core_mae"] = metric_value(clean_df, (clean_df["policy"] == "A_inclusive_binary") & (clean_df["clean_group"] == "clean_core"), "mae_km")
    summary["boundary_mae"] = metric_value(clean_df, (clean_df["policy"] == "A_inclusive_binary") & (clean_df["clean_group"] == "boundary_or_broken_cloud"), "mae_km")

    manifest_doc = {
        "stage_id": STAGE_ID,
        "qc_name": "stage_10_qc",
        "generated_utc": base.utc_now(),
        "input_sample_manifest": str(args.sample_manifest),
        "stage10_output_dir": str(args.stage10_output_dir),
        "sample_count": int(len(manifest)),
        "epic_reference_primary": A_BAND,
        "epic_reference_sensitivity": B_BAND,
        "warnings": [
            "EPIC A/B variables are effective cloud height; GEO sources are treated as cloud top height based on local metadata and mapping evidence.",
            "best_available is a retrospective EPIC-referenced diagnostic oracle, not an operational source-selection rule.",
            "Pixel counts in stage_10_qc_selection_regret_pixel_weighted.csv are true pixel-weighted totals; they intentionally differ from the earlier sample-mean regret summary.",
        ],
        "outputs": {k: str(v) for k, v in outputs.items()},
        "summary": summary,
    }
    outputs["manifest"].write_text(json.dumps(manifest_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(out_dir, outputs, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
