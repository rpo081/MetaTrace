# MetaTrace — Reverse Image Search: Implementation Plan

## Goal

Webapp (Docker) that finds a query image in ~20k local PSD/JPG/PNG renderings and
prints existing metadata (original network path, embedded XMP tags). Matches cover
exact copies, resized/re-encoded variants, and semantic similarity.

```
CIFS share (network) --sync_images.py--> local SSD store --indexer--> SQLite + FAISS
                                                                           ^
React (Vite) UI --> FastAPI /api/search (upload -> embedding -> top-K) ----+
```

## Layout

```
MetaTrace/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI app factory + static mount
│   │   ├── config.py        # pydantic-settings (.env)
│   │   ├── db.py            # SQLite persistence
│   │   ├── metadata.py      # exiftool batch XMP extraction
│   │   ├── embeddings.py    # CLIP ViT-B/32 wrapper + PSD-aware decoding
│   │   ├── indexer.py       # scan/diff/embed/update FAISS
│   │   ├── search.py        # query embed -> FAISS -> metadata join
│   │   ├── scheduler.py     # periodic rescan thread
│   │   └── api/routes.py    # search/thumb/file/rescan/stats/health
│   ├── requirements.txt
│   └── tests/
├── frontend/                # Vite + React + TS
├── scripts/sync_images.py   # CIFS -> local mirror (host-side, Windows-first)
├── Dockerfile               # multi-stage: node build -> python runtime (CPU)
├── docker-compose.yml       # volumes: store(ro), data(rw)
└── README.md
```

## Components

### 1. scripts/sync_images.py (Windows-first, runs on host, outside container)
- Mirror SRC (CIFS UNC path or drive letter) → DST local SSD folder.
- Change detection by `(size, mtime)` with 2 s tolerance for CIFS timestamp rounding.
- Threaded copy pool; `--prune` mirrors deletions; `--dry-run`; `--verbose`.
- Windows long-path safe (`\\?\` / `\\?\UNC\` prefixes); also POSIX-compatible.

### 2. Backend (FastAPI, CPU CLIP)
- **db.py** — SQLite (WAL): id, rel_path, original_path (derived from NETWORK_ROOT),
  size, mtime, sha256, width/height, xmp JSON, indexed_at; kv table (model, last_scan).
- **metadata.py** — `exiftool -json -XMP:all`, batched (~64 files per call),
  per-file retry fallback; degrades to empty tags with warning if exiftool missing.
- **embeddings.py** — open_clip ViT-B/32 (`openai` weights); Pillow decode primary,
  psd-tools composite fallback for PSD; L2-normalized float32 vectors;
  device auto-detect CUDA > MPS > CPU.
- **indexer.py** — walk store → diff DB by (rel_path, size, mtime) → embed only
  new/changed → upsert rows + `add_with_ids`; removed files → `remove_ids`;
  atomic index persist; auto-rebuild if stored model differs; progress/status dict.
- **search.py** — sha256 exact hit pinned to rank 1 (score 1.0); normalized inner
  product cosine ranking over FAISS flat index; SQLite metadata join.
- **API** — `POST /api/search` (multipart image, k, min_score),
  `GET /api/thumb/{id}?size=` (JPEG disk cache), `GET /api/file/{id}` (original),
  `POST /api/rescan?rebuild=`, `GET /api/stats`, `GET /api/health`.
- **scheduler.py** — daemon-thread timer rescan every RESCAN_INTERVAL_MIN;
  busy-lock prevents overlap; manual trigger endpoint.

### 3. Frontend (Vite + React + TS)
Drag & drop upload with query preview, result grid with thumbnails + score bars +
exact-match badge, detail panel (large thumb, original path, XMP tag table),
k slider, min-score filter, stats bar + manual rescan button.

### 4. Docker
- Multi-stage: `node:20-slim` builds frontend → `python:3.11-slim` runtime with
  `libimage-exiftool-perl`; CPU-only torch wheels; CLIP weights baked at build.
- FastAPI serves built static frontend from `/`.
- Volumes: image store read-only (`/store`), writable `/data` (SQLite + FAISS + thumbs).
- Sync script stays on the host (needs CIFS access); Task Scheduler runs it;
  container picks up changes on the next scheduled rescan.

## Performance notes
- Index = 20k × 512 float32 ≈ 40 MB RAM → exact flat search sub-millisecond/query.
- Initial CPU embedding pass ≈ minutes for 20k images; rescans incremental only.
- Source resolution irrelevant to embedding cost (CLIP input is 224 px).
- Thumbnails capped at THUMB_SIZE (default 512 px), cached on disk.

## Config (.env)
| Var | Default | Purpose |
|---|---|---|
| STORE_PATH | /store | local image mirror (read-only in container) |
| DATA_PATH | /data | SQLite + FAISS + thumbs |
| NETWORK_ROOT | — | e.g. `\\nas\share\renderings`; derives original_path |
| MODEL_NAME / MODEL_PRETRAINED | ViT-B-32 / openai | CLIP variant |
| BATCH_SIZE | 64 | embed batch size |
| RESCAN_INTERVAL_MIN | 30 | periodic rescan interval |
| RUN_INITIAL_SCAN_ON_START | true | scan on startup when index empty |
| DEFAULT_TOP_K / MAX_TOP_K | 24 / 200 | search size limits |
| MIN_SCORE_DEFAULT | 0.0 | min cosine score filter |
| THUMB_SIZE | 512 | thumbnail max side |
| MAX_UPLOAD_MB | 64 | query upload limit |

## Build order
1. plan.md + scaffolding
2. sync_images.py + tests
3. db.py + metadata.py (+ tests)
4. embeddings.py + indexer.py + search.py (+ tests)
5. FastAPI routes + scheduler + main (+ API tests)
6. React frontend
7. Dockerfile + compose + .env.example + README

## Known trade-offs
- Pillow reads PSD merged composite; psd-tools fallback covers oddball saves.
  Layer-level matching is out of scope (composite is what was rendered).
- Exact-copy detection relies on byte-identical sha256; resized variants rely on
  CLIP cosine which ranks them high but is not a guarantee of pixel identity.
- exiftool is a required runtime binary for XMP (installed in Docker image);
  without it indexing still works but XMP fields stay empty.
