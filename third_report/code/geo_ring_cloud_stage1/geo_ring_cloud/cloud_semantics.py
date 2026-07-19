"""Cloud-code semantics and array validity contracts shared across stages."""

from __future__ import annotations

from typing import Any

import numpy as np


COMPONENT_ROLE = "cloud_semantics"

VALIDITY_PRIORITY = [
    "cloud_mask",
    "cloud_top_height_km",
    "cloud_top_temperature_K",
    "cloud_top_pressure_hPa",
    "cloud_probability",
    "cloud_phase",
    "cloud_type",
    "cloud_optical_thickness",
    "cloud_effective_radius_um",
    "cloud_water_path_g_m2",
    "sensor_zenith_angle",
    "solar_zenith_angle",
]
QUICKLOOK_PRIORITY = VALIDITY_PRIORITY

CATEGORICAL_VARS = {
    "cloud_mask",
    "cloud_type",
    "cloud_phase",
    "quality_flag_raw",
    "quality_flag_standard",
    "valid_mask",
}

CATEGORY_FILL_VALUES = {
    "cloud_mask": {-128, 126, 127, 255},
    "cloud_type": {-128, 126, 127, 255},
    "cloud_phase": {-128, 126, 127, 255},
    "quality_flag_raw": {-128, 126, 127, 255, 32767, 65535},
    "quality_flag_standard": {-128, 126, 127, 255},
    "valid_mask": {-128, 126, 127, 255},
}

CLOUD_MASK_CODE_TABLES: dict[tuple[str, str], dict[int, dict[str, Any]]] = {
    ("FY4B", "CLM"): {
        0: {"meaning": "cloud", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        1: {"meaning": "probably_cloud", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        2: {"meaning": "probably_clear", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        3: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "FY4B CLM PDF Description"},
        126: {"meaning": "space", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "FY4B CLM PDF Description"},
        127: {"meaning": "fillvalue", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": False, "valid_for_fusion": False, "meaning_source": "FY4B CLM PDF Description"},
    },
    ("GOES", "ACMF"): {
        0: {"meaning": "clear_or_probably_clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "GOES ACMF flag_meanings"},
        1: {"meaning": "cloudy_or_probably_cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "GOES ACMF flag_meanings"},
        255: {"meaning": "fill_or_off_disc", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "GOES ACMF _FillValue plus disk-edge spatial pattern"},
    },
    ("Himawari", "CMSK"): {
        0: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        1: {"meaning": "probably_clear", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        2: {"meaning": "probably_cloudy", "is_cloudy": False, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        3: {"meaning": "cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Himawari CMSK flag_meanings"},
        -128: {"meaning": "fill_or_off_earth", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "Himawari CMSK _FillValue plus radial off-earth pattern"},
    },
    ("Meteosat", "CLM"): {
        0: {"meaning": "clear_sky_over_water_inferred", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        1: {"meaning": "clear_sky_over_land_inferred", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        2: {"meaning": "cloud_inferred", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "EUMETSAT CLM abstract plus spatial inference"},
        3: {"meaning": "not_processed_off_earth_disc_inferred", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": True, "valid_for_fusion": False, "meaning_source": "EUMETSAT CLM abstract plus radial edge pattern"},
    },
    ("CLAAS3", "CMA"): {
        0: {"meaning": "clear", "is_cloudy": False, "is_clear": True, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "CLAAS-3 CMA flag_values/flag_meanings"},
        3: {"meaning": "cloudy", "is_cloudy": True, "is_clear": False, "is_off_disc": False, "valid_for_display": True, "valid_for_fusion": True, "meaning_source": "Canonicalized from CLAAS-3 CMA cloudy code 1"},
        -9999: {"meaning": "fill_or_off_disc", "is_cloudy": False, "is_clear": False, "is_off_disc": True, "valid_for_display": False, "valid_for_fusion": False, "meaning_source": "CLAAS-3 adapter fill policy"},
    },
}

__all__ = [
    "VALIDITY_PRIORITY",
    "QUICKLOOK_PRIORITY",
    "CATEGORICAL_VARS",
    "CATEGORY_FILL_VALUES",
    "CLOUD_MASK_CODE_TABLES",
    "cloud_mask_semantics",
    "cloud_mask_masks",
    "cloud_mask_rows",
    "reproject_mask_for_use",
    "array_valid_mask",
    "add_valid_and_quality",
]


def cloud_mask_semantics(satellite: str, product: str) -> dict[int, dict[str, Any]]:
    sat = str(satellite)
    prod = str(product)
    for family in ("GOES", "Himawari", "Meteosat", "CLAAS3"):
        if sat.startswith(family) and (family, prod) in CLOUD_MASK_CODE_TABLES:
            return CLOUD_MASK_CODE_TABLES[(family, prod)]
    return CLOUD_MASK_CODE_TABLES.get((sat, prod), {})


def array_valid_mask(name: str, arr: np.ndarray) -> np.ndarray | None:
    values = np.asarray(arr)
    if values.ndim < 2:
        return None
    if values.dtype.kind in "f":
        valid = np.isfinite(values)
        for fill in CATEGORY_FILL_VALUES.get(name, set()):
            valid &= ~np.isclose(values, float(fill))
        return valid
    if values.dtype.kind in "iu":
        valid = np.ones(values.shape, dtype=bool)
        for fill in CATEGORY_FILL_VALUES.get(name, set()):
            valid &= values != fill
        return valid
    return None


def cloud_mask_masks(
    satellite: str, product: str, arr: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    values = np.asarray(arr)
    table = cloud_mask_semantics(satellite, product)
    if not table:
        base = array_valid_mask("cloud_mask", values)
        if base is None:
            base = np.zeros(values.shape, dtype=bool)
        return base, base, np.zeros(values.shape, dtype=bool)
    display_codes = np.asarray(
        sorted(code for code, meta in table.items() if meta.get("valid_for_display")), dtype=np.float32
    )
    fusion_codes = np.asarray(
        sorted(code for code, meta in table.items() if meta.get("valid_for_fusion")), dtype=np.float32
    )
    off_disc_codes = np.asarray(
        sorted(code for code, meta in table.items() if meta.get("is_off_disc")), dtype=np.float32
    )
    finite = np.isfinite(values) if values.dtype.kind == "f" else np.ones(values.shape, dtype=bool)
    comparable = values.astype(np.float32, copy=False)
    return (
        finite & np.isin(comparable, display_codes),
        finite & np.isin(comparable, fusion_codes),
        finite & np.isin(comparable, off_disc_codes),
    )


def cloud_mask_rows(
    satellite: str,
    product: str,
    native_arr: np.ndarray,
    native_attrs: dict[str, Any] | None,
    grid_arr: np.ndarray,
    display_valid_mask: np.ndarray,
) -> list[dict[str, Any]]:
    attrs = native_attrs or {}
    table = cloud_mask_semantics(satellite, product)
    native_values, native_counts = np.unique(np.asarray(native_arr), return_counts=True)
    native_count_map = {float(value): int(count) for value, count in zip(native_values, native_counts)}
    selected_grid = np.asarray(grid_arr)[np.asarray(display_valid_mask).astype(bool)]
    grid_values, grid_counts = np.unique(selected_grid, return_counts=True)
    grid_count_map = {float(value): int(count) for value, count in zip(grid_values, grid_counts)}
    all_values = sorted(set(native_count_map) | set(grid_count_map) | {float(key) for key in table})
    rows: list[dict[str, Any]] = []
    for value in all_values:
        meta = table.get(int(value), {})
        rows.append({
            "satellite": satellite,
            "product": product,
            "value": int(value) if float(value).is_integer() else float(value),
            "count_native": native_count_map.get(value, 0),
            "count_reprojected_display": grid_count_map.get(value, 0),
            "metadata_meaning": meta.get("meaning", ""),
            "meaning_source": meta.get("meaning_source", ""),
            "flag_meanings_attr": attrs.get("flag_meanings", ""),
            "flag_values_attr": attrs.get("flag_values", ""),
            "description_attr": attrs.get("Description", attrs.get("description", "")),
            "fill_value_attr": attrs.get("_FillValue", attrs.get("missing_value", "")),
            "is_cloudy": bool(meta.get("is_cloudy", False)),
            "is_clear": bool(meta.get("is_clear", False)),
            "is_off_disc": bool(meta.get("is_off_disc", False)),
            "valid_for_display": bool(meta.get("valid_for_display", False)),
            "valid_for_fusion": bool(meta.get("valid_for_fusion", False)),
        })
    return rows


def reproject_mask_for_use(
    arrays: dict[str, np.ndarray], variable: str, use: str = "fusion"
) -> np.ndarray:
    if variable == "cloud_mask":
        if use == "fusion" and "fusion_valid_mask" in arrays:
            return np.asarray(arrays["fusion_valid_mask"]).astype(bool)
        if use == "display" and "display_valid_mask" in arrays:
            return np.asarray(arrays["display_valid_mask"]).astype(bool)
    if "valid_mask" in arrays:
        return np.asarray(arrays["valid_mask"]).astype(bool)
    reference = arrays.get(variable)
    if reference is None:
        return np.zeros((0, 0), dtype=bool)
    return np.ones(np.asarray(reference).shape, dtype=bool)


def add_valid_and_quality(arrays: dict[str, np.ndarray]) -> None:
    data_candidates = [
        name
        for name in VALIDITY_PRIORITY
        if name in arrays and np.asarray(arrays[name]).ndim >= 2 and name != "quality_flag_raw"
    ]
    shape = None
    if data_candidates:
        shape = np.asarray(arrays[data_candidates[0]]).shape
    elif "quality_flag_raw" in arrays and np.asarray(arrays["quality_flag_raw"]).ndim >= 2:
        shape = np.asarray(arrays["quality_flag_raw"]).shape
    if shape is not None:
        valid = np.ones(shape, dtype=bool)
        mask_inputs = data_candidates[:]
        if "quality_flag_raw" in arrays and np.asarray(arrays["quality_flag_raw"]).shape == shape:
            mask_inputs.append("quality_flag_raw")
        if not mask_inputs:
            mask_inputs = [
                name
                for name, value in arrays.items()
                if np.asarray(value).shape == shape and np.asarray(value).ndim >= 2
            ]
        for name in mask_inputs:
            mask = array_valid_mask(name, np.asarray(arrays[name]))
            if mask is not None and mask.shape == shape:
                valid &= mask
        arrays["valid_mask"] = valid.astype(np.uint8)
    if "quality_flag_raw" in arrays and "quality_flag_standard" not in arrays:
        quality = np.asarray(arrays["quality_flag_raw"])
        if quality.ndim >= 2:
            arrays["quality_flag_standard"] = np.where(
                quality == 0, 3, np.where(np.isfinite(quality), 1, 0)
            ).astype(np.uint8)
