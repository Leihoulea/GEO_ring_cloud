"""Compatibility shim for legacy imports; use ``geo_ring_cloud.lineage``."""

from geo_ring_cloud.lineage import *  # noqa: F401,F403
from geo_ring_cloud.lineage import __all__


COMPONENT_ROLE = "compatibility_shim"
