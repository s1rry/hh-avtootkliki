"""
Manual Habr Career login via VNC.
Opens Playwright Chromium (headed) with saved cookies, navigates to
SSO login. You complete login via VNC, script saves cookies.
"""
import asyncio
import json
import subprocess
import os
import time
from pathlib import Path


async def main():
    os.environ["DISPLAY"] = ":99"
    # Start Xvfb + x11vnc (if not already running)
    try:
        subprocess.run(["pgrep", "-x", "Xvfb"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x720x24"])
        time.sleep(2)

    try:
        subprocess.run(["pgrep", "-x", "x11vnc"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.Popen([
            "x11vnc", "-display", ":99", "-forever",
            "-passwd", "hh2026", "-rfbport", "5900", "-noxdamage",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

    print("=" * 50)
    print("VNC: 138.16.160.99:5900  pass: hh2026")
    print("Залогинься на Habr Career вручную, скрипт сам сохранит куки")
    print("=" * 50)

    from playwright.async_api import async_playwright

    storage_path = Path("data/browser_sessions/habr_state.json")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx_opts = {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "viewport": {"width": 1200, "height": 680},
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
    }
    if storage_path.exists():
        ctx_opts["storage_state"] = str(storage_path)
        print(f"Loaded existing cookies from {storage_path}")

    ctx = await browser.new_context(**ctx_opts)
    await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page = await ctx.new_page()
    await page.goto("https://career.habr.com/users/auth/tmid", wait_until="domcontentloaded")

    print("Browser open. Login via VNC, script will detect it...")

    while True:
        await asyncio.sleep(5)
        try:
            # Visit profile-related page; if we're authenticated we'll see the user menu
            cur = await page.query_selector('a[href="/users/logout"], [data-qa="header__user"]')
            if not cur:
                cur = await page.query_selector('a[href*="/profile"]')
            if not cur:
                # check via account API: if location is career.habr.com (post-SSO redirect)
                if "career.habr.com" in page.url and "/login" not in page.url and "account.habr.com" not in page.url:
                    # Try one more navigation to home and re-check
                    await page.goto("https://career.habr.com/", wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                    cur = await page.query_selector('a[href="/users/logout"], [data-qa="header__user"]')
            if cur:
                print("Login detected. Saving cookies...")
                state = await ctx.storage_state()
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                storage_path.write_text(json.dumps(state), encoding="utf-8")
                print(f"Saved to {storage_path} ({len(state.get('cookies', []))} cookies)")
                break
        except Exception:
            pass

    await asyncio.sleep(2)
    await browser.close()
    await pw.stop()
    print("Done. Restart bot: systemctl restart job-hunter")


if __name__ == "__main__":
    asyncio.run(main())
