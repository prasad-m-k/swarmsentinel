"""Verify Clerk session JWTs at the engine boundary; never trust identity headers."""
import base64
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyCookie, HTTPBearer

cookie_security = APIKeyCookie(name="__session", scheme_name="ClerkCookie", auto_error=False)
bearer_security = HTTPBearer(scheme_name="ClerkBearer", auto_error=False)


# Role -> permissions. Personal tenants make their user "admin", which is the pre-RBAC behaviour.
ROLES = {
    "viewer": frozenset({"session.read"}),
    "operator": frozenset({"session.read", "session.create", "session.act", "session.delete.own"}),
    "admin": frozenset({"session.read", "session.create", "session.act", "session.delete.own", "session.delete.any"}),
}
DEFAULT_ORG_ROLES = {"admin": "admin", "member": "operator"}


@dataclass(frozen=True)
class Principal:
    subject: str
    expires: float
    # Empty tenant means the user's personal tenant. Tenants are derived from verified claims only.
    tenant: str = ""
    role: str = "admin"

    @property
    def tenant_id(self):
        return self.tenant or f"user:{self.subject}"

    @property
    def permissions(self):
        return ROLES.get(self.role, frozenset())

    def can(self, permission):
        return permission in self.permissions


def org_role_map():
    """SWARM_ORG_ROLES="admin=admin,member=operator,auditor=viewer" maps organization roles to engine roles."""
    configured = os.environ.get("SWARM_ORG_ROLES", "")
    pairs = (item.split("=", 1) for item in configured.split(",") if "=" in item)
    mapping = {k.strip(): v.strip() for k, v in pairs if v.strip() in ROLES} if configured else {}
    return mapping or DEFAULT_ORG_ROLES


def tenancy(claims):
    """(tenant, role) from verified Clerk claims. Supports v2 ("o": {id, rol}) and v1 (org_id, org_role) tokens."""
    org = claims.get("o") if isinstance(claims.get("o"), dict) else {}
    org_id = org.get("id") or claims.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        return "", "admin"
    raw = org.get("rol") or claims.get("org_role") or ""
    role = org_role_map().get(str(raw).removeprefix("org:"), "viewer")
    return f"org:{org_id}", role


@lru_cache(maxsize=4)
def key_client(issuer):
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json", timeout=5)


def auth_config():
    # Derive the trusted issuer from operator configuration, never from a token.
    key = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
    try:
        encoded = key.split("_", 2)[2]
        host = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode().rstrip("$")
        parsed = urlsplit(f"https://{host}")
        if parsed.hostname != host or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Invalid issuer")
    except (IndexError, ValueError, UnicodeError):
        raise HTTPException(503, "Authentication is not configured")
    parties = set(filter(None, (v.strip() for v in os.environ.get("SWARM_AUTHORIZED_PARTIES", "").split(","))))
    dev_host = os.environ.get("REPLIT_DEV_DOMAIN")
    if dev_host:
        parties.add(f"https://{dev_host}")
    if not parties:
        raise HTTPException(503, "Trusted application origins are not configured")
    return f"https://{host}", parties


def require_principal(request: Request,
                      _cookie=Security(cookie_security), _bearer=Security(bearer_security)) -> Principal:
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise HTTPException(401, "Authentication required")
    else:
        token = request.cookies.get("__session")
    if not token:
        raise HTTPException(401, "Authentication required")
    issuer, parties = auth_config()
    try:
        signing_key = key_client(issuer).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, signing_key, algorithms=["RS256"], issuer=issuer,
                            options={"require": ["sub", "exp", "iat", "nbf", "iss", "sid", "azp"]})
        if not isinstance(claims["sub"], str) or not claims["sub"] or not isinstance(claims["sid"], str) or not claims["sid"]:
            raise jwt.InvalidTokenError()
        if claims["azp"] not in parties:
            raise jwt.InvalidTokenError()
        # Cookie mutations need an Origin check even with a valid signed cookie.
        # GET snapshots/SSE remain same-origin via the browser's SOP (no CORS).
        if request.method not in ("GET", "HEAD", "OPTIONS") and not authorization:
            if request.headers.get("origin") not in parties:
                raise HTTPException(403, "Untrusted request origin")
        tenant, role = tenancy(claims)
        return Principal(claims["sub"], float(claims["exp"]), tenant, role)
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Authentication service unavailable")
    except (jwt.PyJWTError, TypeError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired authentication")


def still_valid(principal):
    return principal.expires > time.time()


def village_access(principal):
    approved = set(filter(None, os.environ.get("SWARM_VILLAGE_READERS", "").split(",")))
    return os.environ.get("SWARM_ENABLE_VILLAGE") == "true" and principal.subject in approved