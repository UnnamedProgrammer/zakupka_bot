from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    files_dir: str = "/app/data/files"
    approval_override_username: str | None = None
    approval_override_tg_id: int | None = None

    @field_validator("approval_override_tg_id", mode="before")
    @classmethod
    def _parse_optional_int(cls, value):
        if value in ("", None):
            return None
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
