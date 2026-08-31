# MetaTrace — Reverse Image Search

Find a query image inside a large library (~20k PSD/JPG/PNG renderings) and print
its metadata: original network path and embedded XMP tags. Matches cover exact
byte-identical copies, resized/re-encoded variants, and visually similar images.

```
CIFS share (network) ──scripts/network_to_local_copy.py──> local SSD store ──indexer──> SQLite + FAISS
                                                                                   ▲
React UI (drag & drop) ──> FastAPI  /api/search  (upload → CLIP embed → top-K) ────┘
```

- **Backend**: FastAPI + CLIP (`open_clip`, ViT-B-32 QuickGELU, OpenAI weights,
  `DEVICE=cpu` by default; CUDA via GPU compose overlay) + FAISS flat
  inner-product index in RAM + SQLite metadata.
- **Frontend**: Vite + React + TS; served as static files by the same container.
- **Mirror / Sync**: `scripts/network_to_local_menu.py` is the preferred
  Windows UI for maintaining the local SSD mirror; it drives
  `scripts/network_to_local_copy.py` (incremental, long-path safe, robocopy-based).
  The older `scripts/sync_images.py` / `scripts/sync_menu.py` pair is still in
  the repo for the legacy mode-based workflow.

## Quickstart (Docker)

```bash
cp .env.example .env         # set LOCAL_IMAGE_STORE (and NETWORK_ROOT)
docker compose up -d --build
# open http://localhost:8000
```

The container mounts your local image mirror read-only at `/store`; database,
FAISS index and thumbnails live under the host bind mount `./data:/data`. On first start an
initial scan kicks off automatically (progress via `GET /api/stats`). CLIP
weights are baked into the image — no downloads at runtime.

Keep the share in sync with a scheduled task (Windows example below), then
trigger rescans on demand via `POST /api/rescan` (or from the web UI button).

The web UI shows the current scan state (`scanning`, `paused`, `idle`), the
latest scan report, and while a scan is active whether inventory is coming from
the snapshot file or from a live filesystem walk.

### Platforms

The image builds natively for `linux/amd64` (x64 servers, Intel Macs) and
`linux/arm64` (Apple Silicon). A plain local build always targets the host
architecture, so `docker compose up -d --build` works unchanged everywhere.

When an NVIDIA GPU is available to Docker, MetaTrace uses it for CLIP
embedding. FAISS remains CPU (`faiss-cpu`).

The default `docker compose up -d --build` runs CPU-only. For GPU support,
use the GPU compose overlay:

```bash
docker compose -f docker-compose.gpu.yml up -d --build
```

To publish one multi-arch image from a single machine:

```bash
./docker-build-push.sh v1.0.0 rpo081   # explicit tag + username
./docker-build-push.sh                 # defaults: git tag/SHA, gh CLI login
```

Both tags (`:v1.0.0` and `:latest`) are pushed as one multi-arch manifest.

## Network mirror workflow

Preferred Windows workflow:

```powershell
python scripts\network_to_local_menu.py
```

`network_to_local_menu.py` persists presets in `~/.metatrace_network_copy.json`
and lets you configure:

- source and destination folders
- excluded folder depth
- excluded source-relative paths
- excluded folder names
- allowed image extensions
- maximum file size

Before copying, the tool prints a cleanup-style summary of detected changes and
the filtered copy set. During transfer it shows a robocopy-job-based progress
display in the terminal.

For direct CLI usage, the underlying copy script is:

```powershell
python scripts\network_to_local_copy.py "D:\imagestore" --source "\\nas\share\renderings"
```

It keeps a per-share snapshot under `scripts/snapshots/`, detects
new/changed/deleted files, applies the configured filters, and copies only the
remaining image files.

## Legacy sync workflow

```powershell
python scripts\sync_images.py "\\nas\share\renderings" "D:\imagestore" --mode final --threads 8
```

The script is Windows-only and uses `robocopy` for transfer speed. Python walks
the tree and dispatches per-folder `robocopy` calls.

| Flag | Meaning |
|---|---|
| `--mode {all,final,manual}` | required mode selector |
| `all` | copy all allowed images under source |
| `final` | copy all allowed images under folders containing `final` |
| `manual` | copy all images under folders containing `manual`; otherwise only files with `manual` in the filename |
| `--skip-dir PATH` | source-relative subfolder to skip (repeatable) |
| `--dry-run` | show what would happen without changing anything |
| `--threads N` | parallel copy workers (default 8) |

Allowed file types are `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`.
Maximum copied file size is strictly `< 20 MiB`.
Exit code is `2` when any folder transfer failed.

### Interactive sync menu (Windows)

```powershell
python scripts\sync_menu.py
```

`sync_menu.py` is an interactive wrapper around `sync_images.py` with:

- Arrow-key UI for source, destination, mode, threads, and skip folders
- Dry-run and copy actions from the same main menu
- Config persistence in `~/.metatrace_sync_menu.json`
- Folder browser plus manual path entry
- Alternate terminal screen buffer for clean redraws

### Windows Task Scheduler (nightly sync)

```powershell
schtasks /Create /TN "MetaTrace sync" /SC DAILY /ST 03:00 ^
  /TR "python C:\metatrace\scripts\sync_images.py \\nas\share\renderings D:\imagestore --mode manual --threads 8 --skip-dir AT"
```

## Development setup

```bash
# backend (Python ≥3.10)
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt   # macOS/Linux
# .venv\Scripts\pip install -r backend\requirements-dev.txt   (Windows)

# frontend
cd frontend && npm install && npm run dev     # proxies /api to :8000

# backend dev server (separate terminal)
cd backend
STORE_PATH=/path/to/mirror DATA_PATH=/tmp/mt-data ../.venv/bin/uvicorn app.main:app --reload

# tests
.venv/bin/python -m pytest        # repo root
```

Note: tests run fully offline (fake embedder). The real model loads on demand —
first search or scan triggers a one-time ~600 MB weight download unless baked
into the image.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/search?k=24&min_score=0.1` | multipart `file` upload and/or `q` text query → ranked results |
| `GET /api/thumb/{id}?size=256` | cached PNG thumbnail (default 256 px; accepts 64–1024 px, alpha preserved) |
| `GET /api/file/{id}` | original file stream |
| `GET /api/images` | browse/indexed images with pagination, sorting, and filters (ext, folder, size, dimensions, dates, has_xmp) |
| `POST /api/rescan?rebuild=false&use_delta=true` | trigger scan; uses delta changes when available unless `rebuild=true` |
| `POST /api/rescan/pause` | request pause for the running scan at the next checkpoint |
| `POST /api/rescan/resume` | resume a paused scan, including persisted checkpoints after restart |
| `GET /api/stats` | index/db counts, scan state/report (`seen`, `processed`, etc.), `inventory_source`, model, exiftool status |
| `GET /api/rescan-delta` | show the latest prepared delta summary, if present |
| `GET /api/settings/store-snapshot` | return the effective root used by store-snapshot scans |
| `POST /api/settings/store-snapshot/run` | run a store snapshot scan and refresh `data/store_snapshot_latest.json` |
| `GET /api/health` | liveness |

Search response: `score` = cosine similarity (exact sha256 hits are pinned to
rank 1 with score 1.0 and `"exact": true`), plus `rel_path`, `original_path`,
`width`, `height`, full `xmp` tag dict, thumbnail/file URLs.

### Rescan behavior

- Default rescans are non-destructive incremental scans.
- If `use_delta=true` and `data/rescan_delta_latest.json` exists, only created,
  modified, and deleted files are processed.
- A full scan from the UI or `POST /api/rescan?rebuild=false&use_delta=false`
  re-inventories the whole store but does not wipe the database first.
- `rebuild=true` is the destructive mode: it resets DB and index, then rebuilds
  embeddings from scratch.
- `POST /api/rescan/pause` pauses cooperatively at the next checkpoint; resume
  continues in-process or from the persisted checkpoint after a restart.
- During active scans the UI polls stats more frequently, exposes Pause/Resume,
  and shows `inventory_source` as `snapshot` or `walk`.

### Mutating endpoints & admin token

By default MetaTrace assumes a **trusted LAN**: mutating rescan endpoints are open
and a one-time warning is logged at startup. Set `METATRACE_ADMIN_TOKEN` to lock
them down — requests must then send
`X-Admin-Token: <METATRACE_ADMIN_TOKEN>` or receive `403 Forbidden`.
Admin UI: from the browser console run `localStorage.setItem('metatrace_admin_token', '<token>')` once and the UI's rescan button sends it as `X-Admin-Token` automatically.

## Security posture

- **Authentication** is JWT (HS256, 15 min) with opaque refresh tokens in
  httpOnly+SameSite=Lax cookies scoped to `/api/auth/refresh`. Refresh tokens
  are rotated on every use and stored as SHA-256 hashes; reuse revokes the
  entire token family. Passwords are hashed with Argon2id (OWASP 2026 params).
  Role-based access control: `admin` / `editor` / `viewer`. Set
  `METATRACE_JWT_SECRET` (≥32 chars) for any internet-facing deployment —
  without it the app falls back to a trusted-LAN / single-tenant mode that
  must not be exposed.
- No CORS by default (the bundled SPA is same-origin). Set
  `METATRACE_CORS_ORIGINS` (comma-separated) only if you serve the UI from a
  different origin.
- Security headers on every response: `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: default-src 'self'; script-src 'self'; img-src 'self' blob:; style-src 'self'; frame-ancestors 'none'`,
  `Cross-Origin-Resource-Policy: same-origin`, `Referrer-Policy: no-referrer`.
- Uploads are streamed and aborted with `413` as soon as they exceed
  `MAX_UPLOAD_MB`; error responses are generic (details go to server logs).
- The container runs the server as the unprivileged `appuser`; the entrypoint
  fixes `/data` volume ownership on first start, then drops privileges. To run
  fully rootless instead, pre-create the data volume owned by your UID and set
  `user: "1000:1000"` in `docker-compose.yml`.

## Configuration

Environment variables (or `.env`, see `.env.example`):

| Var | Default | Purpose |
|---|---|---|
| `LOCAL_IMAGE_STORE` | — | host path to the local image mirror in `.env`; Docker mounts it read-only to `/store`, and local non-Docker runs also accept it as the store root |
| `STORE_PATH` | `/store` | local image mirror (read-only) |
| `DATA_PATH` | `/data` | SQLite + FAISS + thumbnails |
| `NETWORK_ROOT` | — | e.g. `\\nas\share\renderings`; prefixes `original_path` |
| `MODEL_NAME` | `ViT-B-32-quickgelu` | open_clip architecture |
| `MODEL_PRETRAINED` | `openai` | weight source |
| `DEVICE` | `auto` | `auto`/`cuda`/`mps`/`cpu`; docker-compose defaults to `cpu`, use GPU overlay for CUDA |
| `BATCH_SIZE` | `64` | embedding batch size; larger values may improve throughput but raise RAM use and pause latency — keep modest for print-resolution renders |
| `DECODE_WORKERS` | `4` | scan-time threads for image decode + sha256 (PIL/psd-tools) |
| `DECODE_PREFETCH` | `16` | max decoded images held in memory during a scan chunk (bounded window; frames are downscaled to 512 px in the worker) |
| `METATRACE_MAX_PIXELS` | `100000000` | hard pixel cap for PSD composite rendering (OOM/decompression-bomb guard) |
| `RUN_INITIAL_SCAN_ON_START` | `true` | scan at startup when index empty |
| `USE_STORE_SNAPSHOT_FOR_INITIAL_SCAN` | `true` | prefer `data/store_snapshot_latest.json` over a filesystem walk when building scan inventory |
| `METATRACE_SNAPSHOT_SCAN_ROOT` | — | optional override for `POST /api/settings/store-snapshot/run`; defaults to the effective image store |
| `DEFAULT_TOP_K` / `MAX_TOP_K` | `24` / `200` | result count limits |
| `MIN_SCORE_DEFAULT` | `0.0` | default cosine cutoff |
| `THUMB_SIZE` | `256` | default thumbnail max side for result-grid thumbnails |
| `MAX_UPLOAD_MB` | `64` | query upload limit (streamed, early-abort) |
| `ALLOWED_EXTENSIONS` | `.psd,.jpg,.jpeg,.png,.tif,.tiff` | indexed formats |
| `METATRACE_ADMIN_TOKEN` | — | legacy seed for `X-Admin-Token` / admin password seed; **if set, must be >=32 chars** (`secrets.token_urlsafe(32)`). Empty alone does **not** grant open access — requires `METATRACE_ALLOW_UNAUTH=true` (see `main.py`) |
| `METATRACE_CORS_ORIGINS` | — | comma-separated CORS origins; empty = no CORS (same-origin SPA) |
| `METATRACE_TRUSTED_PROXY` | `false` | when `true`, rate limiting trusts `X-Forwarded-For` / `X-Real-IP` (behind nginx/traefik). Leave `false` to avoid spoofing when not behind proxy |
| `METATRACE_JWT_SECRET` | — | HS256 signing secret for access tokens. **Required when running internet-facing.** Min 32 chars. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. Unset = single-server trusted-LAN mode (X-Admin-Token + open registration) |
| `THUMBS_MAX_FILES` / `METATRACE_THUMBS_MAX_FILES` | `100000` | max thumbnails before LRU eviction (`0`=unbounded); 100k ≈5 GB at 256 px |
| `SNAPSHOT_MAX_AGE_HOURS` / `METATRACE_SNAPSHOT_MAX_AGE_HOURS` | `24` | store snapshot staleness threshold (`0`=never stale); stale triggers filesystem walk |
| `MAX_BROWSE_LIMIT` | `200` | max `limit` for `GET /api/images` (capped on server) |
| `METATRACE_COOKIE_SECURE` | `true` | set the `Secure` flag on auth cookies. Set to `false` for local HTTP dev only |
| `METATRACE_COOKIE_SAMESITE` | `lax` | SameSite attribute for auth cookies; `lax` blocks cross-site POSTs (recommended) |
| `METATRACE_REFRESH_TOKEN_TTL_DAYS` | `7` | refresh-token lifetime in days |
| `METATRACE_ACCESS_TOKEN_TTL_MINUTES` | `15` | access-token lifetime in minutes |
| `METATRACE_LOCKOUT_THRESHOLD` | `5` | failed logins before account lock |
| `METATRACE_LOCKOUT_MINUTES` | `15` | lockout duration in minutes |
| `METATRACE_ALLOW_UNAUTH` | `false` | trusted-LAN escape hatch: when `true` and no JWT secret is set, mutating endpoints accept no auth. **Must be `false` for internet-facing deployments** |

## Performance notes

- Index: 20k × 512 float32 ≈ 40 MB RAM → exact flat search, sub-millisecond queries.
- Embedding can use CUDA when NVIDIA GPU passthrough is enabled (use the GPU
  compose overlay). FAISS remains CPU (`faiss-cpu`).
- Rescan time is often dominated by filesystem inventory/stat calls on large
  bind mounts; GPU helps embedding work, not full-tree metadata walks.
- `scripts/store_snapshot.py` writes the baseline snapshot to
  `data/store_snapshot.json` and refreshes `data/store_snapshot_latest.json`.
  Initial/full scans prefer the `latest` snapshot inventory when available to
  avoid unnecessary filesystem walks.
- `BATCH_SIZE` trades throughput against responsiveness: larger batches reduce
  embedding overhead but increase RAM use, pause latency, and redo work after
  an unclean stop inside a batch.
- All scan-tuning variables (`BATCH_SIZE`, `DECODE_WORKERS`, `DECODE_PREFETCH`,
  `METATRACE_MAX_PIXELS`, `DEVICE`) are passed through docker-compose: change
  them in `.env` and run `docker compose up -d` — the container is recreated,
  **no image rebuild needed**.
- Scans pipeline their stages: decode+hash run on `DECODE_WORKERS` threads with
  at most `DECODE_PREFETCH` decoded images in memory, and XMP extraction
  (exiftool subprocess) overlaps the GPU/CPU embedding step. Decoded frames are
  downscaled to `SCAN_DECODE_MAX_SIDE` (512 px) inside the worker — CLIP only
  consumes 224 px inputs, so full-resolution frames never enter the scan
  pipeline's memory window. DB rows keep the original width/height. DB writes
  are batched into one transaction per chunk, and the working index is
  published periodically (`PUBLISH_INTERVAL_SEC`). On CUDA, embedding runs in
  fp16 autocast (float32 output) with TF32 matmuls enabled.
- Query cost is independent of source resolution (CLIP consumes 224 px inputs).
- Thumbnails are generated once per `(id, size)` and served from disk cache. The UI currently uses 256 px for result cards and 512 px for the detail panel.

## Implementation notes & trade-offs

- **PSD**: Pillow reads the merged composite; files it cannot parse fall back to
  `psd-tools`. Layer-level matching is out of scope — the composite is what was
  rendered/exported.
- **QuickGELU**: OpenAI CLIP weights require the `-quickgelu` architecture
  variants; the default reflects that.
- **macOS faiss+torch conflict**: PyPI's faiss-cpu macOS wheel and torch bundle
  separate `libomp.dylib` copies — together in one process they abort or
  segfault at CLIP instantiation/search. Run `scripts/fix_mac_omp.sh` once per
  venv to relink faiss onto torch's libomp (idempotent; re-run after reinstalling
  either package). Linux/Docker are unaffected. Alternatively force
  `DEVICE=cpu`… which does NOT help — the crash is device-independent.
- **XMP**: extracted with `exiftool -json -XMP:all` (embedded tags only; sidecar
  `.xmp` files are ignored). If exiftool is missing, indexing still works but
  XMP fields stay empty and `/api/stats` flags it. Each exiftool invocation is
  capped at 120 s, bounding the worst case per 128-file batch (~4 h including
  per-file retries) instead of hanging a scan indefinitely.
- **Self-healing index**: on startup the FAISS index is reconciled against the
  DB by comparing vector-id and row-id *sets*. Orphan vectors (no DB row) are
  removed; rows without a vector are pruned so the next scan re-embeds exactly
  those files. A missing or corrupt index file (quarantined as
  `index.faiss.corrupt`) still forces a full rebuild. Long scans publish their
  working index periodically (`PUBLISH_INTERVAL_SEC`, 30 s), so an interrupted
  container loses at most ~one interval of work instead of the whole scan —
  restarts never trigger a rescan-from-zero loop.
- **Exact match** means byte-identical (sha256). Resized variants rank high via
  cosine similarity but are not guaranteed pixel-identical.
