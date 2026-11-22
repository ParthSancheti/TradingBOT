import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .onboarding import BusinessSetup,Source,contact_hint
from .security import require_admin,require_write
from .sheets import AppsScript,SheetsError,validate_script_url
from .providers import GrokClassifier,MarketData
from .store import add_audit

router=APIRouter()


@router.get("/api/intake")
async def intake(request:Request):
    require_admin(request)
    repo=request.app.state.business.repository
    return {"messages":await repo.list("messages",20),"limit":20,"sending_enabled":False}


@router.get("/api/market")
async def market(request:Request):
    require_admin(request)
    repo=request.app.state.business.repository
    return {"fx":await repo.get("fx","latest"),"news":await repo.get("news","latest"),"automatic_price_changes":False}

@router.get("/api/setup/files/{name}")
async def setup_file(name:str,request:Request):
    from pathlib import Path
    from fastapi.responses import FileResponse
    require_admin(request)
    if name not in ("Code.gs","Core.js","appsscript.json"): raise HTTPException(404)
    return FileResponse(Path(__file__).resolve().parents[2]/"apps-script"/name,filename=name)


class Connection(BaseModel):
    model_config=ConfigDict(extra="forbid")
    script_url: str
    signing_secret: SecretStr=Field(min_length=32,max_length=1024)
    xai_key: SecretStr=Field(default=SecretStr(""),max_length=1024)
    fx_key: SecretStr=Field(default=SecretStr(""),max_length=1024)
    news_key: SecretStr=Field(default=SecretStr(""),max_length=1024)


class SetupSave(BaseModel):
    model_config=ConfigDict(extra="forbid")
    setup: BusinessSetup
    expected_revision: int=Field(ge=0)


class ContactSave(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)
    phone: str
    label: str=Field(min_length=1,max_length=100)
    timezone: str
    expected_revision: int=Field(ge=0)

    @field_validator("phone")
    @classmethod
    def phone_format(cls,value): return Source.normalize(value)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls,value): return BusinessSetup.zone(value)


@router.get("/api/onboarding")
async def status(request:Request):
    require_admin(request)
    try:
        health=await request.app.state.business.repository.health()
        record=await request.app.state.business.repository.get("configuration","business")
        return {"sheets_connected":True,"setup":record,"health":health,"poll_seconds":60}
    except (SheetsError,RuntimeError):
        return {"sheets_connected":False,"setup":None,"poll_seconds":60}


@router.post("/api/onboarding/connect")
async def connect(value:Connection,request:Request):
    p=require_write(request,fresh=True)
    try:
        client=AppsScript(validate_script_url(value.script_url),value.signing_secret.get_secret_value())
        result=await client.call("health")
        if result.get("schema_version")!=1 or result.get("storage")!="google_sheets": raise ValueError("Unsupported workbook protocol")
        vault=request.app.state.vault
        import json
        vault.set("sheets_connection",json.dumps({"url":value.script_url,"secret":value.signing_secret.get_secret_value()}))
        for name in ("xai_key","fx_key","news_key"):
            secret=getattr(value,name).get_secret_value()
            if secret: vault.set(name,secret)
    except Exception:
        raise HTTPException(400,"Connection could not be verified or OS secret storage is unavailable") from None
    with request.app.state.store.engine.begin() as c: add_audit(c,p["subject"],"sheets.connection_verified")
    return {"connected":True,"secrets_stored_in":"OS credential store","health":result}


@router.post("/api/onboarding/setup")
async def save(value:SetupSave,request:Request):
    require_write(request,fresh=True)
    repo=request.app.state.business.repository
    record=await repo.put("configuration","business",value.setup.model_dump(),value.expected_revision)
    if not await repo.get("configuration","control"): await repo.put("configuration","control",{"paused":True},0)
    return record


@router.get("/api/contacts/hint")
async def hint(phone:str,request:Request):
    require_admin(request)
    try:
        record=await request.app.state.business.repository.get("contacts",phone)
        return {**contact_hint(phone,record["data"] if record else None),"revision":record["revision"] if record else 0}
    except ValueError: raise HTTPException(400,"Use a valid international phone number") from None


@router.post("/api/contacts/confirm")
async def confirm_contact(value:ContactSave,request:Request):
    principal=require_write(request,fresh=True)
    return await request.app.state.business.repository.put("contacts",value.phone,{"phone":value.phone,"label":value.label,"timezone":value.timezone,"timezone_confirmed":True,"confirmed_by":principal["subject"],"confirmed_at":time.time()},value.expected_revision)


@router.post("/api/market/refresh")
async def refresh(request:Request):
    require_write(request)
    vault=request.app.state.vault; repo=request.app.state.business.repository
    provider=MarketData(vault.get("fx_key"),vault.get("news_key"))
    result={}
    for name,call in (("fx",provider.fx),("news",provider.news)):
        try:
            value=await call()
            payload=value.model_dump(mode="json") if name=="fx" else {"articles":value}
            current=await repo.get(name,"latest")
            await repo.put(name,"latest",payload,current["revision"] if current else 0)
            result[name]="updated"
        except Exception: result[name]="unavailable_or_not_entitled"
    return result
