from yc_monitor.adapters.base import SourceAdapter
from yc_monitor.adapters.linkedin import LinkedInAdapter
from yc_monitor.adapters.twitter import TwitterAdapter
from yc_monitor.adapters.yc_directory import YCDirectoryAdapter
from yc_monitor.adapters.yc_launches import YCLaunchesAdapter
from yc_monitor.adapters.yc_speedrun import YCSpeedrunAdapter
from yc_monitor.config import Settings


def build_adapters(settings: Settings) -> tuple[list[SourceAdapter], list[SourceAdapter]]:
    official: list[SourceAdapter] = [
        YCDirectoryAdapter(settings.yc_latest_changes_url),
        YCSpeedrunAdapter(settings.yc_speedrun_url),
        YCLaunchesAdapter(),
    ]
    social: list[SourceAdapter] = [
        TwitterAdapter(
            settings.twitterapi_io_api_key,
            settings.twitter_max_pages,
            settings.twitter_lookback_days,
            settings.twitter_current_batches,
        ),
        LinkedInAdapter(
            settings.apify_api_token,
            settings.linkedin_total_posts,
            settings.linkedin_actor_id,
            settings.linkedin_actor_build_id,
        ),
    ]
    return official, social
