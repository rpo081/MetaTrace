# MetaTrace — Reverse Image Search

Find a query image inside a large library (~20k PSD/JPG/PNG renderings) and print
its metadata: original network path and embedded XMP tags. Matches cover exact
byte-identical copies, resized/re-encoded variants, and visually similar images.

```
CIFS share (network) ──scripts/sync_images.py──> local SSD store ──indexer──> SQLite + FAISS
                                                                                   ▲
React UI (drag & drop) ──> FastAPI  /api/search  (upload → CLIP embed → top-K) ────┘
```

- **Backend**: FastAPI + CLIP (`open_clip`, ViT-B-32 QuickGELU, OpenAI weights,
  `DEVICE=auto` with CUDA when available) + FAISS flat inner-product index in RAM
  + SQLite metadata.
- **Frontend**: Vite + React + TS; served as static files by the same container.
- **Sync**: `scripts/sync_images.py` mirrors the CIFS share to local SSD
  (incremental, long-path safe, Windows-first).

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

### Platforms

The image builds natively for `linux/amd64` (x64 servers, Intel Macs) and
`linux/arm64` (Apple Silicon). A plain local build always targets the host
architecture, so `docker compose up -d --build` works unchanged everywhere.

When an NVIDIA GPU is available to Docker, MetaTrace uses it for CLIP
embedding (`DEVICE=auto` selects CUDA first). FAISS remains CPU (`faiss-cpu`).

To publish one multi-arch image from a single machine:

```bash
./docker-build-push.sh v0.1.0 rpo081   # explicit tag + username
./docker-build-push.sh                 # defaults: git tag/SHA, gh CLI login
```

Both tags (`:v0.1.0` and `:latest`) are pushed as one multi-arch manifest.
The foreign platform is cross-built under QEMU — first build of the torch and
CLIP weight layers is slow, later builds hit the layer cache.

## Syncing from the network share

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
| `POST /api/search?k=24&min_score=0.1` | multipart `file` upload → ranked results |
| `GET /api/thumb/{id}?size=512` | cached PNG thumbnail (64–1024 px, alpha preserved) |
| `GET /api/file/{id}` | original file stream |
| `POST /api/rescan?rebuild=false` | trigger incremental scan (409 if busy) |
| `GET /api/stats` | index/db counts, scan state/report (`seen`, `processed`, etc.), model, exiftool status |
| `GET /api/health` | liveness |

Search response: `score` = cosine similarity (exact sha256 hits are pinned to
rank 1 with score 1.0 and `"exact": true`), plus `rel_path`, `original_path`,
`width`, `height`, full `xmp` tag dict, thumbnail/file URLs.

### Mutating endpoints & admin token

By default MetaTrace assumes a **trusted LAN**: `POST /api/rescan` is open and
a one-time warning is logged at startup. Set `METATRACE_ADMIN_TOKEN` to lock
mutating endpoints down — requests must then send
`X-Admin-Token: <METATRACE_ADMIN_TOKEN>` or receive `403 Forbidden`.
Admin UI: from the browser console run `localStorage.setItem('metatrace_admin_token', '<token>')` once and the UI's rescan button sends it as `X-Admin-Token` automatically.

## Security posture

- No CORS by default (the bundled SPA is same-origin). Set
  `METATRACE_CORS_ORIGINS` (comma-separated) only if you serve the UI from a
  different origin.
- Security headers on every response: `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`.
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
| `STORE_PATH` | `/store` | local image mirror (read-only) |
| `DATA_PATH` | `/data` | SQLite + FAISS + thumbnails |
| `NETWORK_ROOT` | — | e.g. `\\nas\share\renderings`; prefixes `original_path` |
| `MODEL_NAME` | `ViT-B-32-quickgelu` | open_clip architecture |
| `MODEL_PRETRAINED` | `openai` | weight source |
| `DEVICE` | `auto` | `auto`/`cuda`/`mps`/`cpu`; see macOS note in implementation notes |
| `BATCH_SIZE` | `128` | embedding batch size |
| `RUN_INITIAL_SCAN_ON_START` | `true` | scan at startup when index empty |
| `DEFAULT_TOP_K` / `MAX_TOP_K` | `24` / `200` | result count limits |
| `MIN_SCORE_DEFAULT` | `0.0` | default cosine cutoff |
| `THUMB_SIZE` | `512` | default thumbnail max side |
| `MAX_UPLOAD_MB` | `64` | query upload limit (streamed, early-abort) |
| `ALLOWED_EXTENSIONS` | `.psd,.jpg,.jpeg,.png,.tif,.tiff` | indexed formats |
| `METATRACE_ADMIN_TOKEN` | — | require `X-Admin-Token` on `POST /api/rescan`; empty = trusted-LAN mode |
| `METATRACE_CORS_ORIGINS` | — | comma-separated CORS origins; empty = no CORS (same-origin SPA) |

## Performance notes

- Index: 20k × 512 float32 ≈ 40 MB RAM → exact flat search, sub-millisecond queries.
- Embedding can use CUDA (`DEVICE=auto`) when NVIDIA GPU passthrough is enabled.
- Rescan time is often dominated by filesystem inventory/stat calls on large
  bind mounts; GPU helps embedding work, not full-tree metadata walks.
- For large first-time builds, `scripts/store_snapshot.py` also writes
  `data/store_snapshot_latest.json`; when the index/DB are empty, the backend
  can use that snapshot as the initial inventory to skip the first full walk.
- Query cost is independent of source resolution (CLIP consumes 224 px inputs).
- Thumbnails are generated once per `(id, size)` and served from disk cache.

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
  DB (`ntotal` vs row count). A missing or corrupt index file (quarantined as
  `index.faiss.corrupt`) or any count mismatch forces a loud full rebuild so
  search results never silently go empty.
- **Exact match** means byte-identical (sha256). Resized variants rank high via
  cosine similarity but are not guaranteed pixel-identical.
