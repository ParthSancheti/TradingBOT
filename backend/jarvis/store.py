import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path

from sqlalchemy import JSON, Boolean, Column, Float, Integer, MetaData, String, Table, create_engine, delete, insert, select, update
from sqlalchemy.pool import StaticPool


metadata = MetaData()
sessions = Table("admin_sessions", metadata,
    Column("key", String(64), primary_key=True), Column("data", JSON, nullable=False),
    Column("created", Float, nullable=False), Column("seen", Float, nullable=False),
    Column("expires", Float, nullable=False), Column("version", Integer, nullable=False, default=1))
control = Table("system_control", metadata,
    Column("id", Integer, primary_key=True), Column("paused", Boolean, nullable=False), Column("revision", Integer, nullable=False))
tasks = Table("owner_tasks", metadata,
    Column("id", String(36), primary_key=True), Column("title", String(2000), nullable=False),
    Column("state", String(40), nullable=False), Column("revision", Integer, nullable=False),
    Column("created", Float, nullable=False), Column("principal", String(255), nullable=False),
    Column("request_key", String(80), nullable=False, unique=True))
audit = Table("audit_events", metadata,
    Column("id", String(36), primary_key=True), Column("time", Float, nullable=False),
    Column("actor", String(255), nullable=False), Column("kind", String(80), nullable=False),
    Column("reference", String(80), nullable=False))
rate_limits = Table("rate_limits", metadata, Column("key", String(64), primary_key=True), Column("count", Integer, nullable=False))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def add_audit(connection, actor, kind, reference=""):
    connection.execute(insert(audit).values(id=str(uuid.uuid4()), time=time.time(), actor=actor, kind=kind, reference=reference))


class Store:
    def __init__(self, url):
        if url.startswith("sqlite"):
            Path(".runtime").mkdir(exist_ok=True)
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        if url.endswith(":memory:"):
            options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)

    def initialize(self):
        metadata.create_all(self.engine)
        with self.engine.begin() as c:
            if not c.execute(select(control)).first():
                c.execute(insert(control).values(id=1, paused=True, revision=1))

    def session(self, token, idle):
        if not token or len(token) > 128:
            return None
        with self.engine.begin() as c:
            row = c.execute(select(sessions).where(sessions.c.key == digest(token))).mappings().first()
            if not row:
                return None
            now = time.time()
            if now >= row["expires"] or now - row["seen"] >= idle:
                c.execute(delete(sessions).where(sessions.c.key == row["key"]))
                return None
            c.execute(update(sessions).where(sessions.c.key == row["key"]).values(seen=now))
            return dict(row)

    def new_session(self, data, lifetime):
        raw = secrets.token_urlsafe(32)
        now = time.time()
        with self.engine.begin() as c:
            c.execute(insert(sessions).values(key=digest(raw), data=data, created=now, seen=now, expires=now+lifetime, version=1))
        return raw

    def save_session(self, key, data, version):
        with self.engine.begin() as c:
            return c.execute(update(sessions).where(sessions.c.key == key, sessions.c.version == version).values(data=data, version=version+1)).rowcount == 1

    def revoke(self, key):
        with self.engine.begin() as c:
            c.execute(delete(sessions).where(sessions.c.key == key))

    def permit(self, identity, action, limit, seconds=60):
        from sqlalchemy.exc import IntegrityError
        bucket = int(time.time()) // seconds
        key = digest(f"{identity}:{action}:{bucket}")
        try:
            with self.engine.begin() as c:
                c.execute(insert(rate_limits).values(key=key, count=0))
        except IntegrityError:
            pass
        with self.engine.begin() as c:
            return c.execute(update(rate_limits).where(rate_limits.c.key == key, rate_limits.c.count < limit).values(count=rate_limits.c.count+1)).rowcount == 1

