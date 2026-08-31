# Ethical-Hacker Audit — Login Page

**Scope:** `/api/auth/login` + `LoginPage.tsx` + auth dependency chain
**Date:** 2026-08-31
**Auditor:** ethical-hacker agent
**Verdict:** No backdoor, no debug bypass. Two **low/medium** hardening items, three **low**, one **info**.

---

## Files inspected

| File | Purpose |
|---|---|
| `backend/app/api/auth.py` | login / register / logout / refresh / me / change-password routes |
| `backend/app/auth.py` | Argon2id hashing, JWT (HS256), refresh-token rotation, HMAC file URLs |
| `backend/app/models/auth.py` | Pydantic request/response models + password-complexity validator |
| `backend/app/dependencies.py` | `get_current_user`, `require_role`, legacy-token fallback, signed-URL verify |
| `backend/app/rate_limit.py` | slowapi-backed rate-limit helper (X-Forwarded-For gated by `trusted_proxy`) |
| `backend/app/main.py` | lifespan, `_seed_admin_if_needed` (DEFAULT_ADMIN_PASSWORD = `"changeme"`) |
| `backend/app/db.py` | `record_login_failure` / `record_login_success` (lockout counters) |
| `backend/app/config.py` | pydantic-settings — `jwt_secret` ≥32 chars, lockout defaults 5/15-min |
| `frontend/src/features/auth/LoginPage.tsx` | login form UI |
| `frontend/src/features/auth/AuthContext.tsx` | token storage + boot/refresh flow |
| `frontend/src/api.ts` | `ApiError` + friendly message map |

## Searches run

- `grep -E "DEBUG|debug_mode|test.*token|backdoor"` across backend → no production backdoor routes
- `grep -E "changeme|must_change_password|seed_admin"` → confirms only seed path uses literal password
- Routed grep for `(login|auth|bypass|backdoor|debug)` → only `POST /api/auth/login` is defined; no debug routes

---

## Findings

### No backdoor / no debug skip

- There is **no** `?debug=1`, header-flag, env-conditional, or admin-token skip that bypasses `/api/auth/login` password checks.
- `verify_password()` runs Argon2id verification unconditionally (`api/auth.py:180`). The only branch before it is the existence/timing-balance branch (`api/auth.py:162–165`) which still calls `hash_password(body.password)` to neutralise timing — that is correct hardening, not a backdoor.
- `LoginRequest` is `{username, password}` only — no `bypass`, `admin_override`, etc. Pydantic strips unknown fields by default.
- The seeded `"changeme"` admin password is a *known* bootstrap credential, not a hidden backdoor: it is announced at startup, the user is marked `must_change_password=1`, and the dependency chain in `dependencies._enforce_password_change` 403s every endpoint except `/api/auth/change-password`, `/logout`, `/me` until the flag is cleared.

### Strengths worth noting

| Control | Location | Notes |
|---|---|---|
| Argon2id, `time_cost=2 memory=19MiB parallelism=1 salt=16 hash=32 type=ID` | `auth.py:24–31` | Matches OWASP 2026 minimum-cost recommendation for Argon2id |
| JWT HS256 with `iss`/`aud`/`exp`/`iat`/`jti` + `algorithms=["HS256"]` (no `none`) | `auth.py:78–90` | Algorithm explicitly pinned; `decode_access_token` requires `exp`, `iat`, `sub`, `iss`, `aud` |
| Refresh tokens: opaque (`secrets.token_urlsafe(48)`), **SHA-256 hashed at rest**, rotated, **family revocation on reuse** | `auth.py:127–221` | Hashes never leave the DB; reuse detection revokes the whole family |
| Account lockout (5 failed / 15 min), per-user counters | `api/auth.py:181–187`, `db.py:700–716` | `locked_until` reset on success; lockout response is 423 not 401 |
| `Cache-Control: no-store` on `/login`, `/refresh`, `/me` | `api/auth.py:208, 292, 303` | Prevents token cache in shared proxies/browsers |
| Refresh cookie `httponly + secure (when enabled) + samesite + path=/api/auth/refresh` | `api/auth.py:49–58` | Scoped to refresh endpoint — can't ride other routes |
| Access cookie `__Host-metatrace_access` when `cookie_secure=True` | `api/auth.py:71–84` | `__Host-` prefix enforces Secure + path=/ + no Domain |
| Per-IP rate limit (`10/minute`, `per_endpoint=True`) | `api/auth.py:157` | Bucket key is `ip:/api/auth/login` — bypass needs different IP per attempt |
| `X-Forwarded-For` only trusted when `METATRACE_TRUSTED_PROXY=true` | `rate_limit.py:32–44` | Prevents header-spoofed rate-limit bypass |
| Password complexity validator (≥8 + upper + lower + digit) | `models/auth.py:47–52` | Enforced on register / change-password / admin-reset (login itself stays lenient — correct, Argon2id is the defence) |
| Security headers: CSP `default-src 'self'`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` | `main.py:248–259` | CSP `script-src 'self'` blocks inline token-exfil XSS |
| CORS: opt-in only; SPA served same-origin by default | `main.py:232–243` | No wildcard origins; `allow_credentials=True` requires explicit list |
| Login response leaks nothing about user existence (constant-time-ish fallback hashes on missing user) | `api/auth.py:162–165` | 401 message identical for unknown user vs wrong password |
| Frontend: errors mapped to friendly copy; raw `err.message` only used as last-resort fallback | `LoginPage.tsx:36–41` | No server stack traces leaked to UI |

---

## MED-1 · "Account locked" vs "invalid credentials" oracle (intentional trade-off)

`api/auth.py:166–178`

```python
if row["locked_until"] is not None:
    ...
    if locked_until > datetime.now(timezone.utc):
        raise HTTPException(423, "account temporarily locked")
```

The login flow distinguishes 423 (locked) from 401 (invalid) *only* on the final attempt. The attacker can therefore confirm a valid username by triggering the lockout state for that account (5 wrong passwords → 423). This is the same behaviour as nearly every production auth system; it is intentional.

- **Severity:** Low/Medium depending on threat model. Username enumeration is also visible via the timing fallback (`hash_password` runs even on unknown user) but is harder to exploit than the 423 oracle.
- **Recommendation (optional):** Either return 401 for both states ("invalid credentials — try again later") and rely solely on rate-limiting to slow attackers, or document the accepted trade-off. Current behaviour is **acceptable**.

## MED-2 · Refresh cookie `Secure` flag is configurable without startup warning

`config.py:98`, `api/auth.py:49–58`

`cookie_secure` defaults to `True`, but if an operator toggles it to `False` (no enforcement is done — `Settings.cookie_secure: bool = True`), the refresh cookie is no longer `Secure` and will be transmitted over HTTP and via mixed contexts. There is no startup warning when `cookie_secure=False`.

- **Severity:** Low (only matters in dev or behind a TLS-terminating proxy where operators may wrongly disable it).
- **Recommendation:** In `Settings.cookie_secure` validator or `main.lifespan`, log a `WARNING` (or refuse to start) if `cookie_secure=False` and `not settings.allow_unauthenticated`.

## LOW-1 · Default seed password `"changeme"` (intentional, but a real attack surface)

`main.py:83`, `_seed_admin_if_needed` (`main.py:86–155`)

The service ships with `DEFAULT_ADMIN_PASSWORD = "changeme"` and will create an `admin` user with that exact credential when the DB is empty and `METATRACE_ADMIN_TOKEN` is unset. Mitigations:

- `must_change_password=1` (gates everything except `/change-password`, `/logout`, `/me`).
- WARNING-level startup log.
- Complexity-validated replacement required.

The `must_change_password` gate is enforced by `dependencies._enforce_password_change` against a hard-coded whitelist (`frozenset({"/api/auth/change-password", "/api/auth/logout", "/api/auth/me"})`), and compared with integer `!= 1` (not truthiness) so a future schema change cannot silently relax it.

- **Severity:** Low. Mitigations are adequate; recommend a one-line README or first-run banner.

## LOW-2 · Lockout timestamp relies on server clock

`api/auth.py:170–174`, `db.py:700–716`

`locked_until` is computed *server-side* (`datetime('now', '+15 minutes')` in SQLite) and parsed in Python. If the operator's clock drifts, the lockout window may extend or shorten incorrectly. Python comparison uses explicit UTC handling, so this is a robustness concern more than a security one.

- **Severity:** Info.
- **Recommendation:** Consider monotonic clock for short lockouts; document `tzdata` dependency.

## LOW-3 · `LoginRequest` has no length cap on `username`/`password`

`models/auth.py:71–73`

```python
class LoginRequest(BaseModel):
    username: str
    password: str
```

Pydantic only enforces that they are strings. A 10 MiB `password` would be hashed by Argon2id (cost ~tens of ms) but consumed as a string by `body.password`. `RegisterRequest` and `ChangePasswordRequest` both cap at `max_length=128`. The asymmetry is suspicious.

- **Severity:** Low (only matters under adversarial flooding; mitigated by `10/minute` IP rate limit).
- **Recommendation:** Mirror `min_length=8, max_length=128` on `LoginRequest.password` to bound worst-case Argon2 work per request.

## INFO-1 · Passwords are never logged

Confirmed by grep — the seed code explicitly does **not** interpolate the seed password into the log line (`main.py:104–111`). The `_TokenRedactFilter` installed at `main.py:67` is attached to `uvicorn.access`/`uvicorn.error` so JWTs in `Authorization` headers are not written to logs.

---

## What is *not* a vulnerability

- `record_login_failure` runs on **wrong password** only, after `verify_password` returns False. It does not run on the timing-balance branch (unknown user) — correct, prevents counter-warm DoS against arbitrary usernames.
- The legacy `X-Admin-Token` / `?admin_token=` / `?token=` fallback is gated by `settings.admin_token` being set; when unset, it falls through to `allow_unauthenticated` opt-in. Login itself is unaffected.
- `__virtual__: True` dicts synthesised for legacy / trusted-LAN modes pass through role checks as `"admin"`. This is intentional (legacy mode is admin-by-definition) and is the documented transition path.

---

## Recommendations (priority order)

1. Add a startup WARNING when `cookie_secure=False` to prevent accidental plaintext cookie transmission on internet-facing deployments. (MED-2)
2. Cap `LoginRequest.password` at `max_length=128` to mirror `RegisterRequest` / `ChangePasswordRequest`. (LOW-3)
3. Optionally normalise 423 to 401 to remove the username-existence oracle — only if the trade-off is acceptable. (MED-1)
4. Document the seeded `"changeme"` admin password prominently in `README.md` / first-run banner so it is impossible to miss.

## Verdict

The login page implementation is **sound**. Argon2id with current parameters, opaque SHA-256-hashed refresh tokens with rotation + family revocation, JWT pinned to HS256 with iss/aud/exp checks, account lockout, per-IP rate limit, strict CSP, httpOnly+scoped cookies, and a server-side enforced `must_change_password` gate that closes the bootstrap window. The items above are hardenings, not backdoors or exploitable flaws.
