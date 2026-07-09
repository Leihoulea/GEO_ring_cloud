from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Return the project root that contains Satellite_Data_20240312."""
    start = Path.cwd() if start is None else start.resolve()
    for path in [start, *start.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312.")


def load_build_function(module_name: str, module_path: Path):
    """Load a per-satellite builder module and return its build_and_write entrypoint."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.build_and_write


def main() -> None:
    """Run a small representative batch or a full batch across configured satellites."""
    parser = argparse.ArgumentParser(description="Build standardized_L1_source v0.2 files.")
    parser.add_argument(
        "--mode",
        choices=["sample", "all"],
        default="sample",
        help="sample builds one representative IR-window channel per satellite; all builds every configured channel.",
    )
    parser.add_argument(
        "--goes-backend",
        choices=["direct", "satpy"],
        default="satpy",
        help="GOES standardized backend: direct official-variable decode or Satpy reader backend.",
    )
    args = parser.parse_args()

    project_root = find_project_root()
    sys.path.insert(0, str(project_root / "code"))

    build_goes = load_build_function(
        "goes_standardized_l1_source_builder",
        project_root / "code" / "GOES" / "goes_standardized_l1_source_builder.py",
    )
    build_himawari = load_build_function(
        "himawari_standardized_l1_source_builder",
        project_root / "code" / "Himawari" / "himawari_standardized_l1_source_builder.py",
    )
    build_meteosat = load_build_function(
        "meteosat_standardized_l1_source_builder",
        project_root / "code" / "Meteosat" / "meteosat_standardized_l1_source_builder.py",
    )

    goes_channels = None if args.mode == "all" else ["C13"]
    build_goes("GOES-16", "18", channels=goes_channels, project_root=project_root, backend=args.goes_backend)
    build_goes("GOES-18", "21", channels=goes_channels, project_root=project_root, backend=args.goes_backend)

    build_himawari(
        "03",
        channels=None if args.mode == "all" else ["B13"],
        project_root=project_root,
    )

    build_meteosat(
        "Meteosat-9",
        "09",
        channels=None if args.mode == "all" else ["IR_108"],
        project_root=project_root,
    )
    build_meteosat(
        "Meteosat-10",
        "12",
        channels=None if args.mode == "all" else ["IR_108"],
        project_root=project_root,
    )


if __name__ == "__main__":
    main()
