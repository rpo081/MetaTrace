# ---------- stage 1: build the React frontend ----------
FROM node:20-slim AS ui
WORKDIR /fe
# Lockfile first for npm ci (deterministic, cache-friendly layer).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- stage 2: python runtime (CPU-only torch) ----------
FROM python:3.12-slim AS runtime

# exiftool provides embedded XMP extraction; curl serves the HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake CLIP weights into the image so the first start needs no network.
# HF_HOME moves the cache out of /root (mode 0700): the runtime user must be
# able to read the baked weights, and hf_hub's token probe must not hit EACCES.
ENV HF_HOME=/opt/hf
RUN python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32-quickgelu', pretrained='openai')" \
 && chmod -R a+rX /opt/hf

COPY backend/app ./app
COPY --from=ui /fe/dist ./static

# Unprivileged runtime user; entrypoint chowns /data on first start and drops
# privileges via setpriv before launching the server.
RUN groupadd --system appuser && useradd --system --gid appuser --home-dir /app appuser \
 && mkdir -p /data /store && chown appuser:appuser /data /store
COPY --chmod=755 backend/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV STORE_PATH=/store \
    DATA_PATH=/data \
  HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -sf http://localhost:8000/api/health || exit 1

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
