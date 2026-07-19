"""Compatibility shim; use ``geo_ring_cloud.adapters.claas3``."""

from geo_ring_cloud.adapters.claas3 import *  # noqa: F401,F403
from geo_ring_cloud.adapters.claas3 import __all__


COMPONENT_ROLE = "compatibility_shim"
