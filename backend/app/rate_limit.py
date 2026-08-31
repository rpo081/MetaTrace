"""Centralized rate-limit helper (single source of truth).

All routers should import ``check_rate_limit`` from here instead of
copy-pasting the ``_check_rate_limit`` boilerplate. Centralizing keeps
the ``limits`` parsing, ``slowapi.Limit`` wrapping and the private
``request.app.state.limiter._limiter`` access in one place so upgrades
to ``limits``/``slowapi`` only need a single fix site.

Usage:
    from ..rate_limit import check_rate_limit
    check_rate_limit(request, "30/minute")
    check_rate_limit(request, "10/minute", per_endpoint=True)
    check_rate_limit(request, "5/minute", scope_key="custom")
"""
from __future__ import annotations

from fastapi import Request
from limits import parse as parse_limit
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit


def _get_client_ip(request: Request) -> str:
    """Return client IP, respecting `X-Forwarded-For` when `trusted_proxy` is enabled.

    When `METATRACE_TRUSTED_PROXY=true` (behind nginx/traefik), the first
    entry of `X-Forwarded-For` is the original client. Otherwise the direct
    `remote_addr` is used to avoid spoofing. `X-Real-IP` is checked as fallback.
    """
    try:
        settings = getattr(request.app.state, "settings", None)
        if settings and getattr(settings, "trusted_proxy", False):
            xff = request.headers.get("x-forwarded-for")
            if xff:
                # XFF: client, proxy1, proxy2 — first is original
                first = xff.split(",")[0].strip()
                if first:
                    return first
            xri = request.headers.get("x-real-ip")
            if xri and xri.strip():
                return xri.strip()
    except Exception:
        pass
    return get_remote_address(request)


def check_rate_limit(
    request: Request,
    limit_str: str,
    *,
    scope_key: str | None = None,
    per_endpoint: bool = False,
) -> None:
    """Enforce a rate limit via the app's ``slowapi`` limiter.

    Args:
        request: Incoming FastAPI request (must have ``app.state.limiter``).
        limit_str: ``limits`` parseable string, e.g. ``"30/minute"``.
        scope_key: Explicit bucket key override. When set, ``per_endpoint`` is ignored.
        per_endpoint: When ``True`` the bucket is scoped to ``IP:path`` so
            different endpoints do not share a bucket (used by auth routes).

    Raises:
        RateLimitExceeded: when the limit is exceeded (mapped to 429 by slowapi).
    """
    limiter = request.app.state.limiter
    item = parse_limit(limit_str)
    ip = _get_client_ip(request)
    if scope_key is not None:
        key = scope_key
    elif per_endpoint:
        key = f"{ip}:{request.url.path}"
    else:
        key = ip
    # Use public `limiter.limiter` property (slowapi 0.1.9) instead of private `_limiter`
    # to avoid breakage on upgrades. Falls back to _limiter if property missing.
    _lim = getattr(limiter, "limiter", None) or getattr(limiter, "_limiter", None)
    if _lim is None or not _lim.hit(item, key):
        limit_wrapper = Limit(
            limit=item,
            key_func=_get_client_ip,
            scope=None,
            per_method=False,
            methods=None,
            error_message=None,
            exempt_when=None,
            cost=1,
            override_defaults=False,
        )
        request.state.view_rate_limit = (item, [key])
        raise RateLimitExceeded(limit_wrapper)
