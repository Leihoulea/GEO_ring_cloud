from __future__ import annotations

import json
import shutil
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

import netCDF4
import numpy as np


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))
TEST_TEMP_ROOT = Path(__file__).resolve().parent / "_tmp"
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def test_directory(name: str):
    path = TEST_TEMP_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

from geo_ring_cloud_claas3_adapter import discover_files, parse_filename, read_product, select_for_time  # noqa: E402
from geo_ring_cloud_run_discovery import discover_run_dirs, resolve_run_dir  # noqa: E402
from geo_ring_cloud_source_registry import SOURCE_ID_MAP, tie_order, variable_rules  # noqa: E402
from geo_ring_cloud_time_run_matrix import REQUIRED_PROFILE_ARTIFACTS, profile_artifacts_complete  # noqa: E402
from stage_09d_claas3_epic_profile_pair_evaluation import box_binary, comparison_domains  # noqa: E402


def add_grid(ds: netCDF4.Dataset) -> None:
    ds.createDimension("y", 2)
    ds.createDimension("x", 2)
    x = ds.createVariable("x", "f4", ("x",))
    y = ds.createVariable("y", "f4", ("y",))
    x.units = "radian"
    y.units = "radian"
    x[:] = [-0.01, 0.01]
    y[:] = [0.01, -0.01]
    projection = ds.createVariable("projection", "i2")
    projection.grid_mapping_name = "geostationary"
    projection.perspective_point_height = 35785831.0
    projection.longitude_of_projection_origin = 0.0
    projection.sweep_angle_axis = "y"
    projection.semi_major_axis = 6378169.0
    projection.semi_minor_axis = 6356583.8


def make_cma(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path, "w") as ds:
        add_grid(ds)
        cma = ds.createVariable("cma", "i1", ("y", "x"), fill_value=-1)
        cma.units = "1"
        cma[:] = [[0, 1], [1, 0]]
        probability = ds.createVariable("cma_prob", "i2", ("y", "x"), fill_value=-999)
        probability.set_auto_scale(False)
        probability.units = "percent"
        probability.scale_factor = 0.5
        probability.add_offset = 0.0
        probability[:] = [[10, 20], [30, 40]]
        quality = ds.createVariable("quality", "u2", ("y", "x"))
        quality.flag_masks = np.asarray([56], dtype=np.uint16)
        quality.flag_meanings = "quality_class"
        quality[:] = [[8, 16], [8, 0]]


def make_cpp(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path, "w") as ds:
        add_grid(ds)
        phase = ds.createVariable("cph", "i1", ("y", "x"), fill_value=-1)
        phase.units = "1"
        phase[:] = [[1, 2], [1, 2]]
        cot = ds.createVariable("cot", "f4", ("y", "x"), fill_value=-999.0)
        cot.units = "1"
        cot[:] = [[5.0, -999.0], [10.0, 2.0]]
        radius = ds.createVariable("cre", "f4", ("y", "x"), fill_value=-999.0)
        radius.units = "m"
        radius[:] = [[1e-5, 2e-5], [3e-5, 4e-5]]
        water = ds.createVariable("cwp", "f4", ("y", "x"), fill_value=-999.0)
        water.units = "kg/m2"
        water[:] = [[0.1, 0.2], [0.3, 0.4]]
        processing = ds.createVariable("processing_flag", "u2", ("y", "x"))
        processing.flag_masks = np.asarray([1, 32], dtype=np.uint16)
        processing.flag_meanings = "processed cloudy"
        processing[:] = np.full((2, 2), 33, dtype=np.uint16)


class RegistryTests(unittest.TestCase):
    def test_source_ids_are_stable_and_claas_is_appended(self) -> None:
        self.assertEqual(SOURCE_ID_MAP["GOES-16"], 1)
        self.assertEqual(SOURCE_ID_MAP["Meteosat-IODC"], 6)
        self.assertEqual(SOURCE_ID_MAP["CLAAS3-0deg"], 7)
        self.assertEqual(tie_order("operational_baseline")[-1], "Meteosat-IODC")
        self.assertEqual(tie_order("claas3_candidate")[-1], "CLAAS3-0deg")

    def test_candidate_replaces_only_zero_degree_mask_and_height(self) -> None:
        rules = variable_rules("claas3_candidate")
        self.assertIn({"source_key": "CLAAS3-0deg", "product": "CMA"}, rules["cloud_mask"])
        self.assertNotIn({"source_key": "Meteosat-0deg", "product": "CLM"}, rules["cloud_mask"])
        self.assertIn({"source_key": "Meteosat-IODC", "product": "CLM"}, rules["cloud_mask"])

    def test_operational_baseline_preserves_legacy_mask_and_height_rules(self) -> None:
        rules = variable_rules("operational_baseline")
        self.assertEqual(
            rules["cloud_mask"],
            [
                {"source_key": "FY4B", "product": "CLM"},
                {"source_key": "GOES-16", "product": "ACMF"},
                {"source_key": "GOES-18", "product": "ACMF"},
                {"source_key": "Himawari-9", "product": "CMSK"},
                {"source_key": "Meteosat-0deg", "product": "CLM"},
                {"source_key": "Meteosat-IODC", "product": "CLM"},
            ],
        )
        self.assertEqual(rules["cloud_top_height_km"][-2:], [
            {"source_key": "Meteosat-0deg", "product": "CTH"},
            {"source_key": "Meteosat-IODC", "product": "CTH"},
        ])


class AdapterTests(unittest.TestCase):
    def test_filename_parse_time_tolerance_and_scaling_once(self) -> None:
        with test_directory("scaling_once") as tmp:
            path = tmp / "nested" / "CMAin20240305150000405SVMSGI1MD.nc"
            make_cma(path)
            record = parse_filename(path)
            self.assertIsNotNone(record)
            selected, delta = select_for_time([record], "CMA", "2024-03-05T15:06:00Z")
            self.assertIsNotNone(selected)
            self.assertEqual(delta, 6.0)
            bundle = read_product(path)
            np.testing.assert_allclose(bundle.arrays["cloud_probability"], [[0.05, 0.10], [0.15, 0.20]])
            np.testing.assert_array_equal(bundle.arrays["cloud_mask"], [[0, 3], [3, 0]])
            self.assertEqual(int(bundle.arrays["physical_valid_mask_cloud_probability"].sum()), 4)
            self.assertEqual(int(bundle.arrays["fusion_valid_mask_cloud_probability"].sum()), 2)
            self.assertIn("exactly once", bundle.metadata["scale_offset_policy"])

    def test_cpp_masks_are_variable_specific(self) -> None:
        with test_directory("cpp_masks") as tmp:
            path = tmp / "CPPin20240305150000405SVMSGI1MD.nc"
            make_cpp(path)
            bundle = read_product(path)
            self.assertEqual(int(bundle.arrays["fusion_valid_mask_cloud_phase"].sum()), 4)
            self.assertEqual(int(bundle.arrays["fusion_valid_mask_cloud_optical_thickness"].sum()), 3)
            np.testing.assert_allclose(bundle.arrays["cloud_effective_radius_um"], [[10, 20], [30, 40]], rtol=1e-5)
            np.testing.assert_allclose(bundle.arrays["cloud_water_path_g_m2"], [[100, 200], [300, 400]], rtol=1e-5)

    def test_nested_duplicate_resolution_is_deterministic(self) -> None:
        with test_directory("duplicates") as root:
            make_cma(root / "a" / "CMAin20240305150000405SVMSGI1MD.nc")
            make_cma(root / "b" / "CMAin20240305150000405SVMSGI1MD.nc")
            records, duplicates = discover_files(root)
            self.assertEqual(len(records), 1)
            self.assertEqual(len(duplicates), 1)
            self.assertEqual(duplicates[0]["candidate_count"], 2)


class RunDiscoveryTests(unittest.TestCase):
    def test_matrix_manifest_precedes_legacy_directory(self) -> None:
        with test_directory("run_discovery") as root:
            profile = root / "pair_run" / "claas3_candidate"
            profile.mkdir(parents=True)
            (profile / "single_sample_run_manifest.json").write_text(json.dumps({"time_tag": "20240305_1500"}), encoding="utf-8")
            matrix = {
                "run_id": "pair_run",
                "common_inputs": {"time_tag": "20240305_1500"},
                "profile_runs": [{"source_profile": "claas3_candidate", "profile_root": str(profile)}],
            }
            (root / "pair_run" / "geo_ring_cloud_time_run_matrix_manifest.json").write_text(json.dumps(matrix), encoding="utf-8")
            resolved = resolve_run_dir(root, "20240305_1500", "claas3_candidate")
            self.assertEqual(resolved, profile)
            self.assertIn(profile, discover_run_dirs(root, "claas3_candidate"))

    def test_pruned_profile_is_not_reused_as_complete(self) -> None:
        with test_directory("profile_reuse_guard") as root:
            for relative in REQUIRED_PROFILE_ARTIFACTS:
                path = root / relative
                if relative.suffix:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                else:
                    path.mkdir(parents=True, exist_ok=True)
            (root / "epic_l2_cloud_mask_semantic_sensitivity_20240306_1300").mkdir()
            manifest = {"time_tag": "20240306_1300"}
            self.assertTrue(profile_artifacts_complete(root, manifest))
            manifest["artifact_state"] = "PRUNED_AFTER_ACCEPTANCE"
            self.assertFalse(profile_artifacts_complete(root, manifest))


class ProfilePairDiagnosticTests(unittest.TestCase):
    def test_source_domains_isolate_replacement_and_control(self) -> None:
        common = np.ones((2, 3), dtype=bool)
        base = np.asarray([[5, 1, 7], [2, 5, 3]], dtype=np.float32)
        candidate = np.asarray([[7, 1, 7], [2, 5, 3]], dtype=np.float32)
        domains = comparison_domains(common, base, common, candidate, common)
        np.testing.assert_array_equal(domains["replacement_active"], [[True, False, False], [False, False, False]])
        np.testing.assert_array_equal(domains["unchanged_control"], [[False, True, False], [True, False, True]])

    def test_box7_requires_half_valid_support(self) -> None:
        values = np.ones((9, 9), dtype=np.int16)
        all_valid = np.ones((9, 9), dtype=bool)
        aggregated, aggregated_valid = box_binary(values, all_valid)
        self.assertTrue(aggregated_valid[4, 4])
        self.assertEqual(int(aggregated[4, 4]), 1)
        sparse_valid = np.zeros((9, 9), dtype=bool)
        sparse_valid[4, 4] = True
        _, sparse_aggregated_valid = box_binary(values, sparse_valid)
        self.assertFalse(np.any(sparse_aggregated_valid))


if __name__ == "__main__":
    unittest.main()
