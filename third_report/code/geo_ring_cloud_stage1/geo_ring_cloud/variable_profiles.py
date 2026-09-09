"""Named scientific variable profiles for GEO-ring processing runs."""

from __future__ import annotations

import os


COMPONENT_ROLE = "variable_profile"

CORE_REQUIRED = ("cloud_mask", "cloud_top_height_km")
CORE_OPTIONAL = (
    "cloud_top_temperature_K", "cloud_top_pressure_hPa", "cloud_phase", "cloud_type",
    "cloud_optical_thickness", "cloud_effective_radius_um", "cloud_probability",
)
PROFILES = {"clm_cth_core": {"required": CORE_REQUIRED, "optional": CORE_OPTIONAL}}


def active_profile() -> str:
    profile = os.environ.get("GEO_RING_VARIABLE_PROFILE", "full").strip() or "full"
    if profile != "full" and profile not in PROFILES:
        raise ValueError(f"unknown GEO_RING_VARIABLE_PROFILE={profile!r}")
    return profile


def requested_variables() -> tuple[str, ...] | None:
    profile = active_profile()
    return None if profile == "full" else CORE_REQUIRED


def profile_manifest(available: set[str] | None = None) -> dict[str, object]:
    profile = active_profile()
    if profile == "full":
        return {"variable_profile": "full"}
    requested = list(CORE_REQUIRED)
    available = available or set()
    return {
        "variable_profile": profile,
        "requested_variables": requested,
        "available_variables": sorted(available.intersection(requested)),
        "missing_optional_variables": sorted(set(CORE_OPTIONAL) - available),
        "missing_optional_status": "SKIPPED_OPTIONAL_UNAVAILABLE",
    }
