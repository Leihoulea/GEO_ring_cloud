"""Reusable semantic corrections for the Stage 06f data-asset audit database."""

from __future__ import annotations

import sqlite3


COMPONENT_ROLE = "data_asset_audit"


def apply_fy4b_obitype_patch(conn: sqlite3.Connection) -> int:
    """Classify FY4B OBIType as lineage metadata using documented product semantics."""
    product_list = ("CLM", "CLP", "CLT", "CTH", "CTP", "CTT")
    placeholders = ",".join("?" for _ in product_list)
    note = (
        "FY4B docs (CLM/CLP/CTH): OBIType = Observing Type metadata/diagnostic, "
        "not cloud science variable"
    )
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
    if not item_ids:
        return 0

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
    return len(item_ids)


__all__ = ["apply_fy4b_obitype_patch"]
