"""Deployment entrypoint: FastAPI backend + built-frontend static hosting
in ONE process. `app_main:app` is the uvicorn target on Render/Docker.

- /api/*, /health, /docs hit the real backend app (imported from main.py).
- Everything else serves frontend/dist with an SPA fallback to index.html
  (client-side routing safe). When dist is missing (backend-only deploys),
  / just returns the service JSON like before.
"""
import os
from pathlib import Path

import main  # backend FastAPI app (module lives next to this file in the image)
from fastapi.responses import FileResponse, JSONResponse

_DIST = Path(os.getenv("FRONTEND_DIST", Path(__file__).parent / "frontend" / "dist"))
_INDEX = _DIST / "index.html"

app = main.app


@app.get("/", include_in_schema=False)
async def _spa_root():  # noqa: F811 — intentionally overrides main's "/" JSON
    if _INDEX.is_file():
        return FileResponse(_INDEX)
    return JSONResponse({
        "status": "ok",
        "service": "insta-intel-agent",
        "data_mode": "live (real data only)",
        "ai_engine": main.ai_engine.ai_provider_label(),
        "note": "API-only deployment (no frontend dist baked in).",
    })


@app.get("/{spa_path:path}", include_in_schema=False)
async def _spa_fallback(spa_path: str):
    """Serve real files from dist; everything else falls back to index.html
    so the SPA router owns unknown paths. /api/* paths are answered by the
    backend routes long before this catches them; a stray /api 404 that
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
