from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    telegram_bot_token: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    owner_telegram_id: int = 0
    allowed_telegram_ids: str = ""
    openai_model: str = "gpt-6-luna"
    transcription_model: str = "gpt-transcribe"
    timezone: str = "Europe/Moscow"
    database_url: str = "sqlite:///data/training.sqlite3"
    daily_token_limit: int = Field(default=150000, ge=1000)
    max_voice_seconds: int = Field(default=300, ge=1, le=600)
    daily_audio_seconds: int = Field(default=1800, ge=1, le=86400)
    google_spreadsheet_id: str = ""
    google_sheet_tab_id: int = Field(default=0, ge=0)
    google_credentials_file: Path = Path("google-service-account.json")
    google_sync_interval_seconds: int = Field(default=60, ge=15, le=3600)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def account(self, telegram_id: int) -> str | None:
        if self.owner_telegram_id > 0 and telegram_id == self.owner_telegram_id:
            return "owner"
        allowed = {int(v.strip()) for v in self.allowed_telegram_ids.split(",") if v.strip()}
        return f"telegram:{telegram_id}" if telegram_id in allowed else None

    def require_secrets(self) -> None:
        missing = [name for name in ("telegram_bot_token", "openai_api_key")
                   if not getattr(self, name).get_secret_value()]
        if missing:
            raise ValueError("Заполните .env: " + ", ".join(n.upper() for n in missing))


def secure_local_files() -> None:
    for path in (Path(".env"), Path("data")):
        if path.exists():
            path.chmod(0o700 if path.is_dir() else 0o600)
