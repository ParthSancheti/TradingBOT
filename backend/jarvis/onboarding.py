import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Source(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)
    phone: str
    label: str=Field(default="",max_length=80)
    purpose: Literal["inventory","buyer","both"]="inventory"
    active: bool=True

    @field_validator("phone")
    @classmethod
    def normalize(cls,value):
        if not value.startswith("+"): raise ValueError("Use international format, including +country code")
        import phonenumbers
        try: number=phonenumbers.parse(value,None)
        except phonenumbers.NumberParseException: raise ValueError("Invalid international phone number") from None
        if not phonenumbers.is_possible_number(number): raise ValueError("Invalid phone number length")
        return phonenumbers.format_number(number,phonenumbers.PhoneNumberFormat.E164)


class BusinessSetup(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)
    business_name: str=Field(min_length=2,max_length=100)
    sources: list[Source]=Field(min_length=1,max_length=100)
    owner_phone: str
    database_phone: str | None = None
    timezone: str="Asia/Kolkata"
    base_currency: str=Field(pattern=r"^[A-Z]{3}$",default="INR")
    margin_pct: float=Field(ge=0,le=60,default=5)
    quiet_start: str=Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",default="21:00")
    quiet_end: str=Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",default="09:00")
    poll_seconds: Literal[60]=60
    classifier_model: str=Field(min_length=1,max_length=100)
    classifier_daily_limit: int=Field(default=200,ge=1,le=10000)
    reply_mode: Literal["observe","draft","approved_auto"]="draft"
    scan_all: bool=True
    lead_capture: bool=True

    @field_validator("owner_phone")
    @classmethod
    def owner(cls,value): return Source.normalize(value)

    @field_validator("database_phone")
    @classmethod
    def database_sender(cls, value):
        return Source.normalize(value) if value else None

    @field_validator("timezone")
    @classmethod
    def zone(cls,value):
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError: raise ValueError("Use an IANA timezone") from None
        return value

    @field_validator("sources")
    @classmethod
    def unique_sources(cls,sources):
        if len({s.phone for s in sources})!=len(sources): raise ValueError("Duplicate source number")
        return sources


def contact_hint(phone, confirmed=None):
    normalized=Source.normalize(phone)
    if confirmed and confirmed.get("phone")==normalized and confirmed.get("timezone_confirmed") is True:
        ZoneInfo(confirmed["timezone"])
        return {"phone":normalized,"country_hint":confirmed.get("country"),"timezone":confirmed["timezone"],"source":"confirmed_contact","can_schedule":True}
    import phonenumbers
    number=phonenumbers.parse(normalized,None)
    return {"phone":normalized,"country_hint":phonenumbers.region_code_for_number(number),"timezone":None,"source":"number_prefix_hint","can_schedule":False}
