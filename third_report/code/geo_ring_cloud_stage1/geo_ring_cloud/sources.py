"""Authoritative source registry and source-profile rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


COMPONENT_ROLE = "source_registry"
REGISTRY_VERSION = "geo-ring-cloud-sources-v1"
SOURCE_PROFILES = ("operational_baseline", "claas3_candidate")


@dataclass(frozen=True)
class SourceDefinition:
    source_key: str
    source_id: int
    family: str
    platform: str
    processing_stream: str
    service_longitude_deg: float
    products: tuple[str, ...]
    cadence_minutes: int
    time_tolerance_minutes: float


SOURCE_DEFINITIONS: tuple[SourceDefinition, ...] = (
    SourceDefinition("GOES-16", 1, "GOES", "GOES-16", "NOAA_ABI_L2", -75.2, ("ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF"), 10, 15.0),
    SourceDefinition("GOES-18", 2, "GOES", "GOES-18", "NOAA_ABI_L2", -137.2, ("ACMF", "ACHAF", "ACHTF", "CTPF", "ACTPF", "CODF", "CPSF"), 10, 15.0),
    SourceDefinition("FY4B", 3, "FY4B", "FY-4B", "CMA_AGRI_L2", 133.0, ("CLM", "CLP", "CLT", "CTH", "CTT", "CTP", "GEO"), 15, 15.0),
    SourceDefinition("Himawari-9", 4, "Himawari", "Himawari-9", "JMA_AHI_CLOUD", 140.7, ("CMSK", "CHGT"), 10, 15.0),
    SourceDefinition("Meteosat-0deg", 5, "Meteosat", "METEOSAT-10", "EUMETSAT_OPERATIONAL_MSG", 0.0, ("CLM", "CTH"), 15, 15.0),
    SourceDefinition("Meteosat-IODC", 6, "Meteosat", "METEOSAT-IODC", "EUMETSAT_OPERATIONAL_MSG_IODC", 45.5, ("CLM", "CTH"), 15, 15.0),
    SourceDefinition("CLAAS3-0deg", 7, "CLAAS3", "METEOSAT-10", "CM_SAF_CLAAS_V003_ICDR", 0.0, ("CMA", "CTX", "CPP"), 15, 30.0),
)

SOURCE_BY_KEY = {item.source_key: item for item in SOURCE_DEFINITIONS}
SOURCE_ID_MAP = {item.source_key: item.source_id for item in SOURCE_DEFINITIONS}
SOURCE_ID_TO_KEY = {item.source_id: item.source_key for item in SOURCE_DEFINITIONS}
LEGACY_TIE_ORDER = [item.source_key for item in SOURCE_DEFINITIONS if item.source_id <= 6]


BASELINE_VARIABLE_RULES: dict[str, list[dict[str, str]]] = {
    "cloud_mask": [
        {"source_key": "FY4B", "product": "CLM"},
        {"source_key": "GOES-16", "product": "ACMF"},
        {"source_key": "GOES-18", "product": "ACMF"},
        {"source_key": "Himawari-9", "product": "CMSK"},
        {"source_key": "Meteosat-0deg", "product": "CLM"},
        {"source_key": "Meteosat-IODC", "product": "CLM"},
    ],
    "cloud_top_height_km": [
        {"source_key": "FY4B", "product": "CTH"},
        {"source_key": "GOES-16", "product": "ACHAF"},
        {"source_key": "GOES-18", "product": "ACHAF"},
        {"source_key": "Himawari-9", "product": "CHGT"},
        {"source_key": "Meteosat-0deg", "product": "CTH"},
        {"source_key": "Meteosat-IODC", "product": "CTH"},
    ],
    "cloud_top_temperature_K": [
        {"source_key": "FY4B", "product": "CTT"},
        {"source_key": "GOES-16", "product": "ACHTF"},
        {"source_key": "GOES-18", "product": "ACHTF"},
        {"source_key": "Himawari-9", "product": "CHGT"},
    ],
    "cloud_top_pressure_hPa": [
        {"source_key": "FY4B", "product": "CTP"},
        {"source_key": "GOES-16", "product": "CTPF"},
        {"source_key": "GOES-18", "product": "CTPF"},
        {"source_key": "Himawari-9", "product": "CHGT"},
    ],
    "cloud_phase": [
        {"source_key": "FY4B", "product": "CLP"},
        {"source_key": "GOES-16", "product": "ACTPF"},
        {"source_key": "GOES-18", "product": "ACTPF"},
    ],
    "cloud_type": [{"source_key": "FY4B", "product": "CLT"}],
    "cloud_optical_thickness": [
        {"source_key": "GOES-16", "product": "CODF"},
        {"source_key": "GOES-18", "product": "CODF"},
        {"source_key": "Himawari-9", "product": "CHGT"},
    ],
    "cloud_effective_radius_um": [
        {"source_key": "GOES-16", "product": "CPSF"},
        {"source_key": "GOES-18", "product": "CPSF"},
    ],
}


def validate_profile(profile: str) -> str:
    value = str(profile).strip()
    if value not in SOURCE_PROFILES:
        raise ValueError(f"unsupported source profile: {value}")
    return value


def tie_order(profile: str) -> list[str]:
    profile = validate_profile(profile)
    if profile == "operational_baseline":
        return LEGACY_TIE_ORDER[:]
    return LEGACY_TIE_ORDER + ["CLAAS3-0deg"]


def variable_rules(profile: str) -> dict[str, list[dict[str, str]]]:
    profile = validate_profile(profile)
    rules = {name: [dict(item) for item in items] for name, items in BASELINE_VARIABLE_RULES.items()}
    if profile == "operational_baseline":
        return rules

    def replace(variable: str, old: str, new_product: str) -> None:
        rules[variable] = [
            {"source_key": "CLAAS3-0deg", "product": new_product} if item["source_key"] == old else item
            for item in rules[variable]
        ]

    replace("cloud_mask", "Meteosat-0deg", "CMA")
    replace("cloud_top_height_km", "Meteosat-0deg", "CTX")
    additions = {
        "cloud_probability": "CMA",
        "cloud_top_temperature_K": "CTX",
        "cloud_top_pressure_hPa": "CTX",
        "cloud_phase": "CPP",
        "cloud_optical_thickness": "CPP",
        "cloud_effective_radius_um": "CPP",
        "cloud_water_path_g_m2": "CPP",
    }
    for variable, product in additions.items():
        rules.setdefault(variable, []).append({"source_key": "CLAAS3-0deg", "product": product})
    return rules


def registry_payload() -> dict[str, Any]:
    return {
        "registry_version": REGISTRY_VERSION,
        "source_profiles": list(SOURCE_PROFILES),
        "sources": [asdict(item) for item in SOURCE_DEFINITIONS],
        "profile_rules": {profile: variable_rules(profile) for profile in SOURCE_PROFILES},
    }


__all__ = [
    "BASELINE_VARIABLE_RULES",
    "LEGACY_TIE_ORDER",
    "REGISTRY_VERSION",
    "SOURCE_BY_KEY",
    "SOURCE_DEFINITIONS",
    "SOURCE_ID_MAP",
    "SOURCE_ID_TO_KEY",
    "SOURCE_PROFILES",
    "SourceDefinition",
    "registry_payload",
    "tie_order",
    "validate_profile",
    "variable_rules",
]
