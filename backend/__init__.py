"""Backend package for the Nonprofit AI toolkit."""

from .app import create_app
from .prompts import (
    ESTIMATE,
    ONBOARD,
    STAGE_ORDER,
    redline_prompt,
    stage_prompt,
    strip_reasoning,
    synthesis_prompt,
)

__all__ = [
    "create_app",
    "ESTIMATE",
    "ONBOARD",
    "STAGE_ORDER",
    "redline_prompt",
    "stage_prompt",
    "strip_reasoning",
    "synthesis_prompt",
]
