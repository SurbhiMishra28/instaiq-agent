"""Deployment entrypoint: FastAPI backend + built-frontend static hosting
in ONE process. `app_main:app` is the uvicorn target on Render/Docker.

- The backend app is imported from main.py (same routes, same docs).
- main.py's "/" route serves frontend/dist/index.html when the env var
  FRONTEND_DIST points at the build (set in render-start.sh); without it,
  "/" returns the JSON service banner as before.
- This module only adds the SPA catch-all for client-side routes.
"""
import os
from pathlib import Path

import main  # backend FastAPI app (module lives next to this file in the image)
from fastapi.responses import FileResponse, JSONResponse

_DIST = Path(os.getenv("FRONTEND_DIST", "")) if os.getenv("FRONTEND_DIST") else Path(__file__).parent / "frontend" / "dist"
_INDEX = _DIST / "index.html"

app = main.app


@app.get("/{spa_path:path}", include_in_schema=False)
async def _spa_fallback(spa_path: str):
    """Serve real files from dist; everything else falls back to index.html
    so the SPA router owns unknown paths. /api/* and /health are answered by
    the backend routes long before this catches them; a stray /api 404 that
    reaches here returns JSON, not HTML."""
    if spa_path.startswith("api/") or spa_path == "health":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = (_DIST / spa_path).resolve()
    try:
        candidate.relative_to(_DIST.resolve())
    except ValueError:
        return FileResponse(_INDEX)
    if candidate.is_file():
        return FileResponse(candidate)
    if _INDEX.is_file():
        return FileResponse(_INDEX)
    return JSONResponse({"detail": "Not found"}, status_code=404)
