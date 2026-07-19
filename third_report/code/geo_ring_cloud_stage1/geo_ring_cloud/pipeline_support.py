"""Compatibility facade for historical Stage 1 shared APIs.

New code must import the focused canonical modules directly.
"""

from .adapters.cloud_products import *  # noqa: F401,F403
from .adapters.cloud_products import __all__ as _adapter_all
from .artifact_io import *  # noqa: F401,F403
from .artifact_io import __all__ as _artifact_all
from .cloud_semantics import *  # noqa: F401,F403
from .cloud_semantics import __all__ as _semantics_all
from .diagnostics.summary import *  # noqa: F401,F403
from .diagnostics.summary import __all__ as _summary_all
from .lineage import utc_now
from .pipeline_layout import *  # noqa: F401,F403
from .pipeline_layout import __all__ as _layout_all
from .pipeline_layout import ensure_pipeline_directories
from .quicklooks import *  # noqa: F401,F403
from .quicklooks import __all__ as _quicklook_all


COMPONENT_ROLE = "compatibility_facade"
ensure_dirs = ensure_pipeline_directories

__all__ = list(
    dict.fromkeys(
        [
            *_layout_all,
            *_semantics_all,
            *_summary_all,
            *_adapter_all,
            *_quicklook_all,
            *_artifact_all,
            "ensure_dirs",
            "utc_now",
        ]
    )
)
