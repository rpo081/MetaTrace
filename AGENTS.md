# AGENTS.md

## What this is

MetaTrace: reverse image search over ~20k local PSD/JPG/PNG renderings. FastAPI backend (`backend/app`) + React/Vite/TS frontend (`frontend/src`), SQLite metadata + FAISS flat inner-product index, open_clip ViT-B-32 QuickGELU (OpenAI weights, CUDA via Docker GPU overlay; FAISS CPU-only). `plan.md` is original design, `README.md` documents API/env/security — trust them over prose elsewhere. `research.md`/`result-*.md` are one-off audit artifacts.

## Commands

```bash
# setup
python -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd frontend && npm install

# backend tests (from REPO ROOT only — see Testing)
.venv/bin/python -m pytest                                  # whole suite, offline, ~1 s
.venv/bin/python -m pytest backend/tests/test_api.py -k upload   # single file/-k filter

# frontend
cd frontend
./node_modules/.bin/tsc --noEmit        # typecheck (no lint script)
npm run build                           # production build
npm test                                # vitest run (23 tests)

# dev servers — vite proxies /api -> :8000
# Real DB + store (current data, indexed=6):
STORE_PATH=/Users/ralf/Desktop/MetaTrace_Store DATA_PATH=$PWD/data ../.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
# Isolated throwaway (empty DB):
# STORE_PATH=/tmp/store DATA_PATH=/tmp/data ../.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
(cd frontend && npm run dev)
```

No CI/lint/pre-commit. Full verification = `pytest + tsc + vite build` (+ `npm test` for frontend).

## Testing

- **Must run pytest from repo root**: root `conftest.py` puts repo root on `sys.path` so `backend.app.*` imports resolve. From `backend/` they break.
- **Offline**: fake-embedder fixture replaces CLIP (`backend/tests/test_indexer.py`); real weights (~600 MB) load only on first search/scan unless baked into image.
- `test_sync_images.py` exercises `scripts/sync_images.py` via `backend/tests/sync_module.py`.
- `httpx` is dev-only (TestClient) — never import in app code.
- Frontend uses `vitest` + `jsdom` + `@testing-library/react`; config in `frontend/vite.config.ts`.

## Invariants (each guards a past bug)

- **FAISS publish/swap:** never mutate published index. Scans mutate `faiss.clone_index(self.index)` and swap atomically under `_lock`; searches snapshot reference lock-free (FAISS releases GIL — concurrent read+write is UB). See `indexer.py`.
- **Path containment:** every store file via `_store_file()` in `api/routes.py` (resolve + `is_relative_to`). Never join `rel_path` directly — thumb route once allowed symlink escape.
- **Self-healing index:** startup reconciles FAISS ids vs DB row ids as *sets* (orphans removed, missing rows pruned — next scan re-embeds). Corrupt/missing index quarantined as `.faiss.corrupt*` → full rebuild. Scans publish working index periodically (clone→swap); interrupted scan costs ≤1 chunk. Don't reintroduce count-compare full-rebuild (crash loop). Deleting `index.faiss` is safe.
- **Atomic writes + streamed uploads:** thumbs via mkstemp+os.replace; uploads stream in 1 MiB chunks with early-abort 413. Reuse for new file endpoints.
- **Error hygiene:** client detail generic (full trace to server log); `/api/stats` exposes counts only, no paths/topology.
- **Keyboard accessibility:** interactive UI must use real `<button>` or tabIndex+Enter/Space, AA contrast. Result cards/dropzone were divs — WCAG failure.
- **Focus management:** modals keep `onCancel` in ref and focus first input, not Cancel — unstable `onCancel` + effect deps caused focus steal on every stats poll (10s/2s). See `AddUserModal.tsx`/`ChangePasswordModal.tsx`; parent callbacks must be `useCallback`.
- **Auth (multi-user, JWT):**
  - `METATRACE_JWT_SECRET` ≥32 chars required for login (blank→None, login 500/401). Set in `.env` at repo root (CWD); `Settings` reads `.env` via pydantic-settings. Dev admin seed is `admin`/`changeme` with `must_change_password=true` (403 gate on all non-whitelisted endpoints until changed). Whitelist: `/api/auth/change-password`, `/logout`, `/me`.
  - Password rule: **≥8 chars, ≥1 upper, ≥1 lower, ≥1 digit** — enforced in `app/models/auth.py` (`_validate_password_strength`, `min_length=8` on Register/UserCreate/ChangePassword/AdminReset) and mirrored in `AddUserModal.tsx`/`ChangePasswordModal.tsx` (`PASSWORD_HINT`). Don't reintroduce 12.
  - Email is **optional** on create/register; backend synthesizes `"{username}@metatrace.local"` when omitted (keeps `NOT NULL UNIQUE` invariant). Table still has email column; UI no longer shows it. Don't re-add email field.
  - Roles: `admin`/`editor`/`viewer`. Mutating endpoints use `Depends(require_role("admin"))` or `("admin","editor")` from `app/dependencies.py` — never roll own check. Reads (`/api/search`, `/api/images`, `/api/stats`, `/api/rescan-delta`, `/api/settings/store-snapshot`, `/api/thumb/{id}`, `/api/file/{id}`) gated `require_role("admin","editor","viewer")` (thumb/file allow `?token=` for `<img src>`). Scan: `POST /api/rescan` → `require_role("admin","editor")` + `if rebuild and role!="admin": 403` (editor rescan only, admin full rebuild). `pause`/`resume`/`store-snapshot/run`/`/api/users/*` admin-only. Frontend hides (not disables) buttons per role in `App.tsx` (`canRescan`/`canFullScan`) and `DeltaInfo` `canFullScan` — backend is source of truth.
  - Rate-limit new admin endpoints via `_check_rate_limit(request, "10/minute")` in router (mirrors `routes.py`/`users.py`); refresh cookie scoped to `path=/api/auth/refresh` — don't broaden. Argon2id hashing; access JWT in `localStorage`, refresh in httpOnly cookie.

## Toolchain quirks

- `backend/requirements.txt` **exact-pinned deliberately**. `torch` + `--extra-index-url .../whl/cpu` relies on PEP 440 local-version ordering (Linux gets `+cpu` slim wheel, not CUDA). Don't loosen; keep `psd-tools >=1.12.2` (CVE-2026-27809).
- `MODEL_NAME=ViT-B-32-quickgelu`: OpenAI CLIP requires `-quickgelu` variant; also hardcoded in Dockerfile weight-bake — change both or container tries runtime download (fails offline).
- Config in `backend/app/config.py` (pydantic-settings, `.env` from CWD). Most vars plain names (`STORE_PATH`, `MAX_UPLOAD_MB`); security-sensitive use alias choices `METATRACE_ADMIN_TOKEN`/`METATRACE_CORS_ORIGINS`/`METATRACE_JWT_SECRET` (unprefixed also accepted). CORS middleware attaches only when origins set — SPA is same-origin by default. Blank `JWT_SECRET`→`None` (min 32 chars if set).
- **macOS: run `scripts/fix_mac_omp.sh` once per venv.** faiss-cpu + torch bundle separate `libomp.dylib` → OMP Error #15 or silent SIGSEGV at CLIP/faiss — device-independent. Script relinks faiss onto torch's libomp (idempotent, re-run after reinstall). Linux/Docker unaffected (system libgomp).
- exiftool optional: absent → XMP empty, `/api/stats` flags it.
- Frontend CSP is strict `style-src 'self'` (no `unsafe-inline`); score bars use JS-set width, icons use classes. Avoid inline styles/scripts.
- `.env` at repo root is **gitignored** (`.env.example` is template). Real `STORE_PATH` is `/Users/ralf/Desktop/MetaTrace_Store`, real `DATA_PATH` is `./data` (`data/metatrace.db`, `index.faiss`, `thumbs/`). `/tmp/store` in old examples is throwaway.

