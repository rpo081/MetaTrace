# AGENTS.md

## What this is

MetaTrace: reverse image search over ~20k local PSD/JPG/PNG renderings. FastAPI backend (`backend/app`) + React/Vite/TS frontend (`frontend/src`), SQLite metadata + FAISS flat inner-product index, open_clip ViT-B-32 QuickGELU (OpenAI weights, CPU-only). `plan.md` is the original design doc, `README.md` documents API/env vars/security posture — both current; trust them over prose elsewhere. `research.md` and `result-*.md` are one-off audit artifacts, not docs.

## Commands

```bash
# setup
python -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd frontend && npm install

# backend tests (from REPO ROOT — see Testing)
.venv/bin/python -m pytest                                  # whole suite, offline, ~1 s
.venv/bin/python -m pytest backend/tests/test_api.py -k upload   # single file/-k filter

# frontend — package.json has NO lint/typecheck script; call tsc directly
cd frontend
./node_modules/.bin/tsc --noEmit        # typecheck
npm run build                           # production build

# dev servers (two terminals; vite proxies /api -> :8000)
(cd backend && STORE_PATH=/tmp/store DATA_PATH=/tmp/data ../.venv/bin/uvicorn app.main:app --reload)
(cd frontend && npm run dev)
```

No CI, lint, or pre-commit config exists. Full local verification = pytest + tsc + vite build.

## Testing

- Run pytest from repo root only: root `conftest.py` puts the repo root on sys.path so tests import `backend.app.*`. From `backend/` those imports break.
- Suite runs fully offline — a fake-embedder fixture pattern replaces CLIP (see `backend/tests/test_indexer.py`). Real weights (~600 MB) load only outside tests, on first search/scan, unless baked into the image.
- `test_sync_images.py` exercises `scripts/sync_images.py` via `backend/tests/sync_module.py`.
- `httpx` is dev-only (TestClient dependency) — don't import it in app code.

## Invariants (each guards a bug that actually happened)

- **FAISS publish/swap:** the published index object is never mutated after publication. Scans mutate `faiss.clone_index(self.index)` and swap atomically under `_lock`; searches snapshot the reference and query lock-free (FAISS releases the GIL — concurrent read+write on one index is UB). See `indexer.py`.
- **Path containment:** every store file served goes through `_store_file()` in `api/routes.py` (resolve + `is_relative_to`). Never join a DB `rel_path` onto the store root directly — the thumbnail route once lacked this and allowed symlink escape.
- **Self-healing index:** startup reconciles FAISS `ntotal` vs DB row count; corrupt/missing index files are quarantined as `.faiss.corrupt*` and force a full rebuild. Deleting `index.faiss` is safe by design.
- **Atomic writes + streamed uploads:** thumbnails write via mkstemp + os.replace; uploads stream in chunks with early-abort 413. Reuse these patterns for new file-touching endpoints.
- **Error hygiene:** client-facing error detail stays generic (full exceptions go to server logs); `/api/stats` exposes counts only, no filesystem paths or network topology.
- **Keyboard accessibility:** interactive UI must be reachable by keyboard (real `<button>`s or tabIndex + Enter/Space) and AA contrast. Result cards/dropzone were once divs — WCAG failure.

## Toolchain quirks

- `backend/requirements.txt` is **exact-pinned deliberately**. The `torch==X.Y.Z` pin + `--extra-index-url .../whl/cpu` combo depends on PEP 440 local-version ordering so Linux resolves the slim `+cpu` wheel instead of the multi-GB CUDA stack (macOS wheels only exist on PyPI). Don't loosen pins casually; keep `psd-tools >= 1.12.2` (CVE-2026-27809 fix floor).
- `MODEL_NAME=ViT-B-32-quickgelu`: OpenAI CLIP weights require `-quickgelu` architecture variants. The tag is also hardcoded in the Dockerfile weight-baking step — change both together or containers attempt a runtime download (fails offline).
- Config lives in `backend/app/config.py` (pydantic-settings, reads `.env` from CWD). Most vars are plain names (`STORE_PATH`, `MAX_UPLOAD_MB`, …); the security-sensitive ones use alias choices `METATRACE_ADMIN_TOKEN` / `METATRACE_CORS_ORIGINS` (unprefixed forms also accepted). CORS middleware attaches only when origins are set — SPA is same-origin by default.
- **macOS local dev: run `scripts/fix_mac_omp.sh` once per venv.** PyPI faiss-cpu + torch wheels each bundle their own `libomp.dylib`; both loaded = OMP Error #15 abort or silent SIGSEGV at CLIP instantiation or `faiss.search` — device-independent, no traceback. Script relinks faiss onto torch's libomp (idempotent, re-run after either package reinstall). Linux/Docker unaffected (system libgomp).
- exiftool is optional at runtime: absent → indexing still works, XMP fields stay empty, `/api/stats` flags it.
- Frontend CSP allows `style-src 'unsafe-inline'` because one component uses an inline style attribute; avoid adding more inline styles/scripts.
