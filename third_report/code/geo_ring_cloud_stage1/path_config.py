"""Compatibility shim for legacy imports; use ``geo_ring_cloud.paths``."""

from geo_ring_cloud.paths import *  # noqa: F401,F403
from geo_ring_cloud.paths import __all__


COMPONENT_ROLE = "compatibility_shim"
