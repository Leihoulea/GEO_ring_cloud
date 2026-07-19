from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import netCDF4
import numpy as np
from scipy.ndimage import uniform_filter


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

from geo_ring_cloud.adapters.claas3 import discover_files, parse_filename, read_product, select_for_time  # noqa: E402
from geo_ring_cloud.adapters.epic import read_epic_cth  # noqa: E402
from geo_ring_cloud.run_discovery import discover_run_dirs, resolve_run_dir  # noqa: E402
from geo_ring_cloud.sources import SOURCE_ID_MAP, tie_order, variable_rules  # noqa: E402
from geo_ring_cloud_time_run_matrix import REQUIRED_PROFILE_ARTIFACTS, profile_artifacts_complete  # noqa: E402
from geo_ring_cloud_experiment_profile_pair import reusable_operational_baseline, write_batch_status  # noqa: E402
from run_epic_georing_single_sample import runtime_environment  # noqa: E402
from geo_ring_cloud.diagnostics.epic_pair import (  # noqa: E402
    POLICIES,
    aggregate_height_samples,
    apply_policy,
    block_bootstrap,
    epic_morphology,
    numeric_bin_masks,
    paired_classification_metrics,
)
from stage_09d_claas3_epic_profile_pair_evaluation import box_binary, comparison_domains  # noqa: E402


class PackageBoundaryTests(unittest.TestCase):
    def test_legacy_shims_export_canonical_objects(self) -> None:
        import geo_ring_cloud_lineage as legacy_lineage
        import geo_ring_cloud_run_discovery as legacy_runs
        import geo_ring_cloud_source_registry as legacy_sources
        import geo_ring_cloud_claas3_adapter as legacy_claas3
        import geo_ring_cloud_epic_pair_diagnostics as legacy_diagnostics
        import path_config as legacy_paths
        import stage1_common as legacy_pipeline

        from geo_ring_cloud import lineage, paths, run_discovery, sources
        from geo_ring_cloud import artifact_io, cloud_semantics, pipeline_support, quicklooks
        from geo_ring_cloud.adapters import claas3, cloud_products
        from geo_ring_cloud.diagnostics import epic_pair

        self.assertIs(legacy_lineage.write_manifest, lineage.write_manifest)
        self.assertIs(legacy_runs.resolve_run_dir, run_discovery.resolve_run_dir)
        self.assertIs(legacy_sources.SourceDefinition, sources.SourceDefinition)
        self.assertIs(legacy_claas3.read_product, claas3.read_product)
        self.assertIs(legacy_diagnostics.paired_height_metrics, epic_pair.paired_height_metrics)
        self.assertIs(legacy_pipeline.read_product, pipeline_support.read_product)
        self.assertIs(pipeline_support.read_product, cloud_products.read_product)
        self.assertIs(pipeline_support.make_quicklook, quicklooks.make_quicklook)
        self.assertIs(pipeline_support.safe_name, artifact_io.safe_name)
        self.assertIs(legacy_pipeline.cloud_mask_masks, cloud_semantics.cloud_mask_masks)
        self.assertIs(pipeline_support.cloud_mask_semantics, cloud_semantics.cloud_mask_semantics)
        self.assertEqual(legacy_paths.PROJECT_ROOT, paths.PROJECT_ROOT)

    def test_canonical_lineage_manifest_contract(self) -> None:
        from geo_ring_cloud.lineage import write_manifest
        from geo_ring_cloud.sources import REGISTRY_VERSION

        with test_directory("canonical_lineage") as root:
            manifest_path = root / "stage_09d_contract_manifest.json"
            write_manifest(
                manifest_path,
                canonical_stage_id="stage_09d",
                component_role="diagnostics_library",
                related_stage_ids=("stage_09d", "stage_10"),
                generating_script=CODE_DIR / "geo_ring_cloud" / "diagnostics" / "epic_pair.py",
                input_paths=(root / "input.nc",),
                output_paths=(root / "output.csv",),
                parameters={"sample_count": 2},
                project_root=CODE_DIR.parents[2],
                run_id="contract-test",
                source_profile="claas3_candidate",
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["project_id"], "geo_ring_cloud")
        self.assertEqual(payload["canonical_stage_id"], "stage_09d")
        self.assertEqual(payload["component_role"], "diagnostics_library")
        self.assertEqual(payload["related_stage_ids"], ["stage_09d", "stage_10"])
        self.assertEqual(payload["parameter_summary"], {"sample_count": 2})
        self.assertEqual(payload["source_registry_version"], REGISTRY_VERSION)


class PipelineLayoutTests(unittest.TestCase):
    def test_layout_is_derived_from_configured_stage_root(self) -> None:
        from geo_ring_cloud.pipeline_layout import (
            NATIVE_DIR,
            PIPELINE_DIRECTORIES,
            REPORT_DIR,
            STAGE_ROOT,
        )

        self.assertEqual(NATIVE_DIR, STAGE_ROOT / "standardized_native")
        self.assertEqual(REPORT_DIR, STAGE_ROOT / "reports")
        self.assertEqual(PIPELINE_DIRECTORIES[0], STAGE_ROOT)
        self.assertEqual(len(PIPELINE_DIRECTORIES), len(set(PIPELINE_DIRECTORIES)))

    def test_project_root_override_propagates_in_clean_process(self) -> None:
        with test_directory("path_override") as project_root:
            env = os.environ.copy()
            for name in list(env):
                if name.startswith("GEO_RING_"):
                    env.pop(name)
            env["GEO_RING_PROJECT_ROOT"] = str(project_root)
            code = (
                "import json; from geo_ring_cloud import paths; "
                "print(json.dumps({"
                "'project': str(paths.PROJECT_ROOT), "
                "'code': str(paths.CODE_ROOT), "
                "'stage': str(paths.STAGE_ROOT), "
                "'runs': str(paths.RUNS_ROOT)}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=CODE_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            resolved = json.loads(result.stdout)

        self.assertEqual(Path(resolved["project"]), project_root)
        self.assertEqual(
            Path(resolved["code"]),
            project_root / "third_report" / "code" / "geo_ring_cloud_stage1",
        )
        self.assertEqual(Path(resolved["stage"]), project_root / "geo_ring_cloud_stage1")
        self.assertEqual(Path(resolved["runs"]), project_root / "geo_ring_cloud_stage1_time_runs")


class CloudProductAdapterTests(unittest.TestCase):
    def test_variable_resolution_unit_conversion_and_sentinel_masking(self) -> None:
        from geo_ring_cloud.adapters.cloud_products import (
            convert_units,
            mask_sentinel_values,
            resolve_variable_names,
        )

        resolved = resolve_variable_names(
            ["science/Cloud_Optical_Thickness", "navigation/x"],
            {
                "cloud_optical_thickness": ["cloud optical thickness"],
                "projection_x": ["x"],
            },
        )
        self.assertEqual(resolved["science/Cloud_Optical_Thickness"], "cloud_optical_thickness")
        self.assertEqual(resolved["navigation/x"], "projection_x")

        height = convert_units(
            "cloud_top_height_km",
            np.asarray([1000.0, 2500.0], dtype=np.float32),
            {"units": "m"},
        )
        np.testing.assert_allclose(height, [1.0, 2.5])
        masked = mask_sentinel_values(np.asarray([1.0, -999.0, 65535.0], dtype=np.float32))
        self.assertEqual(float(masked[0]), 1.0)
        self.assertTrue(np.isnan(masked[1:]).all())


class ArtifactIoTests(unittest.TestCase):
    def test_npz_schema_and_portable_name_are_stable(self) -> None:
        from geo_ring_cloud.artifact_io import safe_name, write_json_npz

        self.assertEqual(safe_name("FY4B CPD / 2024:03"), "FY4B_CPD_2024_03")
        with test_directory("artifact_io") as root:
            path = root / "sample.npz"
            write_json_npz(
                path,
                {"cloud_mask": np.asarray([[0, 1]], dtype=np.uint8)},
                {"product": "CPD"},
                {"has_cloud_mask": True},
            )
            with np.load(path, allow_pickle=False) as payload:
                np.testing.assert_array_equal(payload["cloud_mask"], [[0, 1]])
                self.assertEqual(json.loads(str(payload["metadata_json"])), {"product": "CPD"})
                self.assertEqual(
                    json.loads(str(payload["variable_availability_json"])),
                    {"has_cloud_mask": True},
                )


class QuicklookTests(unittest.TestCase):
    def test_representative_categorical_quicklook_is_nonempty(self) -> None:
        from geo_ring_cloud.quicklooks import make_quicklook

        with test_directory("quicklook") as root:
            path = root / "cloud_mask.png"
            make_quicklook(
                np.asarray([[0, 1, 1], [0, 127, 1]], dtype=np.int16),
                path,
                "Cloud mask",
                "cloud_mask",
            )
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 1000)


class CloudSemanticsTests(unittest.TestCase):
    def test_fy4b_display_fusion_and_off_disc_masks_are_distinct(self) -> None:
        from geo_ring_cloud.cloud_semantics import cloud_mask_masks

        values = np.asarray([[0, 1, 126, 127]], dtype=np.int16)
        display, fusion, off_disc = cloud_mask_masks("FY4B", "CLM", values)
        np.testing.assert_array_equal(display, [[True, True, True, False]])
        np.testing.assert_array_equal(fusion, [[True, True, False, False]])
        np.testing.assert_array_equal(off_disc, [[False, False, True, False]])

    def test_unknown_product_falls_back_to_fill_aware_validity(self) -> None:
        from geo_ring_cloud.cloud_semantics import cloud_mask_masks

        values = np.asarray([[0.0, np.nan, 255.0]], dtype=np.float32)
        display, fusion, off_disc = cloud_mask_masks("Unknown", "MASK", values)
        np.testing.assert_array_equal(display, [[True, False, False]])
        np.testing.assert_array_equal(fusion, display)
        self.assertFalse(off_disc.any())

    def test_quality_normalization_preserves_validity_contract(self) -> None:
        from geo_ring_cloud.cloud_semantics import add_valid_and_quality

        arrays = {
            "cloud_mask": np.asarray([[0, 127]], dtype=np.int16),
            "quality_flag_raw": np.asarray([[0, 1]], dtype=np.int16),
        }
        add_valid_and_quality(arrays)
        np.testing.assert_array_equal(arrays["valid_mask"], [[1, 0]])
        np.testing.assert_array_equal(arrays["quality_flag_standard"], [[3, 1]])


class SummaryDiagnosticsTests(unittest.TestCase):
    def test_float_and_integer_summaries_are_deterministic(self) -> None:
        from geo_ring_cloud.diagnostics.summary import finite_stats

        floating = finite_stats(np.asarray([[1.0, np.nan], [3.0, 5.0]], dtype=np.float32))
        integer = finite_stats(np.asarray([1, 2, 3], dtype=np.int16))
        self.assertEqual(floating["shape"], "2x2")
        self.assertEqual(floating["nan_ratio"], 0.25)
        self.assertEqual(floating["mean"], 3.0)
        self.assertEqual(integer["min"], 1)
        self.assertEqual(integer["max"], 3)
        self.assertEqual(integer["mean"], 2.0)


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


def make_epic_cth(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("y", 2)
        ds.createDimension("x", 2)
        geolocation = ds.createGroup("geolocation_data")
        geophysical = ds.createGroup("geophysical_data")
        latitude = geolocation.createVariable("latitude", "f4", ("y", "x"))
        longitude = geolocation.createVariable("longitude", "f4", ("y", "x"))
        latitude[:] = [[10, 10], [9, 9]]
        longitude[:] = [[100, 101], [100, 101]]
        cloud_mask = geophysical.createVariable("Cloud_Mask", "i1", ("y", "x"))
        cloud_mask[:] = [[1, 2], [3, 4]]
        height = geophysical.createVariable(
            "A-band_Effective_Cloud_Height", "f4", ("y", "x"), fill_value=-999.0
        )
        height.units = "m"
        height[:] = [[1000.0, 2000.0], [-999.0, 26000.0]]


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


class EpicAdapterTests(unittest.TestCase):
    def test_cth_units_masks_and_optional_angles(self) -> None:
        with test_directory("epic_adapter") as root:
            path = root / "epic.nc"
            make_epic_cth(path)
            result = read_epic_cth(path, "geophysical_data/A-band_Effective_Cloud_Height")

        np.testing.assert_allclose(result["cth_km"][:1], [[1.0, 2.0]])
        np.testing.assert_array_equal(result["cth_valid"], [[True, True], [False, False]])
        self.assertEqual(result["cth_conversion"], "m_to_km")
        self.assertEqual(result["cth_units_standardized"], "km")
        self.assertTrue(np.isnan(result["epic_vza"]).all())
        self.assertTrue(np.isnan(result["sza"]).all())


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


class ExperimentReuseTests(unittest.TestCase):
    def test_batch_status_never_labels_failures_as_pass(self) -> None:
        with test_directory("batch_status") as root:
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            (checkpoint_root / "a.json").write_text(json.dumps({"time_tag": "a", "status": "PASS"}), encoding="utf-8")
            (checkpoint_root / "b.json").write_text(
                json.dumps({"time_tag": "b", "status": "FAIL", "error": "expected test failure"}),
                encoding="utf-8",
            )
            args = SimpleNamespace(experiment_root=root, experiment_id="test_experiment")
            status = write_batch_status(args, [{"sample_id": "a"}, {"sample_id": "b"}])
            self.assertEqual(status["overall_status"], "COMPLETE_WITH_FAILURES")
            self.assertEqual(status["failed_time_tags"], ["b"])
            self.assertIn("b", (root / "geo_ring_cloud_profile_pair_failure_summary.csv").read_text(encoding="utf-8-sig"))

    def test_runtime_environment_exposes_conda_dll_directory(self) -> None:
        env = runtime_environment()
        expected = Path(sys.executable).resolve().parent / "Library" / "bin"
        if expected.is_dir():
            self.assertEqual(Path(env["PATH"].split(os.pathsep)[0]), expected)
            completed = subprocess.run(
                [sys.executable, "-c", "import cfgrib; print(cfgrib.__version__)"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_missing_manifest_falls_back_to_fresh_baseline(self) -> None:
        with test_directory("missing_legacy_manifest") as root:
            row = {
                "stage_run_dir": str(root),
                "nearest_georing_time_utc": "2024-03-15T04:00:00Z",
                "sample_id": "20240315_0400",
            }
            self.assertIsNone(reusable_operational_baseline(row))

    def test_complete_matching_manifest_is_reusable(self) -> None:
        with test_directory("complete_legacy_manifest") as root:
            row = {
                "stage_run_dir": str(root),
                "nearest_georing_time_utc": "2024-03-15T04:00:00Z",
                "sample_id": "20240315_0400",
            }
            for relative in REQUIRED_PROFILE_ARTIFACTS:
                path = root / relative
                if path.suffix:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                else:
                    path.mkdir(parents=True, exist_ok=True)
            (root / "epic_l2_cloud_mask_semantic_sensitivity_20240315_0400").mkdir()
            (root / "single_sample_run_manifest.json").write_text(
                json.dumps({"target_time": row["nearest_georing_time_utc"], "time_tag": row["sample_id"]}),
                encoding="utf-8",
            )
            self.assertEqual(reusable_operational_baseline(row), root)


class ProfilePairDiagnosticTests(unittest.TestCase):
    def test_policy_a_b_c_contracts(self) -> None:
        epic = np.asarray([1, 2, 3, 4, 9], dtype=np.float32)
        a, a_valid = apply_policy(epic, POLICIES["A_inclusive_binary"]["epic"])
        b, b_valid = apply_policy(epic, POLICIES["B_high_confidence_only"]["epic"])
        c, c_valid = apply_policy(epic, POLICIES["C_uncertainty_aware_3class"]["epic"])
        np.testing.assert_array_equal(a, [0, 0, 1, 1, -1])
        np.testing.assert_array_equal(b, [0, -1, -1, 1, -1])
        np.testing.assert_array_equal(c, [0, 1, 1, 2, -1])
        self.assertEqual(int(a_valid.sum()), 4)
        self.assertEqual(int(b_valid.sum()), 2)
        self.assertEqual(int(c_valid.sum()), 4)

    def test_epic_morphology_is_fixed_and_boundary_aware(self) -> None:
        epic = np.ones((5, 5), dtype=np.float32)
        epic[2, 2] = 4
        result = epic_morphology(epic)
        self.assertEqual(int(result["scene"][2, 2]), 1)
        self.assertTrue(result["boundary"][2, 2])
        self.assertEqual(int(result["scene"][0, 0]), 0)

    def test_latitude_bins_include_high_latitude(self) -> None:
        values = np.asarray([10, 30, 50, 65, 75, 85], dtype=np.float32)
        bins = numeric_bin_masks(values, (("0-20", 0, 20), ("70-80", 70, 80), (">=80", 80, 91)), "lat")
        np.testing.assert_array_equal(bins["lat:70-80"], [False, False, False, False, True, False])
        np.testing.assert_array_equal(bins["lat:>=80"], [False, False, False, False, False, True])

    def test_pair_direction_is_source_b_minus_source_a(self) -> None:
        reference = np.asarray([0, 0, 1, 1], dtype=np.int8)
        source_a = np.asarray([0, 1, 0, 1], dtype=np.int8)
        source_b = reference.copy()
        metrics = paired_classification_metrics(reference, source_a, source_b, np.ones(4, dtype=bool), (0, 1), 1)
        self.assertGreater(metrics["B_minus_A_f1"], 0)
        self.assertEqual(metrics["B_only_correct_fraction"], 0.5)

    def test_block_bootstrap_is_deterministic(self) -> None:
        values = np.asarray([-0.2, 0.0, 0.1, 0.3, 0.4], dtype=np.float64)
        first = block_bootstrap(values, seed=20240309, draws=1000)
        second = block_bootstrap(values, seed=20240309, draws=1000)
        self.assertEqual(first, second)

    def test_height_windows_match_uniform_filter_contract(self) -> None:
        data = np.arange(81, dtype=np.float32).reshape(9, 9) / 5.0
        valid = np.ones((9, 9), dtype=bool)
        valid[2:4, 3:6] = False
        lat_axis = np.arange(-4, 5, dtype=np.float32)
        lon_axis = np.arange(-4, 5, dtype=np.float32)
        lon, lat = np.meshgrid(lon_axis, lat_axis)
        grid = {"resolution_degree": 1.0, "lat_centers_first_last": [-4.0, 4.0], "lon_centers_first_last": [-4.0, 4.0]}
        result = aggregate_height_samples(data, valid, lat, lon, grid)
        physical = valid & np.isfinite(data) & (data >= 0) & (data <= 25)
        for window in (3, 5, 7):
            support = uniform_filter(physical.astype(np.float32), size=window, mode="constant", cval=0.0)
            numerator = uniform_filter(np.where(physical, data, 0.0), size=window, mode="constant", cval=0.0)
            expected_valid = support > 0.25
            expected = np.full(data.shape, np.nan, dtype=np.float32)
            expected[expected_valid] = numerator[expected_valid] / support[expected_valid]
            actual, actual_valid = result[f"box_{window}x{window}"]
            np.testing.assert_array_equal(actual_valid, expected_valid)
            np.testing.assert_allclose(actual[actual_valid], expected[expected_valid], rtol=1e-6, atol=1e-6)

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
