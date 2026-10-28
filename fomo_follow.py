"""Compatibility entry point: python fomo_follow.py login|run|status|stop."""

from fomo_subs.browser import RetryJob, follow_one, profile_url
from fomo_subs.cli import main
from fomo_subs.config import load_env_file
from fomo_subs.service import FollowRunner
from fomo_subs.storage import FollowStore


if __name__ == "__main__":
    main()
