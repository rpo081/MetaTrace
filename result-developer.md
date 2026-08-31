# Result: developer

**Task:** Split frontend god component `App.tsx` (930 LOC) into focused feature modules + PageShell while preserving behaviour and a11y.
**Status:** ✅ Complete
**Step:** 1 of 1 (single-step refactor)

---

## Summary

Refactored the monolithic `App.tsx` into four focused modules:

- `frontend/src/components/PageShell.tsx` — topbar (brand, scan pill, retry, nav, logout), `ScanReportLine`, full-rescan global warning, `FullRescanModal` with focus-trap + esc + body-scroll-lock + return-focus.
- `frontend/src/features/search/SearchView.tsx` — sidebar (Dropzone, text query, controls, action) + content (busy overlay, results, sort, DetailPanel).
- `frontend/src/features/search/useSearch.ts` — encapsulates search state, blob-URL lifecycle, abort/timeout, runSearch.
- `frontend/src/features/settings/SettingsView.tsx` — settings grid (Runtime, Library Maintenance, Scan Activity, Account) + DeltaInfo + ChangePasswordModal + UserManagementSection.

`App.tsx` now acts as a thin facade: holds page state, owns stats polling + scan/rescan callbacks + full-rescan controller, mounts `PageShell`, and routes between `SearchView`/`BrowseView`/`SettingsView`.

Behaviour preserved: full-rescan modal focus trap (refs, dialog ref, cancel ref, return-focus ref), transient notices/errors in the search sidebar, view-mode persistence per page (search & browse each own their localStorage key), a11y attrs, role-gated rescan controls, "Rescan started." retirement when scan finishes.

## Files Changed

- `frontend/src/App.tsx` — `930 → 206 LOC`. Holds: AuthProvider/RequireAuth, page nav state, stats polling, rescan/controlScan callbacks, full-rescan modal controller, App-level notice + error slots, route dispatch.
- `frontend/src/components/PageShell.tsx` — **NEW** (317 LOC). Topbar, `FullRescanModal`, scan report line, full-rescan global warning slot, nav + logout. Owns the modal lifecycle (esc, tab-trap, body-scroll-lock, return-focus).
- `frontend/src/features/search/SearchView.tsx` — **NEW** (293 LOC). Dropzone + text query + sort + results + DetailPanel. Owns search-view view-mode persistence + sort state.
- `frontend/src/features/search/useSearch.ts` — **NEW** (190 LOC). Hook owning query inputs + result state + abort/timeout + blob URL lifecycle.
- `frontend/src/features/settings/SettingsView.tsx` — **NEW** (249 LOC). Settings grid + DeltaInfo + ChangePassword modal (local showPw state) + UserManagementSection. Receives `rescan`/`openFullRescanModal` callbacks and role-derived flags from App.

## New file:line refs

- `frontend/src/features/search/SearchView.tsx` — see line 1
- `frontend/src/features/search/useSearch.ts` — see line 1
- `frontend/src/features/settings/SettingsView.tsx` — see line 1
- `frontend/src/components/PageShell.tsx` — see line 1

## Test Results

- `vitest run` — **64 / 64 passed** (14 test files; no regressions, no new tests added — split was structural).
- `tsc --noEmit` — **clean** (no type errors).
- `vite build` — **succeeded** (57 modules transformed, 219 kB JS / 28 kB CSS).
  - One benign warning remains: `lib/storage.ts` is dynamically imported by `AuthContext.tsx` and statically imported by `BrowseView.tsx` + `SearchView.tsx`. Vite prefers the static-import path (single chunk); this only disables the optional chunk-splitting, no functional impact.

## Preserved behaviour

- Full-rescan modal: focus management (cancel ref, dialog ref, return-focus ref), tab focus-trap, esc-to-close, body scroll lock, click-outside cancel, "I understand" checkbox required, confirm-disabled until checked. App captures the trigger element synchronously into `fullRescanReturnFocusRef` before opening; PageShell restores focus after close via `setTimeout(0)`.
- "Rescan started." notice retires when `stats.state` transitions from scanning→idle/paused.
- Search sidebar renders both search-local errors (`useSearch.error`) and App-level errors (rescan/scan-control failures).
- Role gating preserved (canRescan/canFullScan/canSeeDelta in App.tsx, handed to SettingsView).
- `useStatsPolling` hook retained as-is.
- View-mode persistence: `BROWSE_VIEW_MODE_KEY` owned by `BrowseView`, `VIEW_MODE_KEY` now owned by `SearchView`.
- All keyboard / aria attributes mirrored from original.

## Blockers

None.

## Next Step

Run integration / E2E manually if available; otherwise the changes are ready for review.