"""Chrome session and FOMO profile interactions."""

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .errors import PauseRun, RetryJob, UnavailableJob


LOG = logging.getLogger("fomo_follow")
ORIGIN = "https://fomo.family"
STATE_SETTLE_SECONDS = 5


def chrome_executable():
    configured = os.getenv("CHROME_PATH")
    if configured:
        candidates = [configured]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
        ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Chrome not found; set CHROME_PATH")


def profile_url(handle):
    return f"{ORIGIN}/profile/{quote(handle, safe='')}"


def login(profile_dir, self_handle):
    profile_dir = Path(profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome_executable(), f"--user-data-dir={profile_dir}",
                      "--new-window", profile_url(self_handle)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Войдите в FOMO через Google в открывшемся отдельном окне Chrome.")
    print("Затем закройте все окна этого отдельного профиля и нажмите Enter здесь.")
    input()
    print("Сессия Chrome сохранена. Команда run проверит аккаунт перед подписками.")


async def verify_account(page, self_handle):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    await page.goto(profile_url(self_handle), wait_until="domcontentloaded", timeout=35000)
    try:
        await page.get_by_text(re.compile(rf"^@{re.escape(self_handle)}$", re.I)).first.wait_for(timeout=15000)
    except PlaywrightTimeout as exc:
        try:
            body = await page.locator("body").inner_text(timeout=5000)
        except PlaywrightTimeout:
            body = ""
        if "couldn’t load your account" in body.casefold() or "couldn't load your account" in body.casefold():
            raise PauseRun("FOMO showed 'Couldn't load your account'; try visible Chrome") from exc
        raise PauseRun("Own profile did not load; check the saved login") from exc
    await asyncio.sleep(STATE_SETTLE_SECONDS)
    if await login_prompt_visible_async(page) or await captcha_visible_async(page):
        raise PauseRun("Login or manual verification is required")
    follow = page.get_by_role("button", name="Follow", exact=True)
    following = page.get_by_role("button", name="Following", exact=True)
    if await follow.count() or await following.count():
        raise PauseRun("Chrome is not signed in as the expected FOMO account")
    edit = page.get_by_role("button", name=re.compile(r"^edit( profile)?$", re.I))
    if not await edit.count():
        raise PauseRun("Could not confirm the account from its own profile; inspect the UI")
    LOG.info("Confirmed FOMO account: %s", self_handle)


async def login_prompt_visible_async(page):
    return await page.get_by_role("button", name=re.compile(r"^(log in|sign in|войти)$", re.I)).count() > 0


async def captcha_visible_async(page):
    return await page.locator("iframe[src*='captcha' i], iframe[title*='captcha' i]").count() > 0


async def button_state(page):
    follow = page.get_by_role("button", name="Follow", exact=True)
    following = page.get_by_role("button", name="Following", exact=True)
    if await following.count() == 1 and await following.is_visible():
        return "following"
    if await follow.count() == 1 and await follow.is_visible():
        return "follow"
    return "unknown"


async def inspect_profile(page, handle):
    """Return state only after verifying the page identifies the requested handle."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    response = await page.goto(profile_url(handle), wait_until="domcontentloaded", timeout=35000)
    if response and response.status in (404, 410):
        raise UnavailableJob(f"Profile returned HTTP {response.status}")
    if response and response.status in (401, 403, 429):
        raise PauseRun(f"FOMO returned HTTP {response.status}")
    if response and response.status >= 500:
        raise RetryJob(f"FOMO returned HTTP {response.status}")
    if await login_prompt_visible_async(page) or await captcha_visible_async(page):
        raise PauseRun("Login or manual verification is required")
    parsed = urlparse(page.url)
    path_handle = unquote(parsed.path.removeprefix("/profile/")).strip("/")
    if parsed.hostname not in ("fomo.family", "www.fomo.family") or path_handle.casefold() != handle.casefold():
        raise PauseRun("Profile URL changed unexpectedly")
    try:
        await page.get_by_text(re.compile(rf"^@{re.escape(handle)}$", re.I)).first.wait_for(timeout=20000)
    except PlaywrightTimeout as exc:
        raise RetryJob("Target profile did not finish loading") from exc
    # FOMO briefly renders Follow before loading the account's actual relation.
    await asyncio.sleep(STATE_SETTLE_SECONDS)
    return await button_state(page)


async def follow_one(page, row, self_handle, stop):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    handle = row["handle"]
    if handle.casefold() == self_handle.casefold():
        return "skipped_self"
    try:
        state = await inspect_profile(page, handle)
        if state == "following":
            return "already_following"
        if state != "follow":
            raise PauseRun("No unambiguous Follow button on target profile")
        if stop.is_set():
            raise RetryJob("Service is stopping before the click")
        await page.get_by_role("button", name="Follow", exact=True).click()
        try:
            await page.get_by_role("button", name="Following", exact=True).wait_for(timeout=12000)
        except PlaywrightTimeout:
            pass
        # The immediate Following label may be optimistic. Reload to prove persistence.
        state = await inspect_profile(page, handle)
        if state == "following":
            return "followed"
        if state == "follow":
            raise RetryJob("Follow was not confirmed; will recheck before another click")
        raise PauseRun("Follow result is unknown after reloading the profile")
    except (PlaywrightTimeout, OSError) as exc:
        raise RetryJob(f"Browser/network timeout: {type(exc).__name__}") from exc
