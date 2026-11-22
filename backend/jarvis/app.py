import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .security import BoundaryMiddleware, require_admin, require_write, validate_principal
from .store import Store, add_audit, audit, control, tasks
from .business import BusinessService,VaultRepository
from .secrets import SecretVault
from .sheets import SheetsError
from .setup_routes import router as setup_router
from .runner import Runner,router as runner_router


WEB = Path(__file__).parent / "web"


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=3, max_length=2000)
    request_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


def create_app(settings=None, store=None, repository=None, vault=None):
    s = settings or Settings()
    @asynccontextmanager
    async def lifespan(application):
        try:
            if application.state.vault.get("bridge_secret"):
                application.state.runner.bridge.start()
        except Exception:
            pass
        yield
        application.state.runner.bridge.stop()
    app = FastAPI(title="Jarvis-Trade", docs_url=None, redoc_url=None, openapi_url=None,lifespan=lifespan)
    app.state.settings = s
    app.state.store = store or Store(s.database_url)
    app.state.vault=vault or SecretVault()
    app.state.business=BusinessService(repository or VaultRepository(app.state.vault))
    app.state.runner=Runner(app.state.vault,app.state.business.repository)
    app.include_router(setup_router)
    app.include_router(runner_router)
    @app.exception_handler(SheetsError)
    async def sheets_error(request,error):
        status=409 if error.code in ("REVISION_CONFLICT","IDEMPOTENCY_CONFLICT") else 503
        return JSONResponse({"detail":error.code},status_code=status)
    if s.database_auto_create:
        app.state.store.initialize()
    app.add_middleware(BoundaryMiddleware)
    app.add_middleware(RequestLimitMiddleware)
    oauth = OAuth()
    if s.auth_configured:
        oauth.register("identity", client_id=s.oidc_client_id, client_secret=s.oidc_client_secret.get_secret_value(),
            server_metadata_url=s.oidc_issuer.rstrip("/")+"/.well-known/openid-configuration",
            client_kwargs={"scope": "openid", "code_challenge_method": "S256"},
            authorize_params={"acr_values": " ".join(s.accepted_acr), "max_age": 0},
            claims_options={"iss": {"essential": True, "values": [s.oidc_issuer]}})
    app.state.oauth = oauth

    async def verified_identity_metadata():
        metadata = await oauth.identity.load_server_metadata()
        if metadata.get("issuer") != s.oidc_issuer:
            raise ValueError("Discovery issuer does not match enrolled issuer")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if urlsplit(metadata.get(field, "")).scheme != "https":
                raise ValueError("Identity endpoints must use HTTPS")
        algorithms = [x for x in metadata.get("id_token_signing_alg_values_supported", ["RS256"]) if x in ("RS256", "ES256")]
        if not algorithms:
            raise ValueError("Identity signing algorithms are not permitted")
        oauth.identity.server_metadata["id_token_signing_alg_values_supported"] = algorithms

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/guide")
    def onboarding_guide():
        return FileResponse(WEB / "guide.html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "app.css", "intake.js", "onboarding.js", "onboarding.css", "onboarding-workbook.png", "onboarding-bridge.png"}:
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/api/access")
    def access(request: Request):
        # Public enrollment status contains no account identity, credentials or data.
        return {"configured": s.auth_configured, "authenticated": bool(request.session.get("principal")) and s.auth_configured,
                "local_only": not s.secure_cookie, "login_url": "/auth/login"}

    @app.get("/auth/login")
    async def login(request: Request):
        if not s.auth_configured:
            raise HTTPException(503, "Administrator identity provider is not configured")
        if not app.state.store.permit(request.client.host, "login", 10, 300):
            raise HTTPException(429, "Sign-in rate limit reached")
        # Only a single outstanding flow in this browser; signed state/nonce + PKCE.
        request.session.clear()
        request.state.rotate = True
        request.session["login_started"] = time.time()
        try:
            await verified_identity_metadata()
        except Exception:
            request.session.clear()
            request.state.logout = True
            raise HTTPException(503, "Identity provider could not be verified") from None
        return await oauth.identity.authorize_redirect(request, s.public_origin+"/auth/callback", nonce=secrets.token_urlsafe(32))

    @app.get("/auth/callback")
    async def callback(request: Request):
        if not s.auth_configured or time.time() - request.session.get("login_started", 0) > 300:
            raise HTTPException(401, "Sign-in expired")
        record = request.state.session_record
        if not record or request.session.get("callback_claimed"):
            raise HTTPException(401, "Sign-in response already consumed")
        request.session["callback_claimed"] = True
        if not app.state.store.save_session(record["key"], dict(request.session), record["version"]):
            raise HTTPException(401, "Sign-in response already consumed")
        record["version"] += 1
        try:
            await verified_identity_metadata()
            token = await oauth.identity.authorize_access_token(request, claims_options={"iss": {"essential": True, "values": [s.oidc_issuer]}}, leeway=30)
            if not token.get("id_token") or not token.get("userinfo"):
                raise ValueError("An authenticated ID token is required")
            principal = validate_principal(dict(token["userinfo"]), s)
        except HTTPException:
            request.session.clear()
            request.state.logout = True
            with app.state.store.engine.begin() as c:
                add_audit(c, "unverified", "admin.sign_in_rejected")
            raise
        except Exception:
            request.session.clear()
            request.state.logout = True
            with app.state.store.engine.begin() as c:
                add_audit(c, "unverified", "admin.sign_in_rejected")
            raise HTTPException(401, "Identity verification failed") from None
        request.session.clear()
        request.session.update(principal=principal, csrf=secrets.token_urlsafe(32))
        request.state.rotate = True
        with app.state.store.engine.begin() as c:
            add_audit(c, principal["subject"], "admin.signed_in")
        return RedirectResponse("/", status_code=303)

    @app.get("/api/session")
    def session(request: Request):
        p = require_admin(request)
        return {"role": p["role"], "csrf": request.session["csrf"], "fresh_until": p["auth_time"]+s.fresh_auth_seconds}

    @app.post("/auth/logout")
    def logout(request: Request):
        p = require_write(request)
        request.session.clear()
        request.state.logout = True
        with app.state.store.engine.begin() as c:
            add_audit(c, p["subject"], "admin.signed_out")
        return {"status": "signed_out"}

    @app.get("/api/overview")
    async def overview(request: Request):
        require_admin(request)
        try:
            state=await app.state.business.overview(); storage="google_sheets"
        except SheetsError:
            state={"control":{"id":1,"paused":True,"revision":0},"tasks":[],"audit":[]}; storage="not_connected"
        bridge=app.state.runner.status()
        model_configured=bool(app.state.vault.get("xai_key"))
        return {**state,"storage":storage,
            "bridge": bridge,
            "agent": {"state": "CLASSIFIER_CONFIGURED" if model_configured else "MODEL_NOT_CONFIGURED", "sending_enabled": False},
            "mode": "local-development" if s.environment == "development" else "production",
            "capabilities": [
                {"name": "Administrator access", "state": "verified session"},
                {"name": "Google Sheets ledger", "state": "connected" if storage=="google_sheets" else "not connected"},
                {"name": "WhatsApp extension", "state": bridge["state"]},
                {"name": "Message classifier", "state": "configured; live verification required" if model_configured else "not configured"},
                {"name": "Message dispatch", "state": "disabled"},
            ]}

    @app.post("/api/tasks", status_code=201)
    async def create_task(goal: Goal, request: Request):
        p = require_write(request)
        if not app.state.store.permit(p["subject"], "task", 20):
            raise HTTPException(429, "Task rate limit reached")
        record,created=await app.state.business.create_task(goal.title,goal.request_key,p["subject"])
        return JSONResponse({**record["data"],"id":record["id"],"revision":record["revision"]},status_code=201 if created else 200)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str, version: Revision, request: Request):
        p = require_write(request)
        await app.state.business.cancel_task(task_id,version.revision)
        return {"status": "cancelled"}

    @app.post("/api/control/pause")
    async def pause(version: Revision, request: Request):
        p = require_write(request)
        await app.state.business.pause(version.revision)
        return {"paused": True}

    @app.post("/api/control/resume")
    def resume(request: Request):
        require_write(request, fresh=True)
        # There is no dispatch adapter yet. A UI toggle must not bypass this gate.
        raise HTTPException(409, "WhatsApp bridge and dispatch capability are not verified; sending remains paused")

    @app.post("/api/dashboard-link")
    def dashboard_link(request: Request):
        p = require_write(request)
        with app.state.store.engine.begin() as c:
            add_audit(c, p["subject"], "dashboard.link_prepared")
        return {"url": s.public_origin+"/", "grants_access": False, "sent_to_whatsapp": False,
                "phone_reachable": s.environment == "production" and s.secure_cookie}

    return app


class RequestLimitMiddleware:
    """Bound bytes before any JSON parsing or session middleware; reject WS until implemented."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 16384:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
            chunks.append(message)
            if not message.get("more_body", False):
                break
        async def bounded_receive():
            return chunks.pop(0) if chunks else await receive()
        await self.app(scope, bounded_receive, send)


app = create_app()
