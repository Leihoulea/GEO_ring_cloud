from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

CORE_CODE_ROOT = Path(__file__).resolve().parents[1] / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))

from geo_ring_cloud.paths import THIRD_REPORT_ROOT  # noqa: E402


ROOT = THIRD_REPORT_ROOT
REPORT_DIR = ROOT / "reports"
CRED_FILE = ROOT / "eumetsat_dataservices_API.txt"

COLLECTION_ID = "EO:EUM:DAT:0820"
CLAAS_ACRONYM = "CLAAS_V003"
DEFAULT_PROXY = "http://127.0.0.1:7897"

SEARCH_DATES = ["2024-03-01", "2024-03-12", "2024-03-31"]
SEARCH_HOURS = ["00:00", "06:00", "12:00", "18:00"]

DATASTORE_COLLECTION_URL = (
    "https://api.eumetsat.int/data/browse/1.0.0/collections/"
    f"{quote(COLLECTION_ID, safe='')}?format=json"
)
DATASTORE_SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
CMSAF_DOI_URL = "https://wui.cmsaf.eu/safira/action/viewDoiDetails?acronym=CLAAS_V003"

OUTPUT_AVAILABILITY_CSV = REPORT_DIR / "claas3_202403_availability_audit.csv"
OUTPUT_AVAILABILITY_MD = REPORT_DIR / "claas3_202403_availability_audit.md"
OUTPUT_VARIABLES_CSV = REPORT_DIR / "claas3_sample_variables_inventory.csv"
OUTPUT_MAPPING_MD = REPORT_DIR / "claas3_variable_mapping_proposal.md"


@dataclass(frozen=True)
class ProductPage:
    code: str
    product_name: str
    expected_standard_vars: str
    detail_url: str
    period_url: str
    eid: str
    fid: str
    order_tid: str = "55"
    order_checksum: str = "02d44918a563d52c66f72fc1c36d7d7f"


PRODUCT_PAGES = [
    ProductPage(
        code="CMA",
        product_name="CMA - Cloud mask",
        expected_standard_vars="cloud_mask;cloud_probability;quality_flag",
        detail_url="https://wui.cmsaf.eu/safira/action/viewProduktDetails?fid=38&eid=22214_22235",
        period_url="https://wui.cmsaf.eu/safira/action/viewPeriodEntry?fid=38&eid=22214_22235",
        eid="22214_22235",
        fid="38",
    ),
    ProductPage(
        code="CTX",
        product_name="CTX - Instantaneous CTT, CTP and CTH",
        expected_standard_vars="cloud_top_temperature_k;cloud_top_pressure_hpa;cloud_top_height_km;quality_flag",
        detail_url="https://wui.cmsaf.eu/safira/action/viewProduktDetails?fid=38&eid=22223_22244",
        period_url="https://wui.cmsaf.eu/safira/action/viewPeriodEntry?fid=38&eid=22223_22244",
        eid="22223_22244",
        fid="38",
    ),
    ProductPage(
        code="CPP",
        product_name="CPP - Instantaneous COT, CPH and CWP",
        expected_standard_vars="cloud_phase;cloud_optical_thickness;cloud_effective_radius_um;cloud_water_path_g_m2;quality_flag",
        detail_url="https://wui.cmsaf.eu/safira/action/viewProduktDetails?fid=38&eid=22218_22239",
        period_url="https://wui.cmsaf.eu/safira/action/viewPeriodEntry?fid=38&eid=22218_22239",
        eid="22218_22239",
        fid="38",
    ),
]


def ensure_dirs() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def ensure_proxy_env() -> None:
    if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        return
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ[key] = DEFAULT_PROXY


def proxies() -> dict[str, str]:
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or DEFAULT_PROXY
    )
    return {"http": proxy, "https": proxy} if proxy else {}


def read_credentials() -> tuple[str, str]:
    text = CRED_FILE.read_text(encoding="utf-8", errors="ignore")
    key = ""
    secret = ""
    for line in text.splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
        elif ":" in line:
            name, value = line.split(":", 1)
        else:
            continue
        low = name.lower()
        value = value.strip().strip("\"'")
        if "consumer" in low and "key" in low:
            key = value
        elif "consumer" in low and "secret" in low:
            secret = value
    if not key or not secret:
        raise RuntimeError(f"Unable to read EUMETSAT consumer key/secret from {CRED_FILE}")
    return key, secret


def get_token() -> str:
    key, secret = read_credentials()
    last_error = ""
    for attempt in range(4):
        try:
            response = requests.post(
                "https://api.eumetsat.int/token",
                auth=(key, secret),
                data={"grant_type": "client_credentials"},
                timeout=60,
                proxies=proxies(),
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if token:
                return str(token)
            last_error = "token missing in response"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2 + attempt)
    raise RuntimeError(f"Failed to get EUMETSAT token: {last_error}")


def request_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    raise_for_status: bool = False,
) -> tuple[int, str, str]:
    client = session or requests
    response = client.request(
        method,
        url,
        params=params,
        headers=headers,
        data=data,
        timeout=90,
        proxies=proxies(),
        allow_redirects=True,
    )
    if raise_for_status:
        response.raise_for_status()
    return response.status_code, response.text, response.url


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str, str]:
    status, text, final_url = request_text(url, params=params, headers=headers)
    try:
        payload = json.loads(text) if text else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return status, payload, final_url, text[:500]


def strip_tags(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def extract_field(plain_text: str, label: str) -> str:
    anchor = plain_text.lower().find(label.lower())
    if anchor < 0:
        return ""
    tail = plain_text[anchor + len(label): anchor + len(label) + 240]
    tail = re.sub(r"^[\s:|]+", "", tail)
    enders = [
        "Product group",
        "Product family",
        "Temporal coverage",
        "Spatial resolution",
        "Area",
        "Access",
        "Ordering",
        "Preview",
    ]
    end_positions = [tail.find(marker) for marker in enders if tail.find(marker) > 0]
    if end_positions:
        tail = tail[: min(end_positions)]
    return tail.strip(" |")


def parse_hidden_inputs(html: str) -> dict[str, str]:
    items: dict[str, str] = {}
    for name, value in re.findall(
        r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
        html,
        flags=re.I,
    ):
        items[name] = unescape(value)
    return items


def infer_stream(doi_plain: str) -> str:
    low = doi_plain.lower()
    if "icdr" in low and "cdr" in low:
        return "ICDR"
    if "icdr" in low:
        return "ICDR"
    if "cdr" in low:
        return "CDR"
    return "UNKNOWN"


def probe_datastore() -> dict[str, Any]:
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}
    collection_status, collection_json, collection_url, collection_excerpt = request_json(
        DATASTORE_COLLECTION_URL,
        headers=headers,
    )
    results = {
        "collection_status": collection_status,
        "collection_title": collection_json.get("title", ""),
        "collection_url": collection_url,
        "collection_excerpt": collection_excerpt,
        "search_checks": [],
    }
    for day in SEARCH_DATES:
        params = {
            "format": "json",
            "pi": COLLECTION_ID,
            "dtstart": f"{day}T00:00:00Z",
            "dtend": f"{day}T23:59:59Z",
            "c": "1000",
        }
        status, payload, final_url, excerpt = request_json(
            DATASTORE_SEARCH_URL,
            params=params,
            headers=headers,
        )
        features = payload.get("features")
        results["search_checks"].append(
            {
                "date": day,
                "status": status,
                "count": len(features) if isinstance(features, list) else 0,
                "url": final_url,
                "excerpt": excerpt,
            }
        )
    return results


def fetch_product_metadata(page: ProductPage) -> dict[str, str]:
    status, html, final_url = request_text(page.detail_url)
    plain = strip_tags(html)
    return {
        "detail_status": str(status),
        "detail_url": final_url,
        "temporal_coverage": extract_field(plain, "Temporal coverage"),
        "spatial_resolution": extract_field(plain, "Spatial resolution"),
        "area": extract_field(plain, "Area"),
        "product_group": extract_field(plain, "Product group"),
        "product_family": extract_field(plain, "Product family"),
        "plain_excerpt": plain[:1000],
    }


def cart_probe(page: ProductPage, day: str) -> dict[str, str]:
    session = requests.Session()
    period_status, period_html, period_url = request_text(page.period_url, session=session)
    hidden = parse_hidden_inputs(period_html)
    form = {
        "fid": hidden.get("fid", page.fid),
        "eid": hidden.get("eid", page.eid),
        "orderCartID": hidden.get("orderCartID", ""),
        "tid": hidden.get("tid", page.order_tid),
        "checksum": hidden.get("checksum", page.order_checksum),
        "beginnDateString": day,
        "standingOrderSelected": hidden.get("standingOrderSelected", "0"),
        "endeDateString": day,
        "format": hidden.get("format", "NetCDF4"),
    }
    post_status, post_html, post_url = request_text(
        "https://wui.cmsaf.eu/safira/action/storeToOrderCard",
        method="POST",
        data=form,
        session=session,
        raise_for_status=True,
    )
    accepted = bool(re.search(r"Order cart|removeOrderItem|viewCurrentOrder", post_html, flags=re.I))
    notes = []
    if accepted:
        notes.append("order_cart_accepts_day_range")
    if "NetCDF4" in post_html:
        notes.append("netcdf4_visible")
    size_match = re.search(r"(\d+\.\d+\s*GB)", post_html)
    if size_match:
        notes.append(f"cart_size_hint={size_match.group(1)}")
    return {
        "period_status": str(period_status),
        "period_url": period_url,
        "cart_status": str(post_status),
        "cart_url": post_url,
        "cart_result": "DAY_ACCEPTED" if accepted else "DAY_NOT_CONFIRMED",
        "cart_notes": "; ".join(notes),
    }


def build_variable_inventory() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(
        product_code: str,
        product_name: str,
        variable_name: str,
        standard_guess: str,
        category: str,
        dims: str,
        units: str,
        notes: str,
    ) -> None:
        rows.append(
            {
                "inspection_basis": "Official CLAAS-3 PUM table + CM SAF product pages",
                "product_code": product_code,
                "product_name": product_name,
                "variable_name": variable_name,
                "standard_variable_guess": standard_guess,
                "category": category,
                "dimensions": dims,
                "units_or_encoding": units,
                "notes": notes,
                "sample_file_inspection_status": (
                    "No direct product NetCDF sample retrieved from current WUI/Data Store path; "
                    "inventory derived from official documentation."
                ),
            }
        )

    common_vars = [
        ("ALL_L2", "CLAAS-3 Level-2 common", "time", "observation_time", "time", "time", "", "Per-file observation time."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "time_bnds", "time_bounds", "time", "time,bnds", "", "Per-file time bounds."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "georef_offset_corrected", "georef_offset_corrected", "geometry", "georef_offset_corrected", "", "Coordinate flag used by main and auxiliary files."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "projection", "geostationary_projection", "geometry", "", "CF grid_mapping", "Native MSG/SEVIRI projection metadata."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "platform_flag", "platform_flag", "metadata", "time", "", "MSG platform selector."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "subsatellite_alt", "satellite_height", "geometry", "time", "m", "Satellite altitude metadata."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "subsatellite_lat", "satellite_subpoint_latitude", "geometry", "time", "degrees_north", "Sub-satellite latitude metadata."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "subsatellite_lon", "satellite_subpoint_longitude", "geometry", "time", "degrees_east", "Sub-satellite longitude metadata."),
        ("ALL_L2", "CLAAS-3 Level-2 common", "record_status", "record_status", "quality", "time", "", "File-level record status."),
    ]
    for row in common_vars:
        add(*row)

    cma_vars = [
        ("CMA", "CMA - Cloud mask", "cma", "cloud_mask", "core_cloud", "x,y,time", "flag/enum", "Primary cloud mask."),
        ("CMA", "CMA - Cloud mask", "cma_prob", "cloud_probability", "core_cloud", "x,y,time", "scaled probability", "Probabilistic cloud mask."),
        ("CMA", "CMA - Cloud mask", "status_flag", "quality_flag", "quality", "x,y,time", "bitfield/flag", "Retrieval or status flag candidate."),
        ("CMA", "CMA - Cloud mask", "quality", "quality_flag", "quality", "x,y,time", "flag/enum", "Quality flag candidate."),
        ("CMA", "CMA - Cloud mask", "conditions", "retrieval_conditions", "quality", "x,y,time", "bitfield/flag", "Retrieval conditions."),
    ]
    for row in cma_vars:
        add(*row)

    ctx_vars = [
        ("CTX", "CTX - Instantaneous CTT, CTP and CTH", "ctt", "cloud_top_temperature_k", "cloud_top", "x,y,time", "K", "Primary cloud top temperature."),
        ("CTX", "CTX - Instantaneous CTT, CTP and CTH", "ctp", "cloud_top_pressure_hpa", "cloud_top", "x,y,time", "hPa_or_Pa", "Primary cloud top pressure; confirm actual unit from real sample attrs."),
        ("CTX", "CTX - Instantaneous CTT, CTP and CTH", "cth", "cloud_top_height_km", "cloud_top", "x,y,time", "m_or_km", "Primary cloud top height; convert to km if needed."),
        ("CTX", "CTX - Instantaneous CTT, CTP and CTH", "quality", "quality_flag", "quality", "x,y,time", "flag/enum", "Quality flag candidate."),
        ("CTX", "CTX - Instantaneous CTT, CTP and CTH", "conditions", "retrieval_conditions", "quality", "x,y,time", "bitfield/flag", "Retrieval conditions."),
    ]
    for row in ctx_vars:
        add(*row)

    cpp_vars = [
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cph", "cloud_phase", "microphysics", "x,y,time", "flag/enum", "Preferred cloud phase candidate."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cph_16", "cloud_phase_alt", "microphysics", "x,y,time", "flag/enum", "Alternate phase variant; confirm with real sample attrs."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cwp", "cloud_water_path_g_m2", "microphysics", "x,y,time", "scaled", "Preferred cloud water path candidate."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cwp_16", "cloud_water_path_alt", "microphysics", "x,y,time", "scaled", "Alternate water path variant."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cot", "cloud_optical_thickness", "microphysics", "x,y,time", "scaled", "Preferred cloud optical thickness candidate."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cot_16", "cloud_optical_thickness_alt", "microphysics", "x,y,time", "scaled", "Alternate optical thickness variant."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cre", "cloud_effective_radius_um", "microphysics", "x,y,time", "scaled", "Preferred effective radius candidate."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cre_16", "cloud_effective_radius_alt", "microphysics", "x,y,time", "scaled", "Alternate effective radius variant."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cdnc", "cloud_droplet_number_concentration", "microphysics", "x,y,time", "", "Potential future-use variable."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cgt", "cloud_geometrical_thickness_or_related", "microphysics", "x,y,time", "", "Needs real sample attribute check before use."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cwp_unc", "cloud_water_path_uncertainty", "uncertainty", "x,y,time", "", "Uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cwp_16_unc", "cloud_water_path_uncertainty_alt", "uncertainty", "x,y,time", "", "Alternate uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cot_unc", "cloud_optical_thickness_uncertainty", "uncertainty", "x,y,time", "", "Uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cot_16_unc", "cloud_optical_thickness_uncertainty_alt", "uncertainty", "x,y,time", "", "Alternate uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cre_unc", "cloud_effective_radius_uncertainty", "uncertainty", "x,y,time", "", "Uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "cre_16_unc", "cloud_effective_radius_uncertainty_alt", "uncertainty", "x,y,time", "", "Alternate uncertainty layer."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "h_sigma", "retrieval_sigma_or_spread", "uncertainty", "x,y,time", "", "Auxiliary retrieval spread variable."),
        ("CPP", "CPP - Instantaneous COT, CPH and CWP", "processing_flag", "quality_flag", "quality", "x,y,time", "bitfield/flag", "Primary QA candidate for CPP."),
    ]
    for row in cpp_vars:
        add(*row)

    aux_vars = [
        ("AUX", "CLAAS-3 Level-2 auxiliary", "lat", "latitude", "geometry", "georef_offset_corrected,x,y", "degrees_north", "Auxiliary latitude."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "lon", "longitude", "geometry", "georef_offset_corrected,x,y", "degrees_east", "Auxiliary longitude."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "acq_time", "acquisition_time", "time", "georef_offset_corrected,x,y", "", "Per-pixel acquisition time."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "lsm", "land_sea_mask", "auxiliary", "georef_offset_corrected,x,y", "flag", "Land-sea mask."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "alt", "surface_altitude", "auxiliary", "georef_offset_corrected,x,y", "m", "Surface altitude."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "pixel_area", "pixel_area", "auxiliary", "georef_offset_corrected,x,y", "km2_or_m2", "Pixel area."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "lon0", "service_longitude", "geometry", "lon0", "degrees_east", "Service longitudes for MSG slots."),
        ("AUX", "CLAAS-3 Level-2 auxiliary", "satzen", "sensor_zenith_angle", "geometry", "georef_offset_corrected,x,y,lon0", "degrees", "Per-pixel sensor zenith angle."),
    ]
    for row in aux_vars:
        add(*row)

    return rows


def build_mapping_md() -> str:
    return """# CLAAS-3 Variable Mapping Proposal

## Positioning

- CLAAS-3 is not the same product stream as MSG operational OCA/OCA-IODC or operational CLA/CLM.
- CLAAS-3 (`CLAAS_V003`, catalogue id `EO:EUM:DAT:0820`) is a CM SAF SEVIRI cloud data record family with ICDR extension.
- For `2024-03`, the relevant stream should be treated as `ICDR`, not legacy `CDR`.
- Because operational MSG `OCA/OCA-IODC` is not currently available to us via the present catalogue path for `2024-03`, CLAAS-3 should be integrated as a SEVIRI-derived cloud-property supplement.
- Operational `CLM-IODC` and `CTH-IODC` should remain the baseline operational mask/height products.

## Proposed mapping

| Standard variable | CLAAS-3 variable | Product | Recommendation | Notes |
|---|---|---|---|---|
| `cloud_mask` | `cma` | CMA | direct | Primary cloud mask |
| `cloud_probability` | `cma_prob` | CMA | direct | Probabilistic mask |
| `cloud_top_height_km` | `cth` | CTX | direct after unit check | Convert to km if file stores meters |
| `cloud_top_pressure_hpa` | `ctp` | CTX | direct after unit check | Confirm Pa vs hPa from real attrs |
| `cloud_top_temperature_k` | `ctt` | CTX | direct | Expected to be Kelvin |
| `cloud_phase` | `cph` | CPP | preferred | `cph_16` kept as alternate candidate pending real sample attrs |
| `cloud_optical_thickness` | `cot` | CPP | preferred | `cot_16` alternate candidate |
| `cloud_effective_radius_um` | `cre` | CPP | preferred | `cre_16` alternate candidate |
| `cloud_water_path_g_m2` | `cwp` | CPP | preferred | `cwp_16` alternate candidate |
| `quality_flag` | `quality` | CMA / CTX | keep raw | Keep `status_flag` and `conditions` alongside it |
| `quality_flag` | `processing_flag` | CPP | keep raw | Best current QA carrier for CPP |
| `latitude` | `lat` | AUX | direct | Comes from `claas3_level2_aux_data.nc` |
| `longitude` | `lon` | AUX | direct | Comes from `claas3_level2_aux_data.nc` |
| `sensor_zenith_angle` | `satzen` | AUX | direct | Useful for rating and overlap analysis |
| `observation_time` | `time`, `time_bnds`, `acq_time` | ALL/AUX | direct | File time plus per-pixel timing |
| `geostationary_projection` | `projection`, `subsatellite_lon`, `subsatellite_alt` | ALL_L2 | direct | Sufficient for native projection handling |

## Cautions

1. `cph/cph_16`, `cot/cot_16`, `cre/cre_16`, `cwp/cwp_16` are mapped from official documentation, not from a locally opened real CLAAS-3 sample product file.
2. `quality`, `status_flag`, `conditions`, and `processing_flag` still need code-table confirmation from a real sample or fuller documentation before we map them into a unified quality grade.
3. `lat/lon/satzen` are in the official auxiliary file, so production integration should treat the main product file and auxiliary geometry file as a matched pair.
"""


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def build_availability_rows(
    datastore: dict[str, Any],
    doi_status: int,
    doi_url: str,
    doi_plain: str,
    product_meta: dict[str, dict[str, str]],
    cart_results: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    stream = infer_stream(doi_plain)
    dataset_stream = "ICDR" if stream in {"ICDR", "UNKNOWN"} else "CDR"
    for product in PRODUCT_PAGES:
        meta = product_meta.get(product.code, {})
        for day in SEARCH_DATES:
            cart = cart_results.get((product.code, day), {})
            search_day = next((item for item in datastore.get("search_checks", []) if item["date"] == day), {})
            for hhmm in SEARCH_HOURS:
                rows.append(
                    {
                        "collection_id": COLLECTION_ID,
                        "acronym": CLAAS_ACRONYM,
                        "dataset_stream_for_2024_03": dataset_stream,
                        "product_code": product.code,
                        "product_name": product.product_name,
                        "target_time_utc": f"{day}T{hhmm}:00Z",
                        "target_sampling": "full_hour",
                        "native_sampling_claim": "15-minute instantaneous L2 on native SEVIRI grid",
                        "eumetsat_datastore_collection_status": str(datastore.get("collection_status", "")),
                        "eumetsat_datastore_day_search_status": str(search_day.get("status", "")),
                        "eumetsat_datastore_day_feature_count": str(search_day.get("count", "")),
                        "cmsaf_doi_page_status": str(doi_status),
                        "cmsaf_doi_url": doi_url,
                        "cmsaf_product_detail_status": meta.get("detail_status", ""),
                        "cmsaf_product_temporal_coverage": meta.get("temporal_coverage", ""),
                        "cmsaf_product_spatial_resolution": meta.get("spatial_resolution", ""),
                        "cmsaf_day_probe_result": cart.get("cart_result", "NOT_TESTED"),
                        "cmsaf_day_probe_notes": cart.get("cart_notes", ""),
                        "hour_level_availability_statement": (
                            "LIKELY_AVAILABLE_AS_L2_15MIN_ICDR"
                            if cart.get("cart_result") == "DAY_ACCEPTED"
                            else "NOT_DIRECTLY_ENUMERATED_PUBLICLY"
                        ),
                        "evidence_type": (
                            "CM SAF product definition + temporal coverage + day-level order acceptance"
                            if cart.get("cart_result") == "DAY_ACCEPTED"
                            else "CM SAF product definition only"
                        ),
                        "notes": (
                            "Public interface confirms product family and day-level availability; "
                            "individual full-hour files are inferred from the 15-minute instantaneous design."
                        ),
                    }
                )
    return rows


def build_report_md(
    datastore: dict[str, Any],
    doi_status: int,
    doi_url: str,
    doi_plain: str,
    product_meta: dict[str, dict[str, str]],
    cart_results: dict[tuple[str, str], dict[str, str]],
) -> str:
    stream = infer_stream(doi_plain)
    dataset_stream = "ICDR" if stream in {"ICDR", "UNKNOWN"} else "CDR"
    lines = [
        "# CLAAS-3 2024-03 Availability Audit",
        "",
        "## Executive conclusion",
        "",
        "- `EO:EUM:DAT:0820` is not directly discoverable for `2024-03` in the current EUMETSAT Data Store catalogue/API path we tested.",
        "- CLAAS-3 is not the same product stream as MSG operational OCA/OCA-IODC.",
        "- CLAAS-3 is a CM SAF SEVIRI cloud data record family with an ICDR extension.",
        f"- For `2024-03`, the practical classification should be `{dataset_stream}`.",
        "- Operational `CLM-IODC` and `CTH-IODC` should remain the operational baseline; CLAAS-3 should be used to supplement phase/COT/CER/CWP/CTP/CTT and related cloud-physics variables.",
        "",
        "## 1. EUMETSAT Data Store entry check",
        "",
        f"- Collection id: `{COLLECTION_ID}`",
        f"- Browse status: `{datastore.get('collection_status')}`",
        f"- Browse URL: `{datastore.get('collection_url')}`",
        "",
        "Daily search checks:",
    ]
    for item in datastore.get("search_checks", []):
        lines.append(
            f"- `{item['date']}`: status=`{item['status']}`, feature_count=`{item['count']}`, url=`{item['url']}`"
        )
    lines.extend(
        [
            "",
            "Interpretation: the current operational Data Store path does not expose CLAAS-3 the way it exposes CLM/CTH collections.",
            "",
            "## 2. CM SAF / SAFIRA check",
            "",
            f"- DOI page status: `{doi_status}`",
            f"- DOI page URL: `{doi_url}`",
            f"- Stream decision for 2024-03: `{dataset_stream}`",
            "- The CM SAF product description indicates native SEVIRI L2 instantaneous products with 15-minute repeat cycle and continued ICDR extension to the present.",
            "",
            "Priority instantaneous products found:",
            "",
            "| Product | Meaning | Engineering value |",
            "|---|---|---|",
            "| CMA | Cloud mask | cloud mask / probability / QA |",
            "| CTX | Cloud top temperature / pressure / height | CTT / CTP / CTH |",
            "| CPP | Cloud optical thickness / phase / water path | phase / COT / CER / CWP |",
            "",
            "| Product | detail_status | temporal_coverage | spatial_resolution | day_probe_summary |",
            "|---|---:|---|---|---|",
        ]
    )
    for product in PRODUCT_PAGES:
        meta = product_meta.get(product.code, {})
        probes = []
        for day in SEARCH_DATES:
            row = cart_results.get((product.code, day), {})
            probes.append(f"{day}:{row.get('cart_result', 'NOT_TESTED')}")
        lines.append(
            f"| {product.code} | {meta.get('detail_status', '')} | {meta.get('temporal_coverage', '')} | "
            f"{meta.get('spatial_resolution', '')} | {'; '.join(probes)} |"
        )
    lines.extend(
        [
            "",
            "Important nuance: CM SAF WUI lets us confirm product family and day-level orderability, but it does not publicly enumerate every 15-minute slot in the same way as NOAA or operational EUMETSAT catalogue listings.",
            "",
            "## 3. Time availability interpretation",
            "",
            "- Checked dates: `2024-03-01`, `2024-03-12`, `2024-03-31`",
            "- Checked full hours: `00:00`, `06:00`, `12:00`, `18:00` UTC",
            "- Result policy used in the CSV:",
            "  - `LIKELY_AVAILABLE_AS_L2_15MIN_ICDR`: the product page covers the day and day-level order acceptance succeeded.",
            "  - `NOT_DIRECTLY_ENUMERATED_PUBLICLY`: the product family exists, but the current public path does not enumerate the exact slot.",
            "",
            "## 4. Variable audit status",
            "",
            "- No real CLAAS-3 product NetCDF sample was successfully downloaded and opened locally through the current Data Store/WUI path during this audit.",
            "- Therefore `claas3_sample_variables_inventory.csv` is documentation-based, using the official CLAAS-3 PUM variable tables and CM SAF product descriptions.",
            "- That is sufficient for engineering integration planning, but it is not the same as a completed real-file xarray/ncdump validation.",
            "",
            "## 5. Integration decision",
            "",
            "- Use `CMA` for cloud mask / cloud probability / mask QA.",
            "- Use `CTX` for cloud top height / pressure / temperature.",
            "- Use `CPP` for cloud phase / optical thickness / effective radius / water path.",
            "- Pair the main L2 files with the official auxiliary geometry file for lat/lon/satzen.",
            "",
            "## 6. Required decision wording",
            "",
            "- CLAAS-3 is not the same as MSG operational OCA/CLA streams.",
            "- CLAAS-3 is a CM SAF SEVIRI cloud data record / ICDR family.",
            "- Because operational MSG OCA/OCA-IODC is not currently retrievable for `2024-03` through the tested catalogue path, CLAAS-3 should be integrated as a SEVIRI-derived cloud-property supplement.",
            "- Operational `CLM-IODC` and `CTH-IODC` remain the baseline operational products; CLAAS-3 supplements phase/COT/CER/CWP/CTP/CTT and related physics variables.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    ensure_dirs()
    ensure_proxy_env()

    datastore = probe_datastore()
    doi_status, doi_html, doi_url = request_text(CMSAF_DOI_URL)
    doi_plain = strip_tags(doi_html)

    product_meta = {product.code: fetch_product_metadata(product) for product in PRODUCT_PAGES}
    cart_results: dict[tuple[str, str], dict[str, str]] = {}
    for product in PRODUCT_PAGES:
        for day in SEARCH_DATES:
            cart_results[(product.code, day)] = cart_probe(product, day)

    availability_rows = build_availability_rows(
        datastore=datastore,
        doi_status=doi_status,
        doi_url=doi_url,
        doi_plain=doi_plain,
        product_meta=product_meta,
        cart_results=cart_results,
    )
    variable_rows = build_variable_inventory()
    mapping_md = build_mapping_md()
    report_md = build_report_md(
        datastore=datastore,
        doi_status=doi_status,
        doi_url=doi_url,
        doi_plain=doi_plain,
        product_meta=product_meta,
        cart_results=cart_results,
    )

    write_csv(OUTPUT_AVAILABILITY_CSV, availability_rows)
    write_csv(OUTPUT_VARIABLES_CSV, variable_rows)
    OUTPUT_MAPPING_MD.write_text(mapping_md, encoding="utf-8")
    OUTPUT_AVAILABILITY_MD.write_text(report_md, encoding="utf-8")

    print(OUTPUT_AVAILABILITY_MD)
    print(OUTPUT_AVAILABILITY_CSV)
    print(OUTPUT_VARIABLES_CSV)
    print(OUTPUT_MAPPING_MD)


if __name__ == "__main__":
    main()
