from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: str = "data/monitor.db"
    poll_interval_hours: int = Field(default=8, ge=1)
    scheduler_run_immediately: bool = True
    port: int = 8080
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8080"

    slack_bot_token: str | None = None
    slack_channel_id: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_signing_secret: str | None = None

    twitterapi_io_api_key: str | None = None
    twitter_max_pages: int = Field(default=3, ge=1, le=10)
    twitter_lookback_days: int = Field(default=7, ge=1, le=30)
    twitter_current_batches: str = "F26,W27,S27"

    yc_latest_changes_url: str = "https://yc-oss.github.io/api/changes/latest.json"
    yc_official_alert_max_age_days: int = Field(default=7, ge=1, le=30)

    apify_api_token: str | None = None
    linkedin_total_posts: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Cycle-wide LinkedIn post budget, allocated across HarvestAPI search queries",
    )
    linkedin_actor_id: str = "buIWk2uOUzTmcLsuB"
    linkedin_actor_build_id: str = "ASBzmjLXGQlvadkLr"

    yc_speedrun_url: str | None = "https://speedrun-api.a16z.com/api/companies/companies/"
    pond_access_key: str | None = None

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_timeout_seconds: float = Field(default=20, gt=0, le=120)
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    openai_max_concurrency: int = Field(default=4, ge=1, le=20)
    openai_min_confidence: float = Field(default=0.65, ge=0, le=1)
    openai_review_min_confidence: float = Field(default=0.65, ge=0, le=1)
    openai_immediate_min_confidence: float = Field(default=0.9, ge=0, le=1)
    openai_max_calls_per_cycle: int = Field(default=25, ge=0, le=200)
    openai_max_calls_per_day: int = Field(default=100, ge=0, le=2000)
    max_twitter_pages_per_day: int = Field(default=48, ge=0, le=500)
    max_linkedin_posts_per_day: int = Field(default=150, ge=0, le=2000)
    slack_ops_channel_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
