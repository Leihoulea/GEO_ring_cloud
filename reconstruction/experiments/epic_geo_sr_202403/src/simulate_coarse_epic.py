"""Apply the frozen operator to produce cloud-fraction coarse EPIC inputs."""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from common import DATA, FIGURES, REPORTS, configured_inputs, ensure_layout, load_config, read_epic, resolve_scene_rows, utc_now, write_csv, write_manifest


def coarsen(cloud: np.ndarray, valid: np.ndarray, cell: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    use = valid & (cell >= 0) & np.isfinite(cloud)
    count = np.bincount(cell[use])
    total = np.bincount(cell[cell >= 0])
    sums = np.bincount(cell[use], weights=cloud[use])
    mean = np.full(len(total), np.nan, dtype=np.float32)
    okay = count > 0
    mean[okay] = sums[okay] / count[okay]
    fraction = np.zeros(len(total), dtype=np.float32)
    fraction[total > 0] = count[total > 0] / total[total > 0]
    up = np.full(cloud.shape, np.nan, dtype=np.float32)
    up[cell >= 0] = mean[cell[cell >= 0]]
    return mean, fraction, up


def plot_validation(scene: str, cloud: np.ndarray, coarse_up: np.ndarray, valid: np.ndarray, path: Path) -> None:
    residual = cloud - coarse_up
    fig, axes = plt.subplots(1, 4, figsize=(17, 4.5))
    images = [(cloud, "native EPIC cloud mask", "viridis", (0, 1)), (coarse_up, "50 km cloud fraction (upsampled)", "viridis", (0, 1)), (residual, "native − coarse upsample", "coolwarm", (-1, 1)), (valid.astype(float), "valid Earth mask", "gray", (0, 1))]
    for ax, (arr, title, cmap, bounds) in zip(axes, images):
        image = ax.imshow(arr, cmap=cmap, vmin=bounds[0], vmax=bounds[1], origin="upper")
        ax.set_title(title); ax.set_axis_off(); fig.colorbar(image, ax=ax, shrink=0.72)
    fig.suptitle(f"{scene}: coarsening validation", y=0.99)
    fig.tight_layout(); path.parent.mkdir(parents=True, exist_ok=True); fig.savefig(path, dpi=160); plt.close(fig)


def main() -> None:
    ensure_layout()
    cfg, _, _, manifest = configured_inputs(); coarse_cfg = load_config("coarsening.yaml"); rows = resolve_scene_rows()
    outputs, inventory = [], []
    chosen = [rows[i] for i in np.linspace(0, len(rows) - 1, min(int(coarse_cfg["validation_scene_count"]), len(rows)), dtype=int)]
    for row in rows:
        epic = read_epic(Path(row["epic_clm_file"]), cfg)
        with np.load(DATA / "coarse50" / f"{row['sample_id']}_operator.npz", allow_pickle=False) as z: cell = z["cell_id"]
        coarse, coarse_valid, up = coarsen(epic["cloud"], epic["valid"], cell)
        out = DATA / "coarse50" / f"{row['sample_id']}_coarse50.npz"
        np.savez_compressed(out, cloud_fraction_by_cell=coarse, valid_fraction_by_cell=coarse_valid, coarse_cloud_fraction_up=up, cell_id=cell, native_valid_mask=epic["valid"])
        outputs.append(out)
        inventory.append({"scene_id": row["sample_id"], "coarse_file": str(out), "native_valid_fraction": float(np.mean(epic["valid"])), "coarse_mean_valid_fraction": float(np.mean(coarse_valid)), "coarse_cloud_fraction_mean": float(np.nanmean(coarse))})
        if any(row["sample_id"] == item["sample_id"] for item in chosen):
            fig = FIGURES / "coarsening_validation" / f"{row['sample_id']}.png"; plot_validation(row["sample_id"], epic["cloud"], up, epic["valid"], fig); outputs.append(fig)
    table = DATA / "coarse50" / "coarsening_scene_summary.csv"; write_csv(table, inventory); outputs.append(table)
    report = REPORTS / "04_coarsening_validation.md"
    report.write_text("\n".join(["# 04 粗化验证", "", f"生成时间：`{utc_now()}`", "", f"- 已对 {len(rows)} 个冻结场景应用同一类 50 km metric-cell mean 算子。", "- 图中依次展示 native mask、上采样的 coarse cloud fraction、残差和有效 Earth mask。", "- **训练门禁：请人工核查 `figures/coarsening_validation/` 中不存在导航漂移、disk edge 异常或不合理的 cell 边界后才执行 CNN 训练。**", "- broken-cloud 与 cloud-boundary 区域预期会损失高频结构；均匀区域应保持接近。", "" ]), encoding="utf-8")
    outputs.append(report); write_manifest("04_coarsening_validation", Path(__file__), [manifest], outputs, {"coarsening": coarse_cfg, "scene_count": len(rows)})
    print(f"Simulated coarse EPIC for {len(rows)} scenes; inspect {report}")


if __name__ == "__main__":
    sys.exit(main())
