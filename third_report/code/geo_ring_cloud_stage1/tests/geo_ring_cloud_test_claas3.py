from __future__ import annotations

import ast
import collections
import importlib
import json
import os
import shutil
import sqlite3
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
from geo_ring_cloud_experiment_profile_pair import (  # noqa: E402
    STAGE_07P_PROFILE_PAIR_SCRIPT,
    reusable_operational_baseline,
    write_batch_status,
)
import rebuild_stage1_evidence_pack as evidence_pack  # noqa: E402
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


class EvidencePackBuilderTests(unittest.TestCase):
    def test_path_tokens_render_from_canonical_configuration(self) -> None:
        rendered = evidence_pack.render_path_tokens(evidence_pack.build_stage07())

        self.assertEqual(evidence_pack.COMPONENT_ROLE, "evidence_pack_builder")
        self.assertIn(str(evidence_pack.STAGE1_ROOT), rendered)
        self.assertNotIn("@STAGE_ROOT@", rendered)
        self.assertNotIn("@GEOMETRY_ROOT@", rendered)
        self.assertNotIn("@DATA_CHECK_ROOT@", rendered)

    def test_top_level_function_definitions_are_unique(self) -> None:
        source_path = CODE_DIR / "rebuild_stage1_evidence_pack.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
        counts = collections.Counter(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

        self.assertEqual({name: count for name, count in counts.items() if count > 1}, {})

    def test_script_manifest_prefers_canonical_migrated_stage_paths(self) -> None:
        rows = {row[0]: row[1:] for row in evidence_pack.script_manifest_rows()}

        self.assertEqual(
            rows["stage_06e_geometry_angle_sync/stage_06e_full_geometry_angle_source_sync.py"][:2],
            ["06e", "executed canonical"],
        )
        self.assertEqual(
            rows["06e_full_geometry_angle_source_sync_patch.py"][:2],
            ["06e", "compatibility entrypoint"],
        )
        self.assertEqual(
            rows["stage_06f_data_asset_audit/stage_06f_unknown_aware_data_asset_audit.py"][:2],
            ["06f", "executed canonical"],
        )
        self.assertEqual(
            rows["stage_06c_geometry_audit/stage_06c_multi_satellite_geometry_metadata_audit.py"][:2],
            ["06c-final", "executed canonical"],
        )
        self.assertEqual(
            rows["stage_07p_overlap_validation/stage_07p_overlap_validator.py"][:2],
            ["07p", "executed canonical"],
        )
        self.assertEqual(
            rows["stage_07p_overlap_validation/stage_07p_claas3_profile_pair_evaluation.py"][:2],
            ["07p", "present canonical gate"],
        )
        self.assertEqual(
            rows["07p_overlap_validator_hotfix.py"][:2],
            ["07p", "compatibility entrypoint"],
        )
        self.assertEqual(
            rows["stage_07p_claas3_profile_pair_evaluation.py"][:2],
            ["07p", "compatibility entrypoint"],
        )
        self.assertEqual(
            rows["07p_b_source_boundary_magnitude_review.py"][:2],
            ["07p-b", "executed"],
        )


class PackageBoundaryTests(unittest.TestCase):
    def test_legacy_shims_export_canonical_objects(self) -> None:
        import geo_ring_cloud_lineage as legacy_lineage
        import geo_ring_cloud_run_discovery as legacy_runs
        import geo_ring_cloud_source_registry as legacy_sources
        import geo_ring_cloud_claas3_adapter as legacy_claas3
        import geo_ring_cloud_epic_pair_diagnostics as legacy_diagnostics
        import path_config as legacy_paths
        import stage_09d_diagnostic_common as legacy_full_pixel_workflow
        import stage1_common as legacy_pipeline

        from geo_ring_cloud import lineage, paths, run_discovery, sources
        from geo_ring_cloud import artifact_io, cloud_semantics, pipeline_support, quicklooks
        from geo_ring_cloud.adapters import claas3, cloud_products
        from geo_ring_cloud.diagnostics import epic_pair
        from geo_ring_cloud.diagnostics import full_pixel, full_pixel_workflow

        self.assertIs(legacy_lineage.write_manifest, lineage.write_manifest)
        self.assertIs(legacy_runs.resolve_run_dir, run_discovery.resolve_run_dir)
        self.assertIs(legacy_sources.SourceDefinition, sources.SourceDefinition)
        self.assertIs(legacy_claas3.read_product, claas3.read_product)
        self.assertIs(legacy_diagnostics.paired_height_metrics, epic_pair.paired_height_metrics)
        self.assertIs(legacy_full_pixel_workflow.d09d, full_pixel)
        self.assertIs(
            legacy_full_pixel_workflow.write_run_manifest,
            full_pixel_workflow.write_run_manifest,
        )
        self.assertIs(legacy_pipeline.read_product, pipeline_support.read_product)
        self.assertIs(pipeline_support.read_product, cloud_products.read_product)
        self.assertIs(pipeline_support.make_quicklook, quicklooks.make_quicklook)
        self.assertIs(pipeline_support.safe_name, artifact_io.safe_name)
        self.assertIs(legacy_pipeline.cloud_mask_masks, cloud_semantics.cloud_mask_masks)
        self.assertIs(pipeline_support.cloud_mask_semantics, cloud_semantics.cloud_mask_semantics)
        self.assertEqual(legacy_paths.PROJECT_ROOT, paths.PROJECT_ROOT)

    def test_stage_06f_legacy_entrypoints_export_canonical_objects(self) -> None:
        mappings = {
            "06f_unknown_aware_data_asset_audit": (
                "stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit",
                "main",
            ),
            "06f_reexport_with_obitype_patch": (
                "stage_06f_data_asset_audit.stage_06f_reexport_with_obitype_patch",
                "main",
            ),
            "06f_report_sync_patch": (
                "stage_06f_data_asset_audit.stage_06f_report_sync",
                "main",
            ),
        }
        for legacy_name, (canonical_name, public_name) in mappings.items():
            legacy = importlib.import_module(legacy_name)
            canonical = importlib.import_module(canonical_name)
            self.assertIs(getattr(legacy, public_name), getattr(canonical, public_name))
            self.assertEqual(legacy.STAGE_ID, "stage_06f")

    def test_stage_06f_legacy_and_canonical_audit_help(self) -> None:
        commands = [
            [sys.executable, "06f_unknown_aware_data_asset_audit.py", "--help"],
            [
                sys.executable,
                "-m",
                "stage_06f_data_asset_audit.stage_06f_unknown_aware_data_asset_audit",
                "--help",
            ],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=CODE_DIR,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage:", completed.stdout.lower())

    def test_stage_06e_legacy_entrypoints_and_configured_roots(self) -> None:
        from geo_ring_cloud.paths import CODE_ROOT, THIRD_REPORT_ROOT

        mappings = {
            "06e_full_geometry_angle_source_sync_patch": (
                "stage_06e_geometry_angle_sync.stage_06e_full_geometry_angle_source_sync",
                "main",
            ),
            "06e_vza_ecef_final_audit": (
                "stage_06e_geometry_angle_sync.stage_06e_vza_ecef_final_audit",
                "main",
            ),
        }
        imported = {}
        for legacy_name, (canonical_name, public_name) in mappings.items():
            legacy = importlib.import_module(legacy_name)
            canonical = importlib.import_module(canonical_name)
            imported[canonical_name] = canonical
            self.assertIs(getattr(legacy, public_name), getattr(canonical, public_name))
            self.assertEqual(legacy.STAGE_ID, "stage_06e")

        sync = imported[
            "stage_06e_geometry_angle_sync.stage_06e_full_geometry_angle_source_sync"
        ]
        audit = imported[
            "stage_06e_geometry_angle_sync.stage_06e_vza_ecef_final_audit"
        ]
        self.assertEqual(sync.CODE_DIR, CODE_ROOT)
        self.assertEqual(audit.WORKSPACE_ROOT, THIRD_REPORT_ROOT)
        self.assertEqual(
            audit.LOCAL_REPORT_ROOT,
            THIRD_REPORT_ROOT / "reports" / "geo_ring_cloud_stage1_06e_vza_ecef_final_audit",
        )

    def test_stage_06c_legacy_entrypoints_export_canonical_objects(self) -> None:
        mappings = {
            "06c_geometry_parameter_audit": (
                "stage_06c_geometry_audit.stage_06c_geometry_parameter_audit",
                "main",
            ),
            "06c_multi_satellite_geometry_metadata_audit": (
                "stage_06c_geometry_audit.stage_06c_multi_satellite_geometry_metadata_audit",
                "main",
            ),
            "stage_06c_claas3_geometry_angle_lineage": (
                "stage_06c_geometry_audit.stage_06c_claas3_geometry_angle_lineage",
                "main",
            ),
        }
        for legacy_name, (canonical_name, public_name) in mappings.items():
            legacy = importlib.import_module(legacy_name)
            canonical = importlib.import_module(canonical_name)
            self.assertIs(getattr(legacy, public_name), getattr(canonical, public_name))
            self.assertEqual(legacy.STAGE_ID, "stage_06c")

    def test_stage_06c_claas3_legacy_and_canonical_help(self) -> None:
        commands = [
            [sys.executable, "stage_06c_claas3_geometry_angle_lineage.py", "--help"],
            [
                sys.executable,
                "-m",
                "stage_06c_geometry_audit.stage_06c_claas3_geometry_angle_lineage",
                "--help",
            ],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=CODE_DIR,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage:", completed.stdout.lower())

        source = (
            CODE_DIR
            / "stage_06c_geometry_audit"
            / "stage_06c_claas3_geometry_angle_lineage.py"
        ).read_text(encoding="utf-8-sig")
        self.assertNotIn("import path_config", source)
        self.assertNotIn("from geo_ring_cloud_lineage", source)
        self.assertNotIn("from geo_ring_cloud_source_registry", source)

    def test_stage_07p_legacy_entrypoints_export_canonical_objects(self) -> None:
        mappings = {
            "07p_overlap_validator_hotfix": (
                "stage_07p_overlap_validation.stage_07p_overlap_validator",
                "main",
            ),
            "stage_07p_claas3_profile_pair_evaluation": (
                "stage_07p_overlap_validation.stage_07p_claas3_profile_pair_evaluation",
                "main",
            ),
        }
        for legacy_name, (canonical_name, public_name) in mappings.items():
            legacy = importlib.import_module(legacy_name)
            canonical = importlib.import_module(canonical_name)
            self.assertIs(getattr(legacy, public_name), getattr(canonical, public_name))
            self.assertEqual(legacy.STAGE_ID, "stage_07p")

    def test_stage_07p_claas3_legacy_and_canonical_help(self) -> None:
        commands = [
            [sys.executable, "stage_07p_claas3_profile_pair_evaluation.py", "--help"],
            [
                sys.executable,
                "-m",
                "stage_07p_overlap_validation.stage_07p_claas3_profile_pair_evaluation",
                "--help",
            ],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=CODE_DIR,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage:", completed.stdout.lower())

        canonical_path = (
            CODE_DIR
            / "stage_07p_overlap_validation"
            / "stage_07p_claas3_profile_pair_evaluation.py"
        )
        source = canonical_path.read_text(encoding="utf-8-sig")
        self.assertNotIn("import path_config", source)
        self.assertNotIn("from geo_ring_cloud_lineage", source)
        self.assertEqual(STAGE_07P_PROFILE_PAIR_SCRIPT, canonical_path)
        self.assertTrue(STAGE_07P_PROFILE_PAIR_SCRIPT.exists())

    def test_migrated_stage_scripts_use_static_package_dependencies(self) -> None:
        migrated = [
            "05_reproject_cloud_to_grid.py",
            "06_fuse_best_source.py",
            "06_5_source_selection_diagnostics.py",
            "06c_geometry_parameter_audit.py",
            "06c_multi_satellite_geometry_metadata_audit.py",
            "stage_06c_claas3_geometry_angle_lineage.py",
            "stage_06c_geometry_audit/stage_06c_geometry_parameter_audit.py",
            "stage_06c_geometry_audit/stage_06c_multi_satellite_geometry_metadata_audit.py",
            "stage_06c_geometry_audit/stage_06c_claas3_geometry_angle_lineage.py",
            "07_overlap_consistency_validation.py",
            "07p_overlap_validator_hotfix.py",
            "stage_07p_claas3_profile_pair_evaluation.py",
            "stage_07p_overlap_validation/stage_07p_overlap_validator.py",
            "stage_07p_overlap_validation/stage_07p_claas3_profile_pair_evaluation.py",
            "07p_b_source_boundary_magnitude_review.py",
            "06e_full_geometry_angle_source_sync_patch.py",
            "06e_vza_ecef_final_audit.py",
            "stage_06e_geometry_angle_sync/stage_06e_full_geometry_angle_source_sync.py",
            "stage_06e_geometry_angle_sync/stage_06e_vza_ecef_final_audit.py",
            "06f_unknown_aware_data_asset_audit.py",
            "06f_reexport_with_obitype_patch.py",
        ]
        for name in migrated:
            source = (CODE_DIR / name).read_text(encoding="utf-8-sig")
            self.assertNotIn("spec_from_file_location", source, name)
            self.assertNotIn("import importlib.util", source, name)

        stage_06 = ast.parse((CODE_DIR / "06_fuse_best_source.py").read_text(encoding="utf-8-sig"))
        stage_07 = ast.parse(
            (CODE_DIR / "07_overlap_consistency_validation.py").read_text(encoding="utf-8-sig")
        )
        stage_06_functions = {
            node.name for node in stage_06.body if isinstance(node, ast.FunctionDef)
        }
        stage_07_functions = {
            node.name for node in stage_07.body if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("build_candidate", stage_06_functions)
        self.assertNotIn("compute_boundary_mask", stage_07_functions)

        stage_05 = ast.parse(
            (CODE_DIR / "05_reproject_cloud_to_grid.py").read_text(encoding="utf-8-sig")
        )
        stage_06c = ast.parse(
            (
                CODE_DIR
                / "stage_06c_geometry_audit"
                / "stage_06c_geometry_parameter_audit.py"
            ).read_text(encoding="utf-8-sig")
        )
        self.assertNotIn(
            "geolocate",
            {node.name for node in stage_05.body if isinstance(node, ast.FunctionDef)},
        )
        self.assertNotIn(
            "gather_geometry_params",
            {node.name for node in stage_06c.body if isinstance(node, ast.FunctionDef)},
        )

    def test_full_pixel_consumers_use_canonical_package_dependencies(self) -> None:
        consumers = [
            "stage_09d_geo_visible_control/stage_09d_vis_run.py",
            "stage_09d_geo_visible_control/stage_09d_vis_postprocess.py",
            "stage_09d_source_selection_sensitivity/stage_09d_sel_run.py",
            "stage_09d_source_selection_sensitivity/stage_09d_sel_postprocess.py",
            "stage_09d_vis_sel_joint/stage_09d_vis_sel_joint_report.py",
            "stage_09e_psf_sel_qc/stage_09e_run_psf_sel_qc.py",
            "stage_09f_spatial_story_maps/stage_09f_make_spatial_story_maps.py",
        ]
        for name in consumers:
            source = (CODE_DIR / name).read_text(encoding="utf-8-sig")
            self.assertNotIn("from stage_09d_diagnostic_common", source, name)
            self.assertNotIn("import path_config", source, name)

        runner = ast.parse(
            (
                CODE_DIR
                / "stage09d_full_pixel_diagnostics"
                / "run_stage09d_full_pixel_diagnostics.py"
            ).read_text(encoding="utf-8-sig")
        )
        extracted = {
            "load_npz",
            "load_grid",
            "row_col",
            "sample_grid",
            "read_epic",
            "apply_policy",
            "source_to_standard",
            "binary_metrics",
            "classify_array",
            "find_prefusion",
            "sample_context",
            "make_boundary",
        }
        runner_functions = {
            node.name for node in runner.body if isinstance(node, ast.FunctionDef)
        }
        self.assertFalse(extracted & runner_functions)

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


class FullPixelDiagnosticTests(unittest.TestCase):
    def test_sampling_policy_and_source_mapping_contract(self) -> None:
        from geo_ring_cloud.diagnostics import full_pixel

        grid = {
            "resolution_degree": 1.0,
            "lat_centers_first_last": [-0.5, 0.5],
            "lon_centers_first_last": [-0.5, 0.5],
        }
        data = np.asarray([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
        valid = np.asarray([[True, False], [True, True]])
        sampled, sampled_valid = full_pixel.sample_grid(
            data,
            valid,
            np.asarray([[-0.5, -0.5], [0.5, 0.5]], dtype=np.float32),
            np.asarray([[-0.5, 0.5], [-0.5, 0.5]], dtype=np.float32),
            grid,
        )
        np.testing.assert_allclose(
            sampled,
            [[10.0, np.nan], [30.0, 40.0]],
            equal_nan=True,
        )
        np.testing.assert_array_equal(
            sampled_valid,
            [[True, False], [True, True]],
        )

        raw = np.asarray([[0, 1, 2, 3, 9]], dtype=np.int16)
        np.testing.assert_array_equal(
            full_pixel.source_to_standard("FY4B", raw),
            [[3, 2, 1, 0, -9999]],
        )
        policy = full_pixel.POLICIES["A_inclusive_binary"]
        self.assertEqual(policy["positive"], 1)
        classes, policy_valid = full_pixel.apply_policy(
            np.asarray([[1, 2, 3, 4, 9]], dtype=np.float32),
            policy["epic"],
        )
        np.testing.assert_array_equal(classes, [[0, 0, 1, 1, -1]])
        np.testing.assert_array_equal(policy_valid, [[True, True, True, True, False]])

    def test_stage09d_runner_exports_canonical_primitives(self) -> None:
        from geo_ring_cloud.diagnostics import full_pixel

        runner = importlib.import_module(
            "stage09d_full_pixel_diagnostics.run_stage09d_full_pixel_diagnostics"
        )
        for name in (
            "sample_grid",
            "apply_policy",
            "binary_metrics",
            "source_to_standard",
            "sample_context",
            "make_boundary",
        ):
            self.assertIs(getattr(runner, name), getattr(full_pixel, name))
        self.assertIs(runner.POLICIES, full_pixel.POLICIES)
        self.assertEqual(runner.STAGE_ID, "stage_09d")

    def test_full_pixel_stage_cli_import_boundaries(self) -> None:
        scripts = [
            "stage09d_full_pixel_diagnostics/run_stage09d_full_pixel_diagnostics.py",
            "stage_09e_psf_sel_qc/stage_09e_run_psf_sel_qc.py",
            "stage_09f_spatial_story_maps/stage_09f_make_spatial_story_maps.py",
        ]
        for script in scripts:
            completed = subprocess.run(
                [sys.executable, script, "--help"],
                cwd=CODE_DIR,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage:", completed.stdout.lower())

    def test_workflow_manifest_uses_explicit_canonical_stage(self) -> None:
        from geo_ring_cloud.diagnostics.full_pixel_workflow import write_run_manifest

        with test_directory("full_pixel_manifest") as root:
            manifest_path = root / "stage_09e_contract_manifest.json"
            script_path = CODE_DIR / "stage_09e_psf_sel_qc" / "stage_09e_run_psf_sel_qc.py"
            write_run_manifest(
                manifest_path,
                canonical_stage_id="stage_09e",
                script_path=script_path,
                input_paths=[root / "input.csv"],
                output_paths=[root / "output.csv"],
                filters=["common_valid"],
                unit_conversions=[],
                row_counts={"metrics": 2},
                warnings=[],
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["project_id"], "geo_ring_cloud")
        self.assertEqual(payload["canonical_stage_id"], "stage_09e")
        self.assertEqual(payload["stage_id"], "stage_09e")
        self.assertEqual(payload["generating_script"], str(script_path))
        self.assertEqual(payload["parameter_summary"]["row_counts"], {"metrics": 2})
        self.assertIn("timestamp", payload)
        self.assertIn("code_commit", payload)


class FusionAndOverlapSupportTests(unittest.TestCase):
    def test_fusion_cloud_mapping_and_binary_contract(self) -> None:
        from geo_ring_cloud import fusion_support

        raw = np.asarray([[0, 1, 2, 3, 9]], dtype=np.int16)
        standard = fusion_support.cloud_mask_to_standard("FY4B", "CLM", raw)
        np.testing.assert_array_equal(standard, [[3, 2, 1, 0, -9999]])
        binary = fusion_support.cloud_binary_from_standard(
            standard,
            np.ones(standard.shape, dtype=bool),
        )
        np.testing.assert_array_equal(binary, [[1, 1, 0, 0, -1]])

    def test_fusion_grid_and_geostationary_geometry_contract(self) -> None:
        from geo_ring_cloud import fusion_support

        grid = {
            "lon_min": -1.0,
            "lat_min": -1.0,
            "resolution_degree": 1.0,
            "lon_size": 2,
            "lat_size": 2,
        }
        lon, lat = fusion_support.build_target_lon_lat(grid)
        np.testing.assert_allclose(lon, [-0.5, 0.5])
        np.testing.assert_allclose(lat, [-0.5, 0.5])

        vza = fusion_support.approximate_geostationary_vza(
            np.asarray([[0.0, 0.0]], dtype=np.float32),
            np.asarray([[0.0, 180.0]], dtype=np.float32),
            0.0,
        )
        self.assertAlmostEqual(float(vza[0, 0]), 0.0, places=5)
        self.assertTrue(np.isnan(vza[0, 1]))

    def test_overlap_confusion_and_boundary_contract(self) -> None:
        from geo_ring_cloud import overlap

        confusion = overlap.confusion_from_binary(
            np.asarray([0, 0, 1, 1]),
            np.asarray([0, 1, 0, 1]),
        )
        self.assertEqual(confusion, {"tn": 1, "fp": 1, "fn": 1, "tp": 1})

        source_map = np.asarray([[1, 1, 2], [1, 2, 2]], dtype=np.int16)
        valid = np.ones(source_map.shape, dtype=bool)
        boundary = overlap.compute_boundary_mask(source_map, valid)
        np.testing.assert_array_equal(
            boundary,
            [[False, True, True], [True, True, False]],
        )
        mapping = overlap.build_variable_to_product()
        self.assertIn(("FY4B", "cloud_mask"), mapping)


class ReprojectionAndGeometrySupportTests(unittest.TestCase):
    def test_target_grid_and_longitude_contract(self) -> None:
        from geo_ring_cloud import reprojection

        lon, lat, grid = reprojection.make_target_grid()
        self.assertEqual(lon.shape, (7200,))
        self.assertEqual(lat.shape, (3600,))
        self.assertEqual(grid["shape"], [3600, 7200])
        self.assertAlmostEqual(float(lon[0]), -179.975)
        self.assertAlmostEqual(float(lat[-1]), 89.975)
        normalized = reprojection.normalize_longitude(
            np.asarray([0.0, 181.0, 360.0, -180.0], dtype=np.float32)
        )
        np.testing.assert_allclose(normalized, [0.0, -179.0, 0.0, 180.0])

    def test_geometry_parameter_and_vza_contract(self) -> None:
        from geo_ring_cloud import geometry

        params = geometry.GeometryParams(
            satellite="test",
            reference_product="test",
            source_file="test.nc",
            current_subpoint_lon_deg=0.0,
            current_subpoint_source="test",
            current_earth_radius_m=geometry.DEFAULT_A_M,
            current_earth_radius_source="test",
            current_height_above_ellipsoid_m=geometry.DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M,
            current_height_source="test",
            recommended_subpoint_lon_deg=0.0,
            recommended_subpoint_source="test",
            recommended_a_m=geometry.DEFAULT_A_M,
            recommended_a_source="test",
            recommended_b_m=geometry.DEFAULT_B_M,
            recommended_b_source="test",
            recommended_center_distance_m=geometry.DEFAULT_CENTER_DISTANCE_M,
            recommended_center_distance_source="test",
            recommended_height_above_ellipsoid_m=geometry.DEFAULT_HEIGHT_ABOVE_ELLIPSOID_M,
            recommended_height_source="test",
            fallback_used=False,
            notes="",
        )
        self.assertEqual(params.satellite, "test")
        spherical = geometry.spherical_vza_chunk(
            np.asarray([0.0]),
            np.asarray([0.0]),
            0.0,
            geometry.DEFAULT_A_M,
            geometry.DEFAULT_CENTER_DISTANCE_M,
        )
        ecef = geometry.ecef_vza_chunk(
            np.asarray([0.0]),
            np.asarray([0.0]),
            0.0,
            geometry.DEFAULT_A_M,
            geometry.DEFAULT_B_M,
            geometry.DEFAULT_CENTER_DISTANCE_M,
        )
        self.assertAlmostEqual(float(spherical[0, 0]), 0.0, places=5)
        self.assertAlmostEqual(float(ecef[0, 0]), 0.0, places=5)


class DataAssetAuditTests(unittest.TestCase):
    def test_fy4b_obitype_patch_updates_semantics_and_is_idempotent(self) -> None:
        from geo_ring_cloud.data_asset_audit import apply_fy4b_obitype_patch

        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE files(file_id INTEGER PRIMARY KEY, satellite TEXT, product TEXT);
                CREATE TABLE items(
                    item_id INTEGER PRIMARY KEY,
                    file_id INTEGER,
                    normalized_name TEXT,
                    semantic_class TEXT,
                    known_status TEXT,
                    manual_review_priority TEXT,
                    notes TEXT
                );
                CREATE TABLE unknowns(item_id INTEGER);
                CREATE TABLE flags(item_id INTEGER);
                CREATE TABLE recommendations(
                    item_id INTEGER,
                    use_now INTEGER,
                    use_later INTEGER,
                    do_not_use INTEGER,
                    use_for_fusion INTEGER,
                    use_for_rating INTEGER,
                    use_for_screening INTEGER,
                    use_for_07_stratification INTEGER,
                    use_for_future_deep_space_enhancement INTEGER,
                    reason TEXT,
                    confidence REAL,
                    blocking_issue INTEGER
                );
                INSERT INTO files VALUES (1, 'FY4B', 'CLM');
                INSERT INTO items VALUES (10, 1, 'obitype', 'unknown', 'unknown', 'HIGH', '');
                INSERT INTO unknowns VALUES (10);
                INSERT INTO flags VALUES (10);
                INSERT INTO recommendations VALUES (10, 1, 0, 1, 1, 1, 1, 1, 1, '', 0.1, 1);
                """
            )
            self.assertEqual(apply_fy4b_obitype_patch(conn), 1)
            self.assertEqual(apply_fy4b_obitype_patch(conn), 1)
            item = conn.execute(
                "SELECT semantic_class, known_status, manual_review_priority, notes FROM items"
            ).fetchone()
            recommendation = conn.execute(
                "SELECT use_now, use_later, do_not_use, confidence, blocking_issue FROM recommendations"
            ).fetchone()
            unknown_count = conn.execute("SELECT count(*) FROM unknowns").fetchone()[0]
            flag_count = conn.execute("SELECT count(*) FROM flags").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(item[:3], ("lineage_metadata", "known_uninterpreted", "LOW"))
        self.assertEqual(item[3].count("OBIType = Observing Type"), 1)
        self.assertEqual(recommendation, (0, 1, 0, 0.95, 0))
        self.assertEqual(unknown_count, 0)
        self.assertEqual(flag_count, 0)


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
            external_root = project_root / "external_geo_cloud"
            credentials_file = project_root / "secrets" / "eumetsat.txt"
            env = os.environ.copy()
            for name in list(env):
                if name.startswith("GEO_RING_"):
                    env.pop(name)
            env["GEO_RING_PROJECT_ROOT"] = str(project_root)
            env["GEO_RING_EXTERNAL_GEO_CLOUD_ROOT"] = str(external_root)
            env["GEO_RING_EUMETSAT_CREDENTIALS_FILE"] = str(credentials_file)
            code = (
                "import json; from geo_ring_cloud import paths; "
                "print(json.dumps({"
                "'project': str(paths.PROJECT_ROOT), "
                "'code': str(paths.CODE_ROOT), "
                "'stage': str(paths.STAGE_ROOT), "
                "'runs': str(paths.RUNS_ROOT), "
                "'data_check': str(paths.DATA_CHECK_ROOT), "
                "'external': str(paths.EXTERNAL_GEO_CLOUD_ROOT), "
                "'credentials': str(paths.EUMETSAT_CREDENTIALS_FILE)}))"
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
        self.assertEqual(Path(resolved["data_check"]), project_root / "data_check_report")
        self.assertEqual(Path(resolved["external"]), external_root)
        self.assertEqual(Path(resolved["credentials"]), credentials_file)


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
