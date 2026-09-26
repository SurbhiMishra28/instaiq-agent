#!/bin/sh
# InstaIQ start command (Render/Docker): one process serves the API and the
# built frontend. FRONTEND_DIST tells main.py to serve the SPA at "/".
export FRONTEND_DIST=/app/frontend/dist
exec python -m uvicorn app_main:app --host 0.0.0.0 --port "${PORT:-8000}"
