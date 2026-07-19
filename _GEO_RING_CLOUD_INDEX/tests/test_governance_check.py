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

        self.assertTrue(any("not legacy shim: stage1_common" in item.message for item in findings))

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


if __name__ == "__main__":
    unittest.main()
