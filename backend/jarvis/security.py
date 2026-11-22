import json
import math
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class BoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        settings = request.app.state.settings
        origin = urlsplit(settings.public_origin)
        if request.headers.get("host", "") != origin.netloc:
            return JSONResponse({"detail": "Untrusted host"}, status_code=400)
        if request.url.scheme != origin.scheme:
            return JSONResponse({"detail": "Transport does not match configured origin"}, status_code=400)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, status_code=400)
        if length < 0 or length > 16384:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        body = await request.body()
        if len(body) > 16384:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        store = request.app.state.store
        record = store.session(request.cookies.get(settings.cookie_name), settings.session_idle_seconds)
        data = dict(record["data"]) if record else {}
        initial = json.dumps(data, sort_keys=True)
        request.scope["session"] = data
        request.state.session_record = record
        request.state.rotate = False
        request.state.logout = False
        response = await call_next(request)
        # Only update existing sessions; a revoked session is never resurrected.
        if request.state.logout:
            if record:
                store.revoke(record["key"])
            response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="lax")
        elif data:
            if request.state.rotate or not record:
                if record:
                    store.revoke(record["key"])
                token = store.new_session(data, settings.session_absolute_seconds if data.get("principal") else 300)
                response.set_cookie(settings.cookie_name, token, max_age=settings.session_absolute_seconds if data.get("principal") else 300, path="/", secure=settings.secure_cookie, httponly=True, samesite="lax")
            elif initial != json.dumps(data, sort_keys=True):
                if not store.save_session(record["key"], data, record["version"]):
                    return JSONResponse({"detail": "Session changed; sign in again"}, status_code=409)
        response.headers.update({
            "Cache-Control": "no-store", "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })
        if settings.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response


def validate_principal(claims, settings, now=None):
    now = now or time.time()
    # Signature/nonce/audience/expiry verification occurs in Authlib before this policy.
    if claims.get("iss") != settings.oidc_issuer or claims.get("sub") != settings.admin_subject:
        raise HTTPException(403, "Identity is not an enrolled administrator")
    if claims.get("acr") not in settings.accepted_acr:
        raise HTTPException(403, "Required authentication assurance was not established")
    auth_time = claims.get("auth_time")
    if isinstance(auth_time, bool) or not isinstance(auth_time, (int, float)) or not math.isfinite(auth_time) or auth_time > now + 30 or now - auth_time > settings.fresh_auth_seconds:
        raise HTTPException(403, "A fresh verified sign-in is required")
    return {"subject": claims["sub"], "issuer": claims["iss"], "role": "admin", "auth_time": auth_time, "acr": claims["acr"]}


def require_admin(request: Request):
    p = request.session.get("principal")
    s = request.app.state.settings
    if not s.auth_configured or not p:
        raise HTTPException(401, "Administrator sign-in required")
    if p.get("subject") != s.admin_subject or p.get("issuer") != s.oidc_issuer or p.get("role") != "admin" or p.get("acr") not in s.accepted_acr:
        raise HTTPException(403, "Administrator authorization revoked")
    return p


def require_write(request: Request, fresh=False):
    p = require_admin(request)
    if request.headers.get("origin") != request.app.state.settings.public_origin:
        raise HTTPException(403, "Same-origin request required")
    token = request.headers.get("x-csrf-token", "")
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(403, "Request verification failed")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "JSON request required")
    if fresh and time.time() - p["auth_time"] > request.app.state.settings.fresh_auth_seconds:
        raise HTTPException(403, "Sign in again before this action")
    return p
