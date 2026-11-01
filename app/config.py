from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    openrouter_api_key: SecretStr
    # Comma-separated: primary first, then fallbacks (OpenRouter's `models` routing).
    openrouter_models: str = "google/gemma-4-31b-it:free,nvidia/nemotron-3.5-lightning:free"
    openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    app_url: str = "https://github.com/Sourav692/dar-wasalt-chat"  # sent as HTTP-Referer
    app_title: str = "Dar Global & Wasalt Assistant"
    # Reasoning tokens count against max_tokens. Nemotron spent 1,466 of 1,500 on reasoning at
    # effort=low and cut the answer off; disabling reasoning made it hallucinate instead.
    max_tokens: int = 4000
    temperature: float = 0.2
    llm_timeout_s: float = 30.0  # per read; OpenRouter keep-alives reset it
    llm_deadline_s: float = 50.0  # whole answer, inside Cloud Run's 60 s request timeout
    index_dir: Path = ROOT / "index"
    access_passcode: SecretStr | None = None
    rate_per_minute: int = 10
    rate_per_day: int = 100
    # Whole service: OpenRouter allows 1,000 free-model requests a day on an account with credit.
    global_per_minute: int = 60
    global_per_day: int = 1000
    # Wrong passcodes across all clients; past this, codes are not even compared.
    auth_fail_per_minute: int = 30
    auth_fail_per_day: int = 500
    # Proxies that append to X-Forwarded-For: 1 for Cloud Run's front end.
    trusted_proxy_hops: int = 1
    log_salt: SecretStr | None = None

    @field_validator("access_passcode")
    @classmethod
    def passcode_is_long(cls, value: SecretStr | None) -> SecretStr | None:
        # Correct codes are never locked out (that would hand attackers a denial of service),
        # so guessing must be infeasible instead: 20 random characters is ~119 bits.
        if value is not None and len(value.get_secret_value()) < 20:
            raise ValueError("ACCESS_PASSCODE must be at least 20 characters; use a random one")
        return value

    @property
    def models(self) -> list[str]:
        return [m.strip() for m in self.openrouter_models.split(",") if m.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
