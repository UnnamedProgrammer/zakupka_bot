from urllib.parse import quote_plus

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str | None = None
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    files_dir: str = "/app/data/files"
    approval_override_username: str | None = None
    approval_override_tg_id: int | None = None

    @field_validator("approval_override_tg_id", mode="before")
    @classmethod
    def _parse_optional_int(cls, value):
        if value in ("", None):
            return None
        return value

    @model_validator(mode="after")
    def _ensure_database_url(self):
        if self.database_url:
            return self

        missing = [
            name
            for name, value in (
                ("POSTGRES_DB", self.postgres_db),
                ("POSTGRES_USER", self.postgres_user),
                ("POSTGRES_PASSWORD", self.postgres_password),
                ("POSTGRES_HOST", self.postgres_host),
                ("POSTGRES_PORT", self.postgres_port),
            )
            if value in ("", None)
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(
                f"Set DATABASE_URL or provide PostgreSQL variables in .env: {joined}."
            )

        user = quote_plus(str(self.postgres_user))
        password = quote_plus(str(self.postgres_password))
        host = str(self.postgres_host)
        port = int(self.postgres_port)
        db_name = quote_plus(str(self.postgres_db))
        self.database_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
