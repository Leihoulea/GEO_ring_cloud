"""Freeze whole-day train/validation/test splits without pixel leakage."""
from __future__ import annotations

import sys
from pathlib import Path

from common import DATA, REPORTS, configured_inputs, ensure_layout, resolve_scene_rows, split_for_date, utc_now, write_csv, write_manifest


def main() -> None:
    ensure_layout()
    cfg, _, _, manifest = configured_inputs()
    rows = resolve_scene_rows()
    for row in rows:
        row["split"] = split_for_date(row["scene_date"], cfg)
    index = DATA / "index" / "scene_index_frozen_202403.csv"
    write_csv(index, rows)
    names = {"train": "train_scenes.csv", "validation": "val_scenes.csv", "test": "test_scenes.csv", "buffer_1": "buffer_1_scenes.csv", "buffer_2": "buffer_2_scenes.csv"}
    outputs = [index]
    counts = {}
    for split, name in names.items():
        target = DATA / "index" / name
        selected = [row for row in rows if row["split"] == split]
        write_csv(target, selected)
        counts[split] = len(selected)
        outputs.append(target)
    report = REPORTS / "02_dataset_freeze.md"
    report.write_text("\n".join([
        "# 02 数据集冻结", "", f"生成时间：`{utc_now()}`", "",
        f"- `dataset_version`: `{cfg['dataset_version']}`", "- 目标：EPIC native Cloud Mask / probability。",
        "- EPIC 产品：DSCOVR EPIC L2 CLOUD_03，历史 Stage 09D 文件名重绑定至当前配置的数据根。",
        "- GEO 产品：从原始 GEO 数据重新处理并通过 Meteosat 导航版本校验的 `fused_best_source`。",
        f"- GEO 时间容差记录：最大 {cfg['max_geo_time_difference_minutes']} min；实际每场景差值位于冻结索引。",
        "- Cloud Mask 翻译：1,2→clear(0)；3,4→cloud(1)；其余→invalid。", "- 导航：读取 EPIC 原生 latitude/longitude 与 zenith angle；GEO 使用已有 0.05° 栅格采样。", "",
        "## Whole-day split", "", *[f"- `{key}`: {value} scenes" for key, value in counts.items()],
        "", "Buffer 日期完全排除于训练、验证和测试；没有随机像元切分。", "",
    ]), encoding="utf-8")
    outputs.append(report)
    write_manifest("02_dataset_freeze", Path(__file__), [manifest], outputs, {"dataset_version": cfg["dataset_version"], "splits": cfg["splits"], "counts": counts})
    print(counts)


if __name__ == "__main__":
    sys.exit(main())
