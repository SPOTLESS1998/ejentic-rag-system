"""
Authentication for the Ejentic RAG — the ONLY place a caller's role is decided.

WHY THIS EXISTS
---------------
The retrieval layer filters documents by a `clearance` tag, and it does that job
correctly. But until this module existed, the clearance was simply a FIELD IN THE
REQUEST BODY: any caller could put `"clearance_level": "executive"` in a plain
curl and read board-only material. Authorization (given a role, filter right) was
sound; AUTHENTICATION (proving you ARE that role) did not exist, and
authorization on an unauthenticated claim is an honour system, not a boundary.

THE MODEL
---------
Each tenant's config maps a ROLE to the NAME of an env var holding that role's
API key (never the key itself — see MULTITENANCY.md):

    "auth": {
      "required": true,
      "keys": {"guest": "RAG_KEY_GUEST", "executive": "RAG_KEY_EXECUTIVE"},
      "admin_role": "executive"
    }

A caller presents the key as `X-API-Key` (or `Authorization: Bearer <key>`), and
the server derives the role FROM THE KEY. The request body may then only ever
NARROW that role — an executive key may ask to see the guest view, but a guest
key asking for executive is a loud 403, never a silent downgrade.

FAIL-CLOSED
-----------
`required: true` with no key env vars set is a CONFIGURATION ERROR that refuses
to boot (validated in client_registry). It must never degrade to open access —
that is the exact failure mode this module was written to remove.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException

# Header names we accept a key on. `X-API-Key` is the documented one; Bearer is
# accepted because n8n, curl habits and most HTTP clients reach for it first.
API_KEY_HEADER = "X-API-Key"


class AuthError(Exception):
    """Raised for an auth failure. `status` is the HTTP code to surface.

    Kept as a plain exception (not HTTPException) so the pure functions here stay
    framework-free and directly unit-testable without a request object.
    """

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def auth_config(cfg: dict) -> dict:
    """The tenant's auth block, with safe defaults for a config that omits it."""
    raw = cfg.get("auth") or {}
    return {
        "required": bool(raw.get("required", False)),
        "keys": dict(raw.get("keys") or {}),
        "admin_role": (raw.get("admin_role") or "").strip(),
    }


def configured_roles(cfg: dict) -> list:
    """Roles that have a key env var NAME declared (regardless of whether the
    value is set in the environment yet)."""
    return sorted(auth_config(cfg)["keys"])


def available_roles(cfg: dict) -> list:
    """Roles whose key env var is actually SET in this process's environment.

    The gap between this and configured_roles() is what makes a half-configured
    deployment visible: a role declared in config but with no value in .env can
    never be authenticated, so we surface it rather than pretend it works.
    """
    out = []
    for role, env_name in auth_config(cfg)["keys"].items():
        if (os.environ.get(env_name) or "").strip():
            out.append(role)
    return sorted(out)


def is_enabled(cfg: dict) -> bool:
    """True when this instance requires authentication."""
    return auth_config(cfg)["required"]


def resolve_role(api_key: Optional[str], cfg: dict) -> Optional[str]:
    """Reverse-map a presented key to the role it grants. None if it matches none.

    Comparison uses hmac.compare_digest so a wrong key takes the same time to
    reject regardless of how many leading characters were right — a plain `==`
    on secrets leaks their prefix to anyone who can time the response.

    Every configured role is compared even after a match is found, so the work
    done does not depend on WHICH role matched either.
    """
    presented = (api_key or "").strip()
    if not presented:
        return None

    matched: Optional[str] = None
    for role, env_name in sorted(auth_config(cfg)["keys"].items()):
        expected = (os.environ.get(env_name) or "").strip()
        if not expected:
            continue  # declared in config but not set in .env -> cannot authenticate
        if hmac.compare_digest(presented, expected) and matched is None:
            matched = role
    return matched


def _rank(role: str, cfg: dict) -> int:
    """How much a role can see, as a sortable number. Higher = broader.

    A wildcard ("*") role sees everything, so it ranks above every explicit list;
    otherwise the count of allowed tags is the natural measure. Used only to
    decide whether a requested clearance would WIDEN what the key already grants.
    """
    tags = (cfg.get("clearance_levels") or {}).get(role)
    if tags == "*":
        return 10_000
    if isinstance(tags, list):
        return len(tags)
    return -1  # unknown role: narrower than anything real


def effective_clearance(granted_role: str, requested: Optional[str], cfg: dict) -> str:
    """The clearance a request may actually use.

    THE NARROWING RULE: the body may ask for less than the key grants (useful and
    safe — an executive key checking what a guest sees), but asking for MORE is a
    403. Refusing loudly matters: a silent downgrade would let a misconfigured
    caller believe it had executive reach while quietly getting public answers.
    """
    want = (requested or "").strip().lower()
    if not want or want == granted_role:
        return granted_role

    levels = cfg.get("clearance_levels") or {}
    if want not in levels:
        # An unknown role is not a privilege escalation, but it IS a mistake we
        # should not silently honour — the retrieval layer would fail it closed
        # to the least-privileged tier, which looks like "the KB is empty".
        raise AuthError(
            400,
            f"unknown clearance_level '{want}'. This client's roles are: "
            f"{', '.join(sorted(levels))}.",
        )

    if _rank(want, cfg) > _rank(granted_role, cfg):
        raise AuthError(
            403,
            f"your key grants '{granted_role}' clearance; it cannot request "
            f"'{want}'. A request may narrow its own clearance, never widen it.",
        )
    return want


def authenticate(api_key: Optional[str], cfg: dict) -> str:
    """Turn a presented key into a role, or raise AuthError.

    When auth is disabled (local dev), the caller is treated as the LEAST
    privileged configured role rather than the most — a dev instance should not
    be more permissive than production by accident.
    """
    if not is_enabled(cfg):
        roles = configured_roles(cfg)
        if roles:
            return min(roles, key=lambda r: _rank(r, cfg))
        levels = cfg.get("clearance_levels") or {}
        return min(levels, key=lambda r: _rank(r, cfg)) if levels else "guest"

    if not (api_key or "").strip():
        raise AuthError(
            401,
            f"missing API key. Send it as the {API_KEY_HEADER} header "
            "(or 'Authorization: Bearer <key>').",
        )

    role = resolve_role(api_key, cfg)
    if role is None:
        # Deliberately vague: never hint whether the key was unknown, revoked, or
        # merely unset in this deployment's environment.
        raise AuthError(401, "invalid API key.")
    return role


def require_admin(role: str, cfg: dict) -> None:
    """Gate the destructive/administrative endpoints (/ingest).

    With no admin_role configured, NO role is an admin. That is the fail-closed
    reading: an unconfigured admin must not mean "everyone".
    """
    admin = auth_config(cfg)["admin_role"]
    if not admin:
        raise AuthError(
            403,
            "this instance has no auth.admin_role configured, so no caller may "
            "perform administrative actions. Set it in the client config.",
        )
    if role != admin:
        raise AuthError(403, f"this action requires the '{admin}' role.")


def key_from_headers(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """Pull the key out of whichever header carried it."""
    if (x_api_key or "").strip():
        return x_api_key.strip()
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ---------------------------------------------------------------------------
# FastAPI glue — thin wrappers so endpoints stay readable and the logic above
# remains testable without a web request.
# ---------------------------------------------------------------------------
def as_http(err: AuthError) -> HTTPException:
    """Convert our framework-free error into the HTTP response FastAPI wants."""
    headers = {"WWW-Authenticate": API_KEY_HEADER} if err.status == 401 else None
    return HTTPException(status_code=err.status, detail=err.detail, headers=headers)


def caller_role(cfg: dict, x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER),
                authorization: Optional[str] = Header(default=None)) -> str:
    """Resolve the caller's role from request headers, raising HTTPException.

    Not used as a FastAPI dependency directly (the active client config has to be
    bound first); main.py wraps it. Kept here so every path that decides identity
    lives in this module.
    """
    try:
        return authenticate(key_from_headers(x_api_key, authorization), cfg)
    except AuthError as e:
        raise as_http(e)


def status(cfg: dict) -> dict:
    """Auth posture for the health endpoint.

    An UNPROTECTED instance must be visible at a glance rather than assumed safe,
    so this is reported publicly. It exposes role NAMES and whether keys are
    present — never a key, and never which env var holds it.
    """
    cfgd = auth_config(cfg)
    declared = configured_roles(cfg)
    live = available_roles(cfg)
    return {
        "required": cfgd["required"],
        "roles": declared,
        "roles_with_keys_set": live,
        "roles_missing_keys": [r for r in declared if r not in live],
        "admin_role": cfgd["admin_role"] or None,
    }
