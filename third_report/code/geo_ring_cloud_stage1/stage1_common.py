"""Compatibility shim; import focused APIs from ``geo_ring_cloud`` instead."""

from geo_ring_cloud.pipeline_support import *  # noqa: F401,F403
from geo_ring_cloud.pipeline_support import __all__


COMPONENT_ROLE = "compatibility_shim"
