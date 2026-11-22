from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file=".env", extra="ignore")

    environment: str = "development"
    public_origin: str = "http://127.0.0.1:8765"
    database_url: str = "sqlite+pysqlite:///.runtime/control.sqlite3"
    database_auto_create: bool = True
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    admin_subject: str = ""
    accepted_acr: list[str] = []
    session_absolute_seconds: int = 3600
    session_idle_seconds: int = 900
    fresh_auth_seconds: int = 300

    @model_validator(mode="after")
    def validate_deployment(self):
        u = urlsplit(self.public_origin)
        if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise ValueError("public_origin must be an origin without credentials or path")
        if u.scheme not in ("https", "http") or not u.hostname:
            raise ValueError("Invalid public origin")
        if u.scheme == "http" and (self.environment != "development" or u.hostname not in ("127.0.0.1", "localhost", "::1")):
            raise ValueError("HTTP is permitted only on a development loopback origin")
        self.public_origin = self.public_origin.rstrip("/")
        if self.environment not in ("development", "production"):
            raise ValueError("Unknown environment")
        if self.oidc_issuer:
            i = urlsplit(self.oidc_issuer)
            if i.scheme != "https" or not i.hostname or i.query or i.fragment or i.username or i.password:
                raise ValueError("OIDC issuer must use HTTPS")
        if min(self.session_absolute_seconds, self.session_idle_seconds, self.fresh_auth_seconds) <= 0:
            raise ValueError("Session durations must be positive")
        if self.environment == "production":
            if self.database_auto_create:
                raise ValueError("Production needs explicit security-state schema initialization")
            if not self.auth_configured:
                raise ValueError("Production requires pinned identity and assurance policy")
        if self.auth_configured and not self.secure_cookie:
            raise ValueError("Administrator authentication requires HTTPS, including development")
        return self

    @property
    def auth_configured(self):
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret.get_secret_value() and self.admin_subject and self.accepted_acr)

    @property
    def secure_cookie(self):
        return self.public_origin.startswith("https://")

    @property
    def cookie_name(self):
        return "__Host-jarvis" if self.secure_cookie else "jarvis-local"

