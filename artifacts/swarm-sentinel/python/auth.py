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


@dataclass(frozen=True)
class Principal:
    subject: str
    expires: float


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
        return Principal(claims["sub"], float(claims["exp"]))
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Authentication service unavailable")
    except (jwt.PyJWTError, TypeError, ValueError, KeyError):
        raise HTTPException(401, "Invalid or expired authentication")


def still_valid(principal):
    return principal.expires > time.time()


def village_access(principal):
    approved = set(filter(None, os.environ.get("SWARM_VILLAGE_READERS", "").split(",")))
    return os.environ.get("SWARM_ENABLE_VILLAGE") == "true" and principal.subject in approved