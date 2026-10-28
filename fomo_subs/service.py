"""Continuous queue processing with bounded browser concurrency."""

import asyncio
import logging
import time
from collections import deque
from pathlib import Path

from .browser import follow_one, verify_account
from .errors import PauseRun, RetryJob, UnavailableJob


LOG = logging.getLogger("fomo_follow")


class VisitLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.times = deque()
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while len(self.times) >= self.per_minute:
                now = time.monotonic()
                if now - self.times[0] >= 60:
                    self.times.popleft()
                else:
                    await asyncio.sleep(60 - (now - self.times[0]))
            self.times.append(time.monotonic())


class FollowRunner:
    def __init__(self, store, source, profile_dir, account, workers, rpm, max_profiles,
                 stop_path=None, headless=False):
        self.store, self.source, self.profile_dir = store, Path(source), Path(profile_dir)
        self.stop_path = Path(stop_path) if stop_path else None
        self.account, self.workers, self.max_profiles = account, workers, max_profiles
        self.headless = headless
        self.limiter = VisitLimiter(rpm)
        self.stop = asyncio.Event()
        self.claimed = 0
        self.pause_reason = None

    async def sync_loop(self):
        while not self.stop.is_set():
            try:
                self.store.sync_snapshot(self.source, self.account)
            except (OSError, ValueError) as exc:
                self.pause_reason = f"Profile export is unavailable: {exc}"
                self.stop.set()
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def stop_monitor(self):
        while not self.stop.is_set():
            if self.stop_path and self.stop_path.exists():
                LOG.info("Graceful stop requested")
                self.stop.set()
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def worker(self, context):
        page = await context.new_page()
        try:
            while not self.stop.is_set():
                if self.max_profiles is not None and self.claimed >= self.max_profiles:
                    return
                row = self.store.claim()
                if row is None:
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self.claimed += 1
                try:
                    await self.limiter.wait()
                    if self.stop.is_set():
                        self.store.mark(row["user_id"], "retry", "Stopped before visit")
                        return
                    result = await follow_one(page, row, self.account, self.stop)
                    self.store.mark(row["user_id"], result)
                    LOG.info("%s %s (%s)", result, row["handle"], row["user_id"])
                except PauseRun as exc:
                    self.store.mark(row["user_id"], "retry", str(exc)[:200])
                    self.pause_reason = str(exc)
                    self.stop.set()
                    return
                except RetryJob as exc:
                    self.store.retry(row, exc)
                    LOG.warning("Retry later: %s (%s)", row["handle"], type(exc).__name__)
                except UnavailableJob as exc:
                    self.store.mark(row["user_id"], "unavailable", str(exc)[:200])
                    LOG.info("Unavailable profile: %s (%s)", row["handle"], row["user_id"])
                except Exception as exc:
                    self.store.retry(row, type(exc).__name__)
                    self.pause_reason = f"Unexpected browser error: {type(exc).__name__}"
                    self.stop.set()
                    return
        finally:
            await page.close()

    async def run(self):
        from playwright.async_api import async_playwright

        if self.stop_path:
            self.stop_path.unlink(missing_ok=True)
        total = self.store.sync_snapshot(self.source, self.account)
        LOG.info("Loaded %s profiles; starting %s tabs; headless=%s",
                 total, self.workers, self.headless)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(self.profile_dir.resolve()), channel="chrome", headless=self.headless,
                accept_downloads=False)
            try:
                auth_page = await context.new_page()
                try:
                    await verify_account(auth_page, self.account)
                finally:
                    await auth_page.close()
                scanner = asyncio.create_task(self.sync_loop())
                monitor = asyncio.create_task(self.stop_monitor())
                tasks = [asyncio.create_task(self.worker(context)) for _ in range(self.workers)]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    self.stop.set()
                    scanner.cancel()
                    monitor.cancel()
                    await asyncio.gather(scanner, monitor, return_exceptions=True)
            finally:
                await context.close()
                if self.stop_path:
                    self.stop_path.unlink(missing_ok=True)
        if self.pause_reason:
            raise PauseRun(self.pause_reason)
        LOG.info("Stopped; processed %s profiles this run", self.claimed)
