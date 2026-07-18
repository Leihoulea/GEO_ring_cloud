"""Compatibility shim for legacy imports; use ``geo_ring_cloud.sources``."""

from geo_ring_cloud.sources import *  # noqa: F401,F403
from geo_ring_cloud.sources import __all__


COMPONENT_ROLE = "compatibility_shim"
