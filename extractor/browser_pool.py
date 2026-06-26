"""
Browser Pool — Single shared Playwright instance
=================================================
Instead of each connector opening its own browser,
we use one shared browser with multiple contexts.
This prevents RAM overflow and context conflicts.
"""
import asyncio
from playwright.async_api import async_playwright, Browser, Playwright

_playwright: Playwright = None
_browser: Browser       = None
_lock = asyncio.Lock()

async def get_browser() -> Browser:
    """Get or create shared browser instance"""
    global _playwright, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--single-process",  # Important for Railway
                ]
            )
    return _browser

async def capture_pharmacy(url: str, wait_ms: int = 7000, actions=None) -> list[dict]:
    """
    Open URL in a new browser context, capture JSON responses.
    Uses shared browser to avoid RAM issues.
    """
    browser   = await get_browser()
    captured  = []

    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
            }
        )

        # Block images/fonts to save RAM and speed up
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,ico}",
            lambda r: r.abort()
        )

        async def handle_response(response):
            try:
                if response.status != 200:
                    return
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = await response.json()
                if data:
                    captured.append({"url": response.url, "data": data})
            except Exception:
                pass

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass

        if actions:
            try:
                await actions(page)
            except Exception:
                pass

        await page.wait_for_timeout(wait_ms)
        await context.close()

    except Exception as e:
        captured.append({"url": url, "data": {}, "error": str(e)})

    return captured
