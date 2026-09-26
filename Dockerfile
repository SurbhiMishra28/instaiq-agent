# ---------------------------------------------------------------------------
# InstaIQ — single-container deploy (Render / Railway / Fly / any VPS).
# Builds the React frontend, then serves it plus the FastAPI backend from ONE
# uvicorn process on $PORT. One service, one URL, zero CORS setup.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- Backend deps -----------------------------------------------------------
COPY backend/requirements.txt ./render-requirements.txt
RUN pip install --no-cache-dir -r render-requirements.txt

# --- Backend code (modules + deployment entrypoint) --------------------------
COPY backend/*.py ./
COPY backend/chrome_fetch.cjs ./
COPY app_main.py ./

# --- Frontend: build with Node in the same image ----------------------------
COPY frontend/package.json frontend/package-lock.json frontend/vite.config.js frontend/index.html ./frontend/
COPY frontend/public ./frontend/public
COPY frontend/src ./frontend/src
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && cd frontend && npm install --no-audit --no-fund && npm run build \
    && cd .. && rm -rf frontend/node_modules \
    && apt-get purge -y nodejs npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# --- Start script ------------------------------------------------------------
COPY render-start.sh /app/render-start.sh
RUN chmod +x /app/render-start.sh

EXPOSE 8000

# Render (and most platforms) inject a dynamic PORT env var — bind to it
# when present, 8000 otherwise (local/docker default).
CMD ["/app/render-start.sh"]
