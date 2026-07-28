#!/usr/bin/env python3
"""Run Tursor-AI locally: uvicorn app.main:app"""

import uvicorn

from app.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
