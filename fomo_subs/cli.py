"""Command-line interface for the FOMO follow service."""

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
from pathlib import Path

from .browser import login
from .config import load_env_file
from .errors import PauseRun
from .service import FollowRunner
from .storage import FollowStore, SingleInstance


LOG = logging.getLogger("fomo_follow")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    try:
        load_env_file(Path(__file__).resolve().parent.parent / ".env")
    except ValueError as exc:
        parser.error(str(exc))
    parser.add_argument("command", choices=("login", "run", "status", "stop"))
    parser.add_argument("--source", default=os.getenv("FOLLOW_SOURCE", "data/all_profiles.csv"))
    parser.add_argument("--db", default=os.getenv("FOLLOW_DB", "follow_data/follow_state.sqlite3"))
    parser.add_argument("--profile-dir", default=os.getenv("FOLLOW_PROFILE_DIR", "follow_data/chrome_profile"))
    parser.add_argument("--account", default=os.getenv("FOLLOW_ACCOUNT", "ArmedSlimPony"))
    parser.add_argument("--workers", type=int, default=int(os.getenv("FOLLOW_WORKERS", "2")))
    parser.add_argument("--rpm", type=int, default=int(os.getenv("FOLLOW_RPM", "10")))
    max_profiles = os.getenv("FOLLOW_MAX_PROFILES", "").strip()
    parser.add_argument("--max-profiles", type=int,
                        default=int(max_profiles) if max_profiles else None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction,
                        default=os.getenv("FOLLOW_HEADLESS", "0") == "1",
                        help="Use the saved Chrome profile without a visible window")
    args = parser.parse_args()
    if args.workers < 1 or args.rpm < 1 or (args.max_profiles is not None and args.max_profiles < 1):
        parser.error("workers, rpm and max-profiles must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "login":
            with SingleInstance(Path(args.db).with_suffix(".lock")):
                login(args.profile_dir, args.account)
            return
        if args.command == "stop":
            stop_path = Path(args.db).with_suffix(".stop")
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.touch()
            print("Stop requested; active browser actions will finish first")
            return
        if args.command == "status":
            if not Path(args.db).exists():
                print("No follow jobs yet")
                return
            db_uri = Path(args.db).resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(db_uri, uri=True)
            try:
                counts = dict(db.execute("SELECT status,count(*) FROM follow_jobs GROUP BY status"))
                print(" ".join(f"{key}={value}" for key, value in sorted(counts.items())))
            finally:
                db.close()
            return
        with SingleInstance(Path(args.db).with_suffix(".lock")):
            store = FollowStore(args.db)
            try:
                runner = FollowRunner(store, args.source, args.profile_dir, args.account,
                                      args.workers, args.rpm, args.max_profiles,
                                      Path(args.db).with_suffix(".stop"), args.headless)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        try:
                            loop.add_signal_handler(sig, runner.stop.set)
                        except (NotImplementedError, RuntimeError):
                            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(runner.stop.set))
                    loop.run_until_complete(runner.run())
                finally:
                    loop.close()
            finally:
                store.close()
    except (PauseRun, OSError, ValueError, RuntimeError) as exc:
        LOG.error("Stopped: %s", exc)
        raise SystemExit(2) from None
