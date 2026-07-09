from __future__ import annotations

import csv
import sys

sys.path.insert(0, r"D:\AAAresearch_paper\third_report\code\geo_data_audit")
import meteosat_catalogue_discovery as m  # noqa: E402


def load(name: str) -> list[dict[str, str]]:
    with (m.OUT_DIR / name).open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    candidates = load("candidate_collections_discovered.csv")
    availability = load("candidate_collection_availability_202403.csv")
    inventory = load("candidate_sample_variable_inventory.csv")
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in inventory:
        grouped.setdefault(row.get("collection_id", ""), []).append(row)
    updated: list[dict[str, str]] = []
    for rows in grouped.values():
        flags = m.variable_flags(rows)
        for row in rows:
            for key, value in flags.items():
                row[f"contains_{key}"] = str(value)
            updated.append(row)
    m.write_csv(m.OUT_DIR / "candidate_sample_variable_inventory.csv", updated)
    m.write_report(candidates, availability, updated)
    print(m.OUT_DIR / "meteosat_catalogue_discovery_report.md")


if __name__ == "__main__":
    main()
