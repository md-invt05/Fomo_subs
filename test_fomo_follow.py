import asyncio
import csv
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeout

from fomo_follow import FollowRunner, FollowStore, RetryJob, follow_one, load_env_file, profile_url


class StoreTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent
        ident = uuid4().hex
        self.source = root / f"test_tmp_{ident}.csv"
        self.db = root / f"test_tmp_{ident}.sqlite3"
        self.store = FollowStore(self.db)

    def tearDown(self):
        self.store.close()
        self.source.unlink(missing_ok=True)
        self.db.unlink(missing_ok=True)

    def snapshot(self, rows):
        with self.source.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(("userId", "handle"))
            writer.writerows(rows)
        return self.store.sync_snapshot(self.source, "ArmedSlimPony")

    def job(self, user_id):
        return self.store.db.execute("SELECT * FROM follow_jobs WHERE user_id=?", (user_id,)).fetchone()

    def test_every_profile_is_queued_and_duplicate_handle_is_held(self):
        self.assertEqual(self.snapshot([
            ("self", "ArmedSlimPony"), ("a", "noTrades"), ("b", "same"), ("c", "SAME")
        ]), 4)
        self.assertEqual(self.job("self")["status"], "skipped_self")
        self.assertEqual(self.job("a")["status"], "pending")
        self.assertEqual(self.job("b")["status"], "ambiguous_handle")
        self.assertEqual(self.job("c")["status"], "ambiguous_handle")
        self.assertEqual(self.store.claim()["user_id"], "a")

    def test_env_file_keeps_explicit_process_values(self):
        path = self.source.with_suffix(".env")
        path.write_text("# settings\nFOLLOW_WORKERS=2\nFOLLOW_RPM=7 # comment\nFOLLOW_HEADLESS='0'\n",
                        encoding="utf-8")
        try:
            with patch.dict(os.environ, {"FOLLOW_WORKERS": "9"}, clear=True):
                load_env_file(path)
                self.assertEqual(os.environ["FOLLOW_WORKERS"], "9")
                self.assertEqual(os.environ["FOLLOW_RPM"], "7")
                self.assertEqual(os.environ["FOLLOW_HEADLESS"], "0")
        finally:
            path.unlink(missing_ok=True)

    def test_rename_preserves_success_and_resolved_collision_requeues(self):
        self.snapshot([("a", "old"), ("b", "same"), ("c", "SAME")])
        self.store.mark("a", "followed")
        self.store.mark("c", "unavailable")
        self.snapshot([("a", "new"), ("b", "same"), ("c", "different")])
        self.assertEqual((self.job("a")["handle"], self.job("a")["status"]), ("new", "followed"))
        self.assertEqual(self.job("b")["status"], "pending")
        self.assertEqual(self.job("c")["status"], "pending")

    def test_running_job_is_rechecked_after_restart_but_status_does_not_reset_it(self):
        self.snapshot([("a", "alice")])
        self.assertEqual(self.store.claim()["user_id"], "a")
        status_reader = FollowStore(self.db, recover_running=False)
        try:
            self.assertEqual(status_reader.counts()["running"], 1)
        finally:
            status_reader.close()
        self.store.close()
        self.store = FollowStore(self.db)
        self.assertEqual(self.job("a")["status"], "retry")
        self.assertEqual(self.store.claim()["user_id"], "a")


class FakeLocator:
    def __init__(self, page, label):
        self.page, self.label = page, label

    @property
    def first(self):
        return self

    async def count(self):
        if self.label == "Follow":
            return int(self.page.state == "follow")
        if self.label == "Following":
            return int(self.page.state == "following")
        if self.label == "handle":
            return int(not self.page.missing_handle)
        return 0 if self.label in ("login", "captcha") else 1

    async def is_visible(self):
        return bool(await self.count())

    async def wait_for(self, **_):
        if not await self.count():
            raise PlaywrightTimeout("not visible")

    async def click(self):
        self.page.clicks += 1
        if self.page.confirm_immediately:
            self.page.state = "following"


class FakePage:
    def __init__(self, state="follow", confirm_immediately=True, confirm_after_reload=False,
                 missing_handle=False):
        self.state = state
        self.confirm_immediately = confirm_immediately
        self.confirm_after_reload = confirm_after_reload
        self.missing_handle = missing_handle
        self.clicks = 0
        self.visits = 0
        self.url = ""

    async def goto(self, url, **_):
        self.visits += 1
        self.url = url
        if self.visits > 1 and self.confirm_after_reload:
            self.state = "following"
        return None

    def get_by_role(self, _role, name=None, **_):
        return FakeLocator(self, name if isinstance(name, str) else "login")

    def get_by_text(self, *_args, **_kwargs):
        return FakeLocator(self, "handle")

    def locator(self, *_args, **_kwargs):
        return FakeLocator(self, "captcha")


class BrowserLogicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = {"handle": "MBDAN"}
        self.settle = patch("fomo_subs.browser.STATE_SETTLE_SECONDS", 0)
        self.settle.start()

    def tearDown(self):
        self.settle.stop()

    async def test_success_is_rechecked_after_reload(self):
        page = FakePage()
        self.assertEqual(await follow_one(page, self.row, "ArmedSlimPony", asyncio.Event()),
                         "followed")
        self.assertEqual((page.clicks, page.visits), (1, 2))

    async def test_already_following_never_clicks(self):
        page = FakePage(state="following")
        self.assertEqual(await follow_one(page, self.row, "ArmedSlimPony", asyncio.Event()),
                         "already_following")
        self.assertEqual(page.clicks, 0)

    async def test_delayed_acknowledgement_rechecks_instead_of_clicking_twice(self):
        page = FakePage(confirm_immediately=False, confirm_after_reload=True)
        self.assertEqual(await follow_one(page, self.row, "ArmedSlimPony", asyncio.Event()),
                         "followed")
        self.assertEqual((page.clicks, page.visits), (1, 2))

    async def test_unconfirmed_click_is_retried_on_next_visit(self):
        page = FakePage(confirm_immediately=False)
        with self.assertRaises(RetryJob):
            await follow_one(page, self.row, "ArmedSlimPony", asyncio.Event())
        self.assertEqual(page.clicks, 1)

    async def test_slow_profile_is_retried_without_click(self):
        page = FakePage(missing_handle=True)
        with self.assertRaises(RetryJob):
            await follow_one(page, self.row, "ArmedSlimPony", asyncio.Event())
        self.assertEqual(page.clicks, 0)

    def test_referral_is_not_in_profile_url(self):
        self.assertEqual(profile_url("MBDAN"), "https://fomo.family/profile/MBDAN")

    async def test_background_stop_request_is_observed(self):
        stop_path = Path(__file__).parent / f"test_tmp_{uuid4().hex}.stop"
        runner = FollowRunner(None, "unused.csv", "unused-profile", "ArmedSlimPony",
                              2, 10, None, stop_path)
        task = asyncio.create_task(runner.stop_monitor())
        try:
            stop_path.touch()
            await asyncio.wait_for(runner.stop.wait(), timeout=3)
            await task
        finally:
            task.cancel()
            stop_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
