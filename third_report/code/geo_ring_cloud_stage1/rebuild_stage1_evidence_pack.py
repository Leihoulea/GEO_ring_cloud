"""Compatibility entrypoint for the canonical evidence-pack builder."""

from geo_ring_cloud.evidence_pack import *


COMPONENT_ROLE = "compatibility_entrypoint"


if __name__ == "__main__":
    raise SystemExit(main())
