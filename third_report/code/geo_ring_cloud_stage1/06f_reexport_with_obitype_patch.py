from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

from geo_ring_cloud.paths import CODE_ROOT

MODULE_PATH = CODE_ROOT / "06f_unknown_aware_data_asset_audit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("audit06f", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit06f"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    mod = load_module()
    conn = sqlite3.connect(mod.SQLITE_PATH)
    try:
        product_list = ("CLM", "CLP", "CLT", "CTH", "CTP", "CTT")
        placeholders = ",".join("?" for _ in product_list)
        note = "FY4B docs (CLM/CLP/CTH): OBIType = Observing Type metadata/diagnostic, not cloud science variable"
        item_ids = [
            row[0]
            for row in conn.execute(
                f"""
                SELECT i.item_id
                FROM items i
                JOIN files f ON i.file_id = f.file_id
                WHERE f.satellite = 'FY4B'
                  AND f.product IN ({placeholders})
                  AND i.normalized_name = 'obitype'
                """,
                product_list,
            ).fetchall()
        ]
        if item_ids:
            item_placeholders = ",".join("?" for _ in item_ids)
            conn.execute(
                f"""
                UPDATE items
                SET semantic_class = 'lineage_metadata',
                    known_status = 'known_uninterpreted',
                    manual_review_priority = 'LOW',
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN ?
                        WHEN instr(notes, ?) > 0 THEN notes
                        ELSE notes || '; ' || ?
                    END
                WHERE item_id IN ({item_placeholders})
                """,
                [note, note, note, *item_ids],
            )
            conn.execute(f"DELETE FROM unknowns WHERE item_id IN ({item_placeholders})", item_ids)
            conn.execute(f"DELETE FROM flags WHERE item_id IN ({item_placeholders})", item_ids)
            conn.execute(
                f"""
                UPDATE recommendations
                SET use_now = 0,
                    use_later = 1,
                    do_not_use = 0,
                    use_for_fusion = 0,
                    use_for_rating = 0,
                    use_for_screening = 0,
                    use_for_07_stratification = 0,
                    use_for_future_deep_space_enhancement = 0,
                    reason = ?,
                    confidence = 0.95,
                    blocking_issue = 0
                WHERE item_id IN ({item_placeholders})
                """,
                [note, *item_ids],
            )
            conn.commit()

        gates = mod.write_gate_rows(conn)
        mod.export_views(conn)
        parquet_paths = mod.maybe_write_parquet(conn)
        summary = mod.build_summary(conn, gates, parquet_paths)
        mod.SUMMARY_JSON.write_text(mod.safe_json(summary), encoding="utf-8")
        mod.REPORT_MD.write_text(mod.build_report(conn, summary), encoding="utf-8")

        print(f"patched_obitype_items={len(item_ids)}")
        print(f"DISCOVERY_GATE={summary['gate_status']['DISCOVERY_GATE']}")
        print(f"SEMANTIC_GATE={summary['gate_status']['SEMANTIC_GATE']}")
        print(f"FUSION_READINESS_GATE={summary['gate_status']['FUSION_READINESS_GATE']}")
        print(f"UNKNOWN_RISK_GATE={summary['gate_status']['UNKNOWN_RISK_GATE']}")
        print(f"allow_enter_07={summary['allow_enter_07']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
