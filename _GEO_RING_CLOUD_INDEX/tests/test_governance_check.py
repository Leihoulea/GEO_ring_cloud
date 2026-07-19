from __future__ import annotations

import shutil
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


INDEX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INDEX_ROOT))

import governance_check  # noqa: E402


TEST_TMP = Path(__file__).resolve().parent / "_tmp"


@contextmanager
def isolated_root(name: str):
    root = TEST_TMP / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        with patch.object(governance_check, "ROOT", root):
            yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CompatibilityShimTests(unittest.TestCase):
    def test_registered_legacy_name_skips_new_stage_naming_rules(self) -> None:
        relative = "third_report/code/geo_ring_cloud_stage1/stage1_common.py"
        with isolated_root("registered_legacy_shim") as root:
            write(
                root,
                relative,
                'from geo_ring_cloud.pipeline_support import *\n'
                'COMPONENT_ROLE = "compatibility_shim"\n',
            )
            naming = governance_check.check_naming([relative], {relative}, baseline_mode=False)
            contract = governance_check.check_stage_contract(
                [relative], {relative}, enforce_index_docs=True
            )

        self.assertEqual(naming, [])
        self.assertEqual(contract, [])

    def test_implementation_logic_is_rejected(self) -> None:
        with isolated_root("shim_logic") as root:
            for relative, canonical in governance_check.COMPATIBILITY_SHIM_PATHS.items():
                write(
                    root,
                    relative,
                    f'"""shim"""\nfrom {canonical} import *\nCOMPONENT_ROLE = "compatibility_shim"\n',
                )
            target = next(iter(governance_check.COMPATIBILITY_SHIM_PATHS))
            with (root / target).open("a", encoding="utf-8") as handle:
                handle.write("\ndef implementation():\n    return 1\n")

            findings = governance_check.check_compatibility_shims()

        self.assertEqual(sum("implementation logic" in item.message for item in findings), 1)
        self.assertTrue(all(item.severity == "ERROR" for item in findings))


class StageCompatibilityEntrypointTests(unittest.TestCase):
    @staticmethod
    def write_registered_entrypoints(root: Path) -> None:
        for relative, (canonical, stage_id) in governance_check.STAGE_COMPATIBILITY_ENTRYPOINTS.items():
            canonical_path = (
                f"{governance_check.CORE_CODE_PREFIX}{canonical.replace('.', '/')}.py"
            )
            write(root, canonical_path, f'STAGE_ID = "{stage_id}"\n\ndef main():\n    return 0\n')
            write(
                root,
                relative,
                f'"""compatibility entrypoint"""\n'
                f'from {canonical} import *\n'
                f'STAGE_ID = "{stage_id}"\n'
                'COMPONENT_ROLE = "compatibility_entrypoint"\n'
                'if __name__ == "__main__":\n    raise SystemExit(main())\n',
            )

    def test_registered_thin_entrypoints_are_accepted(self) -> None:
        with isolated_root("stage_entrypoint_valid") as root:
            self.write_registered_entrypoints(root)
            findings = governance_check.check_stage_compatibility_entrypoints()

        self.assertEqual(findings, [])

    def test_stage_entrypoint_implementation_logic_is_rejected(self) -> None:
        with isolated_root("stage_entrypoint_logic") as root:
            self.write_registered_entrypoints(root)
            target = next(iter(governance_check.STAGE_COMPATIBILITY_ENTRYPOINTS))
            with (root / target).open("a", encoding="utf-8") as handle:
                handle.write("\ndef implementation():\n    return 1\n")
            findings = governance_check.check_stage_compatibility_entrypoints()

        self.assertEqual(sum("implementation logic" in item.message for item in findings), 1)


class PackageFacadeTests(unittest.TestCase):
    def test_import_only_facade_is_accepted(self) -> None:
        relative = next(iter(governance_check.PACKAGE_FACADE_PATHS))
        with isolated_root("package_facade_valid") as root:
            write(
                root,
                relative,
                '"""facade"""\n'
                'from .pipeline_layout import *\n'
                'COMPONENT_ROLE = "compatibility_facade"\n'
                'ensure_dirs = ensure_pipeline_directories\n'
                '__all__ = ["ensure_dirs"]\n',
            )
            findings = governance_check.check_package_facades()

        self.assertEqual(findings, [])

    def test_facade_implementation_logic_is_rejected(self) -> None:
        relative = next(iter(governance_check.PACKAGE_FACADE_PATHS))
        with isolated_root("package_facade_logic") as root:
            write(
                root,
                relative,
                'from .pipeline_layout import *\n'
                'COMPONENT_ROLE = "compatibility_facade"\n'
                'def ensure_dirs():\n    return None\n',
            )
            findings = governance_check.check_package_facades()

        self.assertTrue(any("implementation logic" in item.message for item in findings))


class ModuleRegistryTests(unittest.TestCase):
    def test_nested_package_init_does_not_require_module_registration(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "geo_ring_cloud/adapters/__init__.py"
        )
        registry = "_GEO_RING_CLOUD_WORKSPACE/module_registry.md"
        with isolated_root("nested_package_init") as root:
            write(root, relative, 'COMPONENT_ROLE = "package_namespace"\n')
            write(root, registry, "| canonical_module |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [relative, registry],
                {relative},
                enforce_index_docs=True,
            )

        self.assertEqual(findings, [])

    def test_staged_code_cannot_add_legacy_shim_imports(self) -> None:
        script = "third_report/code/geo_ring_cloud_stage1/stage_10_example.py"
        artifact_index = "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md"
        with isolated_root("legacy_import") as root:
            write(
                root,
                script,
                'STAGE_ID = "stage_10"\nfrom stage1_common import finite_stats\n',
            )
            write(root, artifact_index, "| path |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [script, artifact_index],
                set(),
                enforce_index_docs=True,
            )

        self.assertTrue(any("compatibility boundary: stage1_common" in item.message for item in findings))

    def test_staged_code_cannot_import_transitional_package_facade(self) -> None:
        script = "third_report/code/geo_ring_cloud_stage1/stage_10_example.py"
        artifact_index = "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md"
        with isolated_root("package_facade_import") as root:
            write(
                root,
                script,
                'STAGE_ID = "stage_10"\n'
                'from geo_ring_cloud.pipeline_support import REPORT_DIR\n',
            )
            write(root, artifact_index, "| path |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [script, artifact_index], set(), enforce_index_docs=True
            )

        self.assertTrue(
            any("compatibility boundary: geo_ring_cloud.pipeline_support" in item.message for item in findings)
        )

    def test_dedicated_compatibility_test_may_import_registered_shims(self) -> None:
        script = (
            "third_report/code/geo_ring_cloud_stage1/"
            "tests/geo_ring_cloud_test_claas3.py"
        )
        with isolated_root("compatibility_test_allowlist") as root:
            write(root, script, "import stage1_common\n")
            findings = governance_check.check_stage_contract(
                [script], set(), enforce_index_docs=True
            )

        self.assertFalse(any("legacy shim" in item.message for item in findings))

    def test_modified_stage_script_requires_artifact_index_only(self) -> None:
        script = "third_report/code/geo_ring_cloud_stage1/stage_10_example.py"
        artifact_index = "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md"
        with isolated_root("modified_stage_index") as root:
            write(root, script, 'STAGE_ID = "stage_10"\n')
            write(root, artifact_index, "| path |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [script, artifact_index],
                set(),
                enforce_index_docs=True,
            )

        self.assertEqual(findings, [])

    def test_modified_stage_script_accepts_refreshed_engineering_status(self) -> None:
        script = "third_report/code/geo_ring_cloud_stage1/stage_10_example.py"
        engineering_status = "_GEO_RING_CLOUD_WORKSPACE/engineering_status.md"
        with isolated_root("modified_stage_status") as root:
            write(root, script, 'STAGE_ID = "stage_10"\n')
            write(root, engineering_status, "Generated: now\n")
            findings = governance_check.check_stage_contract(
                [script, engineering_status],
                set(),
                enforce_index_docs=True,
            )

        self.assertEqual(findings, [])

    def test_registered_stage_migration_is_not_treated_as_a_new_stage(self) -> None:
        canonical_module, expected_stage_id = next(
            iter(governance_check.STAGE_COMPATIBILITY_ENTRYPOINTS.values())
        )
        script = (
            f"{governance_check.CORE_CODE_PREFIX}"
            f"{canonical_module.replace('.', '/')}.py"
        )
        artifact_index = "_GEO_RING_CLOUD_WORKSPACE/artifact_index.md"
        with isolated_root("registered_stage_migration") as root:
            write(root, script, f'STAGE_ID = "{expected_stage_id}"\n')
            write(root, artifact_index, "| path |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [script, artifact_index],
                {script},
                enforce_index_docs=True,
            )

        self.assertEqual(findings, [])

    def test_unregistered_package_module_is_rejected(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "geo_ring_cloud/new_shared_api.py"
        )
        registry = "_GEO_RING_CLOUD_WORKSPACE/module_registry.md"
        with isolated_root("missing_registry") as root:
            write(root, relative, 'COMPONENT_ROLE = "shared_library"\n')
            write(root, registry, "| canonical_module |\n| --- |\n")
            findings = governance_check.check_stage_contract(
                [relative, registry],
                {relative},
                enforce_index_docs=True,
            )

        self.assertTrue(any("geo_ring_cloud.new_shared_api" in item.message for item in findings))

    def test_registered_package_module_is_accepted(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "geo_ring_cloud/new_shared_api.py"
        )
        registry = "_GEO_RING_CLOUD_WORKSPACE/module_registry.md"
        with isolated_root("registered_module") as root:
            write(root, relative, 'COMPONENT_ROLE = "shared_library"\n')
            write(root, registry, "| canonical_module |\n| --- |\n| geo_ring_cloud.new_shared_api |\n")
            findings = governance_check.check_stage_contract(
                [relative, registry],
                {relative},
                enforce_index_docs=True,
            )

        self.assertEqual(findings, [])


class PackageDependencyBoundaryTests(unittest.TestCase):
    def test_direct_legacy_stage_imports_are_rejected(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "geo_ring_cloud/diagnostics/bad.py"
        )
        sources = (
            "import stage09d_full_pixel_diagnostics\n",
            "import run_stage09d_full_pixel_diagnostics\n",
            "from stage_09d_pipeline import helper\n",
            "from stage09_pipeline import helper\n",
        )
        for index, source in enumerate(sources):
            with self.subTest(source=source), isolated_root(f"direct_stage_import_{index}") as root:
                write(root, relative, source)
                findings = governance_check.check_package_dependency_boundaries([relative])

            self.assertTrue(
                any("must not depend on stage module" in item.message for item in findings)
            )

    def test_dynamic_stage_loading_is_rejected(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "geo_ring_cloud/diagnostics/bad.py"
        )
        with isolated_root("package_stage_dependency") as root:
            write(
                root,
                relative,
                "import importlib.util\n"
                "importlib.util.spec_from_file_location('stage', 'stage_10_run.py')\n",
            )
            findings = governance_check.check_package_dependency_boundaries([relative])

        self.assertTrue(any("dynamically load stage scripts" in item.message for item in findings))

    def test_new_stage_to_stage_dynamic_loading_is_rejected(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "stage_10_bad_loader.py"
        )
        with isolated_root("new_dynamic_stage_loader") as root:
            write(
                root,
                relative,
                "import importlib.util\n"
                "importlib.util.spec_from_file_location('stage', 'stage_09_run.py')\n",
            )
            findings = governance_check.check_dynamic_stage_loading(
                [relative],
                baseline_mode=False,
            )

        self.assertTrue(any(item.severity == "ERROR" for item in findings))
        self.assertTrue(any("must not dynamically load" in item.message for item in findings))

    def test_migrated_legacy_path_cannot_reintroduce_dynamic_loading(self) -> None:
        relative = (
            "third_report/code/geo_ring_cloud_stage1/"
            "06e_vza_ecef_final_audit.py"
        )
        with isolated_root("migrated_dynamic_stage_loader") as root:
            write(
                root,
                relative,
                "import importlib.util\n"
                "importlib.util.spec_from_file_location('stage', '06c_geometry_parameter_audit.py')\n",
            )
            findings = governance_check.check_dynamic_stage_loading(
                [relative],
                baseline_mode=False,
            )

        self.assertEqual([item.severity for item in findings], ["ERROR"])
        self.assertTrue(any("must not dynamically load" in item.message for item in findings))


class PathEnforcementTests(unittest.TestCase):
    def test_machine_local_path_is_rejected_in_active_tooling(self) -> None:
        relative = "third_report/code/geo_data_audit/new_probe.py"
        with isolated_root("absolute_local_path") as root:
            write(root, relative, 'ROOT = r"E:\\local_data"\n')
            findings = governance_check.check_paths([relative], {relative}, baseline_mode=False)

        self.assertTrue(any(item.severity == "ERROR" for item in findings))
        self.assertTrue(any("machine-local absolute path" in item.message for item in findings))

    def test_environment_based_path_is_accepted(self) -> None:
        relative = "third_report/code/geo_data_audit/new_probe.py"
        with isolated_root("environment_path") as root:
            write(root, relative, 'from geo_ring_cloud.paths import DATA_ROOT\nROOT = DATA_ROOT / "FY4B"\n')
            findings = governance_check.check_paths([relative], {relative}, baseline_mode=False)

        self.assertEqual(findings, [])

    def test_canonical_powershell_path_configuration_is_allowlisted(self) -> None:
        relative = "third_report/code/geo_ring_cloud_stage1/geo_ring_cloud_path_configuration.ps1"
        with isolated_root("powershell_path_allowlist") as root:
            write(root, relative, '$GeoRingExternalGeoCloudRoot = "E:\\GEO_Cloud_2024"\n')
            findings = governance_check.check_paths([relative], {relative}, baseline_mode=False)

        self.assertEqual(findings, [])


class PythonStructureTests(unittest.TestCase):
    def test_duplicate_top_level_functions_are_rejected(self) -> None:
        relative = "third_report/code/geo_ring_cloud_stage1/duplicate.py"
        with isolated_root("duplicate_top_level") as root:
            write(root, relative, "def build():\n    return 1\n\ndef build():\n    return 2\n")
            findings = governance_check.check_python_structure([relative])

        self.assertEqual(len(findings), 1)
        self.assertIn("build", findings[0].message)

    def test_nested_methods_with_same_name_are_accepted(self) -> None:
        relative = "third_report/code/geo_ring_cloud_stage1/classes.py"
        with isolated_root("nested_method_names") as root:
            write(
                root,
                relative,
                "class First:\n    def run(self):\n        return 1\n\n"
                "class Second:\n    def run(self):\n        return 2\n",
            )
            findings = governance_check.check_python_structure([relative])

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
