import base64
import hashlib
import io
import json
import struct
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select, update

from jarvis.app import create_app
from jarvis.bridge import MAX_FRAME, check_origin, read_frame, write_frame
from jarvis.config import Settings
from jarvis.domain import quote_usd
from jarvis.store import Store, audit, digest, sessions, tasks
from conftest import FakeSheets,FakeVault


@pytest.fixture
def setup_app():
    settings = Settings(_env_file=None, public_origin="https://desk.example", database_url="sqlite+pysqlite:///:memory:",
        oidc_issuer="https://identity.example", oidc_client_id="jarvis", oidc_client_secret="test-only-secret",
        admin_subject="owner-123", accepted_acr=["urn:test:passkey"])
    store = Store(settings.database_url)
    repository=FakeSheets()
    repository.records["configuration","control"]={"id":"control","revision":1,"data":{"paused":True}}
    app = create_app(settings, store,repository=repository,vault=FakeVault())
    key = RSAKey.generate_key(2048, parameters={"kid":"test-signing-key"})
    state = {"nonce":"", "challenge":"", "claims":{}, "wrong_key":False}
    def identity(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200,json={"issuer":settings.oidc_issuer,
                "authorization_endpoint":"https://identity.example/authorize", "token_endpoint":"https://identity.example/token",
                "jwks_uri":"https://identity.example/jwks", "id_token_signing_alg_values_supported":["RS256"]})
        if request.url.path == "/jwks":
            return httpx.Response(200,json={"keys":[key.as_dict(private=False)]})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            verifier = form["code_verifier"][0]
            computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert computed == state["challenge"]
            claims={"iss":settings.oidc_issuer,"sub":settings.admin_subject,"aud":settings.oidc_client_id,
                "iat":int(time.time()),"exp":int(time.time())+300,"auth_time":int(time.time()),
                "nonce":state["nonce"],"acr":"urn:test:passkey", **state["claims"]}
            signing = RSAKey.generate_key(2048,parameters={"kid":"test-signing-key"}) if state["wrong_key"] else key
            encoded = jwt.encode({"alg":"RS256","kid":"test-signing-key"},claims,signing)
            return httpx.Response(200,json={"access_token":"not-retained","token_type":"Bearer","id_token":encoded})
        raise AssertionError(f"Unexpected identity request: {request.url}")
    app.state.oauth.identity.client_kwargs["transport"] = httpx.MockTransport(identity)
    client = TestClient(app,base_url=settings.public_origin)
    def login():
        start = client.get("/auth/login",follow_redirects=False)
        assert start.status_code == 302
        params = parse_qs(urlsplit(start.headers["location"]).query)
        assert params["code_challenge_method"] == ["S256"]
        state["nonce"],state["challenge"] = params["nonce"][0],params["code_challenge"][0]
        return client.get("/auth/callback", params={"code":"test-code","state":params["state"][0]},follow_redirects=False)
    return settings,store,app,client,state,login


def write_headers(client):
    return {"origin":"https://desk.example","x-csrf-token":client.get("/api/session").json()["csrf"]}


def test_closed_without_configuration():
    app=create_app(Settings(_env_file=None,database_url="sqlite+pysqlite:///:memory:"))
    client=TestClient(app,base_url="http://127.0.0.1:8765")
    assert client.get("/").status_code == 200
    for endpoint in ("/api/overview","/api/session"):
        assert client.get(endpoint).status_code == 401
    assert client.get("/auth/login").status_code == 503
    assert client.get("/docs").status_code == 404
    assert client.get("/api/overview?token=anything").status_code == 401
    assert client.get("/guide").status_code==200
    for endpoint in ("/api/onboarding","/api/contacts/hint?phone=%2B919876543210","/api/runner","/api/setup/files/Code.gs"):
        assert client.get(endpoint).status_code==401


def test_onboarding_daily_revision_and_confirmed_contact(setup_app):
    s,store,app,c,state,login=setup_app
    assert login().status_code==303
    headers=write_headers(c)
    setup={"business_name":"Test trader","owner_phone":"+919876543210","classifier_model":"account-model","sources":[{"phone":"+919876543210","label":"Supplier"},{"phone":"+971501234567","label":"Dubai"}]}
    assert c.post("/api/onboarding/setup",json={"setup":setup,"expected_revision":0}).status_code==403
    saved=c.post("/api/onboarding/setup",json={"setup":setup,"expected_revision":0},headers=headers)
    assert saved.status_code==200 and saved.json()["revision"]==1
    assert c.post("/api/onboarding/setup",json={"setup":setup,"expected_revision":0},headers=headers).status_code==409
    assert len(c.get("/api/onboarding").json()["setup"]["data"]["sources"])==2
    hint=c.get("/api/contacts/hint",params={"phone":"+14155552671"}).json()
    assert not hint["can_schedule"]
    payload={"phone":"+14155552671","label":"Verified buyer","timezone":"America/New_York","expected_revision":0}
    assert c.post("/api/contacts/confirm",json=payload,headers=headers).status_code==200
    assert c.get("/api/contacts/hint",params={"phone":payload["phone"]}).json()["can_schedule"]
    assert c.post("/api/contacts/confirm",json={**payload,"timezone":"Guess/Unknown"},headers=headers).status_code==422


def test_real_signed_oidc_pkce_login_and_cookie_rotation(setup_app):
    s,store,app,c,state,login=setup_app
    assert login().status_code == 303
    assert c.get("/api/overview").status_code == 200
    cookie=c.cookies.get(s.cookie_name)
    assert "owner" not in cookie
    with store.engine.connect() as conn:
        row=conn.execute(select(sessions).where(sessions.c.key==digest(cookie))).mappings().one()
        assert "access_token" not in json.dumps(row["data"])
        assert "id_token" not in json.dumps(row["data"])
        assert "_state" not in json.dumps(row["data"])
    assert c.get("/auth/callback?code=test-code&state=replayed").status_code == 401


@pytest.mark.parametrize("claim,value",[("iss","https://evil.example"),("sub","other-owner"),("aud","other-client"),("nonce","wrong"),("exp",1),("acr","password-only"),("auth_time",0),("auth_time",True)])
def test_bad_identity_claims_never_admit_admin(setup_app,claim,value):
    s,store,app,c,state,login=setup_app
    state["claims"]={claim:value}
    assert login().status_code in (401,403)
    assert c.get("/api/overview").status_code == 401


def test_bad_signature(setup_app):
    s,store,app,c,state,login=setup_app
    state["wrong_key"]=True
    assert login().status_code == 401
    assert c.get("/api/overview").status_code == 401


def test_mismatched_state_cannot_exchange_code(setup_app):
    s,store,app,c,state,login=setup_app
    assert c.get("/auth/login",follow_redirects=False).status_code==302
    assert c.get("/auth/callback?code=unsolicited&state=wrong").status_code==401
    assert c.get("/api/session").status_code==401


def test_idle_session_and_cookie_flags(setup_app):
    s,store,app,c,state,login=setup_app
    callback=login()
    cookie=callback.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "domain=" not in cookie
    with store.engine.begin() as conn:
        conn.execute(update(sessions).values(seen=time.time()-s.session_idle_seconds-1))
    assert c.get("/api/overview").status_code==401


def test_csrf_origin_and_type_are_independent_checks(setup_app):
    s,store,app,c,state,login=setup_app
    assert login().status_code == 303
    payload={"title":"Check stock", "request_key":"request_123456789012"}
    assert c.post("/api/tasks",json=payload).status_code == 403
    headers=write_headers(c)
    assert c.post("/api/tasks",json=payload,headers={**headers,"origin":"https://evil.example"}).status_code==403
    assert c.post("/api/tasks",json=payload,headers={**headers,"x-csrf-token":"wrong"}).status_code==403
    assert c.post("/api/tasks",content=json.dumps(payload),headers={**headers,"content-type":"text/plain"}).status_code in (415,422)


def test_task_persistence_idempotency_and_revision(setup_app):
    s,store,app,c,state,login=setup_app
    login(); headers=write_headers(c)
    goal={"title":"Check stock", "request_key":"request_123456789012"}
    a=c.post("/api/tasks",json=goal,headers=headers)
    assert a.status_code==201
    assert a.json()["state"]=="WAITING_FOR_REVIEW"
    assert c.post("/api/tasks",json=goal,headers=headers).json()["id"]==a.json()["id"]
    assert c.post("/api/tasks",json={**goal,"title":"Different goal"},headers=headers).status_code==409
    url=f"/api/tasks/{a.json()['id']}/cancel"
    assert c.post(url,json={"revision":1},headers=headers).status_code==200
    assert c.post(url,json={"revision":1},headers=headers).status_code==409
    with store.engine.connect() as conn:
        assert len(conn.execute(select(tasks)).all())==0
        assert len(conn.execute(select(audit).where(audit.c.kind=="task.created")).all())==0
    assert len([k for k in app.state.business.repository.records if k[0]=="tasks"])==1


def test_bridge_gate_cannot_be_enabled_by_dashboard(setup_app):
    s,store,app,c,state,login=setup_app
    login(); h=write_headers(c)
    assert c.post("/api/control/pause",json={"revision":1},headers=h).status_code==200
    assert c.post("/api/control/pause",json={"revision":1},headers=h).status_code==409
    assert c.post("/api/control/resume",json={},headers=h).status_code==409
    link=c.post("/api/dashboard-link",json={},headers=h).json()
    assert link["url"]=="https://desk.example/" and not link["grants_access"] and not link["sent_to_whatsapp"]


def test_revoked_cookie_cannot_be_reused(setup_app):
    s,store,app,c,state,login=setup_app
    login(); h=write_headers(c); cookie=c.cookies.get(s.cookie_name)
    assert c.post("/auth/logout",json={},headers=h).status_code==200
    c.cookies.set(s.cookie_name,cookie)
    assert c.get("/api/overview").status_code==401


def test_expired_and_policy_revoked_sessions(setup_app):
    s,store,app,c,state,login=setup_app
    login()
    with store.engine.begin() as conn:
        conn.execute(update(sessions).values(expires=1))
    assert c.get("/api/overview").status_code==401
    login(); s.admin_subject="replacement-owner"
    assert c.get("/api/overview").status_code==403


def test_host_headers_size_and_no_cache(setup_app):
    s,store,app,c,state,login=setup_app
    assert c.get("/",headers={"host":"evil.example"}).status_code==400
    assert c.post("/api/tasks",content=b"x"*17000).status_code==413
    r=c.get("/")
    assert r.headers["cache-control"]=="no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "31536000" in r.headers["strict-transport-security"]


def test_production_is_fail_closed():
    with pytest.raises(ValueError): Settings(_env_file=None,environment="production")
    with pytest.raises(ValueError): Settings(_env_file=None,public_origin="http://0.0.0.0:8765")
    with pytest.raises(ValueError): Settings(_env_file=None,oidc_issuer="https://identity.example",oidc_client_id="client",oidc_client_secret="test-only",admin_subject="owner",accepted_acr=["mfa"])


def test_native_framing_and_origin():
    stream=io.BytesIO(); write_frame(stream,{"unicode":"₹"}); stream.seek(0)
    assert read_frame(stream)=={"unicode":"₹"}
    for raw in (struct.pack("=I",MAX_FRAME+1),struct.pack("=I",12)+b"{}",struct.pack("=I",13)+b'{"a":1,"a":2}'):
        with pytest.raises((ValueError,EOFError)): read_frame(io.BytesIO(raw))
    assert check_origin("chrome-extension://"+"a"*32+"/","a"*32)
    assert not check_origin("https://web.whatsapp.com/","a"*32)


def test_decimal_quote_and_invalid_inputs():
    assert quote_usd(*(Decimal(v) for v in ("71000","85",".01",".05",".01")))==Decimal("897.11")
    with pytest.raises(ValueError): quote_usd(*(Decimal(v) for v in ("71000","0",".01",".05",".01")))

