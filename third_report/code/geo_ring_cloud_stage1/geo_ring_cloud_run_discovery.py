"""Compatibility shim for legacy imports; use ``geo_ring_cloud.run_discovery``."""

from geo_ring_cloud.run_discovery import *  # noqa: F401,F403
from geo_ring_cloud.run_discovery import __all__


COMPONENT_ROLE = "compatibility_shim"
