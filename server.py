#!/usr/bin/env python3
"""Nonprofit AI toolkit web service.

The browser, account API, record-scoped guide, synthesis, and concept maps are
served from one origin. PostgreSQL is required in production; local development
uses SQLite unless DATABASE_URL is set.
"""

import os

import uvicorn

from backend import (
    ESTIMATE,
    ONBOARD,
    STAGE_ORDER,
    create_app,
    redline_prompt,
    stage_prompt,
    strip_reasoning,
    synthesis_prompt,
)


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8765")),
        log_level="info",
    )
