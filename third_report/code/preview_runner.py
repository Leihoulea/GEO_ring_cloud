"""Unified preview runner for all satellite 0.05 degree quicklook notebooks.

Run from the project root with the pytorch conda environment, for example:

    D:\\anaconda\\envs\\pytorch\\python.exe code\\preview_runner.py

Each preview notebook keeps its own `TARGET_HOUR_UTC` variable. This runner
executes the notebooks as-is; change the target hour inside each notebook when
you want a different local daytime scene.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_NOTEBOOKS = [
    Path("code/FY4B/fy4b_global_grid_preview_005.ipynb"),
    Path("code/GOES/goes16_global_grid_preview_005.ipynb"),
    Path("code/GOES/goes18_global_grid_preview_005.ipynb"),
    Path("code/Himawari/himawari9_global_grid_preview_005.ipynb"),
    Path("code/Meteosat/meteosat9_global_grid_preview_005.ipynb"),
    Path("code/Meteosat/meteosat10_global_grid_preview_005.ipynb"),
]


GROUPS = {
    "all": DEFAULT_NOTEBOOKS,
    "fy4b": [DEFAULT_NOTEBOOKS[0]],
    "goes": DEFAULT_NOTEBOOKS[1:3],
    "himawari": [DEFAULT_NOTEBOOKS[3]],
    "meteosat": DEFAULT_NOTEBOOKS[4:6],
}


def project_root() -> Path:
    here = Path.cwd().resolve()
    for path in [here, *here.parents]:
        if (path / "Satellite_Data_20240312").exists():
            return path
    raise FileNotFoundError("Cannot find Satellite_Data_20240312 from current directory.")


def run_notebook(path: Path) -> None:
    """Execute code cells from a notebook in a fresh namespace."""
    nb = json.loads(path.read_text(encoding="utf-8"))
    namespace = {"display": print, "__name__": "__preview_notebook__"}
    print(f"\n=== RUN {path} ===")
    for idx, cell in enumerate(nb["cells"]):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        code = compile(source, f"{path}:cell{idx}", "exec")
        exec(code, namespace)
    print(f"=== DONE {path} ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run satellite preview notebooks.")
    parser.add_argument(
        "--group",
        choices=sorted(GROUPS),
        default="all",
        help="Subset of preview notebooks to execute.",
    )
    args = parser.parse_args(argv)

    root = project_root()
    notebooks = [root / p for p in GROUPS[args.group]]
    missing = [p for p in notebooks if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing notebooks: {missing}")

    for notebook in notebooks:
        run_notebook(notebook)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
