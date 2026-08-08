"""Backend package for the Nonprofit AI toolkit."""

from .app import create_app
from .prompts import (
    ESTIMATE,
    INTERFACE_STATES,
    ONBOARD,
    STAGE_DEFINITIONS,
    STAGE_ORDER,
    redline_prompt,
    routing_prompt,
    stage_prompt,
    strip_reasoning,
    synthesis_prompt,
)

__all__ = [
    "create_app",
    "ESTIMATE",
    "INTERFACE_STATES",
    "ONBOARD",
    "STAGE_DEFINITIONS",
    "STAGE_ORDER",
    "redline_prompt",
    "routing_prompt",
    "stage_prompt",
    "strip_reasoning",
    "synthesis_prompt",
]
