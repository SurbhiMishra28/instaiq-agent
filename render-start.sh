#!/bin/sh
# InstaIQ start command (Render/Docker): one process serves the API and the
# built frontend. Bind to $PORT when the platform injects it, 8000 otherwise.
exec python -m uvicorn app_main:app --host 0.0.0.0 --port "${PORT:-8000}"
