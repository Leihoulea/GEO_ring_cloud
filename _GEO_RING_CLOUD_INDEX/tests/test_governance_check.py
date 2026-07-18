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


if __name__ == "__main__":
    unittest.main()
