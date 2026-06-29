"""
MedCompare India — v3 Sequential Edition
==========================================
Runs pharmacies sequentially to avoid RAM overflow.
Uses single Playwright browser reused across requests.
"""
import asyncio
import time
import re
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from cachetools import TTLCache
import httpx

from services.matcher import group_by_medicine, normalize
from database.db import init_db, save_search, get_popular_searches

# ── Shared state ──────────────────────────────────────────────────────────────
cache: TTLCache = TTLCache(maxsize=1000, ttl=1800)
_browser = None
_playwright = None

async def get_browser():
    global _browser, _playwright
    if _browser and _browser.is_connected():
        return _browser
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process", "--no-zygote",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    return _browser

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        await get_browser()
        print("✅ Browser ready")
    except Exception as e:
        print(f"⚠️ Browser init: {e}")
    yield
    global _browser, _playwright
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()

app = FastAPI(title="MedCompare India", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Price parser ──────────────────────────────────────────────────────────────
def px(s) -> float:
    if s is None: return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(s))
    try: return float(cleaned)
    except: return 0.0

def mk(name, price, mrp, link) -> dict | None:
    price = px(price); mrp = px(mrp) or price
    if not name or price <= 0: return None
    if price > 50000: price /= 100
    if mrp   > 50000: mrp   /= 100
    return {
        "name": str(name).strip(), "price": round(price,2),
        "mrp": round(mrp,2), "discount": round((1-price/mrp)*100) if mrp>price else 0,
        "link": link, "inStock": True
    }

# ── Single page capture ───────────────────────────────────────────────────────
async def capture(url: str, wait_ms: int = 6000, actions=None) -> list[dict]:
    """Open one page, capture JSON responses, close page"""
    browser  = await get_browser()
    captured = []
    try:
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"}
        )
        await ctx.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,ico}", lambda r: r.abort())

        async def on_resp(resp):
            try:
                if resp.status != 200: return
                if "json" not in resp.headers.get("content-type",""): return
                data = await resp.json()
                if data: captured.append({"url": resp.url, "data": data})
            except Exception: pass

        page = await ctx.new_page()
        page.on("response", on_resp)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception: pass
        if actions:
            try: await actions(page)
            except Exception: pass
        await page.wait_for_timeout(wait_ms)
        await ctx.close()
    except Exception as e:
        print(f"Capture error {url}: {e}")
    return captured

# ── PharmEasy ─────────────────────────────────────────────────────────────────
async def fetch_pharmeasy(query: str) -> dict:
    pharmacy = "PharmEasy"
    url      = f"https://pharmeasy.in/search/all?name={query}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent":"Mozilla/5.0","Accept-Language":"en-IN"})
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
            if m:
                pp  = json.loads(m.group(1)).get("props",{}).get("pageProps",{})
                lst = pp.get("productList") or pp.get("searchResult",{}).get("products") or []
                products = []
                for p in lst[:5]:
                    name  = p.get("name") or p.get("productName") or ""
                    price = p.get("salePriceDecimal") or p.get("sellingPrice") or p.get("price")
                    mrp   = p.get("mrpDecimal") or p.get("mrp") or price
                    slug  = p.get("slug") or p.get("urlKey") or ""
                    prod  = mk(name, price, mrp, f"https://pharmeasy.in/medicines/all/{slug}")
                    if prod: products.append(prod)
                if products:
                    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None}
    except Exception: pass
    # Browser fallback
    resps = await capture(url, 5000)
    return parse_generic(resps, pharmacy, url, "https://pharmeasy.in/medicines/all/")

# ── 1mg ───────────────────────────────────────────────────────────────────────
async def fetch_1mg(query: str) -> dict:
    pharmacy = "1mg"
    url      = f"https://www.1mg.com/search/all?name={query}"
    found    = {}

    async def scroll(page):
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 300)")
        await page.wait_for_timeout(1500)

    resps = await capture(url, 6000, scroll)

    for r in resps:
        if "search/all" in r["url"] and "pwa-dweb-api" in r["url"]:
            found = r["data"]
            break

    items    = found.get("data", {}).get("search_results", [])
    products = []
    for item in items[:5]:
        name   = item.get("name","")
        prices = item.get("prices",{})
        price  = px(prices.get("discounted_price") or prices.get("mrp"))
        mrp    = px(prices.get("mrp") or prices.get("discounted_price"))
        path   = item.get("url","")
        link   = f"https://www.1mg.com{path}" if path else url
        prod   = mk(name, price, mrp, link)
        if prod: products.append(prod)

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


@app.get("/api/debugnew")
async def debug_new(q: str = Query(...)):
    import httpx, re as _re, json as _json
    from urllib.parse import quote
    UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    out = {}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        h    = {"User-Agent": UA, "Accept": "text/html,application/json"}
        h_j  = {"User-Agent": UA, "Accept": "application/json"}

        # ── Jan Aushadhi — only use confirmed domain ──────────────────────────
        ja = []
        ja_urls = [
            f"https://janaushadhi.gov.in/product-list.aspx?cat=0&search={quote(q)}",
            f"https://janaushadhi.gov.in/SearchProduct.aspx?search={quote(q)}",
            f"https://janaushadhi.gov.in/ProductList.aspx?search={quote(q)}",
        ]
        for u in ja_urls:
            try:
                r = await client.get(u, headers=h)
                ja.append({
                    "url": u[30:90], "status": r.status_code,
                    "size": len(r.text), "sample": r.text[:500]
                })
            except Exception as e:
                ja.append({"url": u[30:90], "error": str(e)})
        out["jan_aushadhi"] = ja

        # ── Sastasundar — check what their site actually returns ───────────────
        ss = []
        try:
            r = await client.get(
                f"https://www.sastasundar.com/search?q={quote(q)}",
                headers=h
            )
            m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, _re.DOTALL)
            if m:
                data = _json.loads(m.group(1))
                pp   = data.get("props",{}).get("pageProps",{})
                ss.append({
                    "url": "search page", "status": r.status_code,
                    "has_next_data": True,
                    "pageProps_keys": list(pp.keys()),
                    "sample": str(pp)[:600]
                })
            else:
                ss.append({
                    "url": "search page", "status": r.status_code,
                    "has_next_data": False,
                    "size": len(r.text),
                    "sample": r.text[:400]
                })
        except Exception as e:
            ss.append({"url": "search page", "error": str(e)})

        # Try Sastasundar API with same domain only
        for api in [
            f"https://www.sastasundar.com/api/search?q={quote(q)}",
            f"https://www.sastasundar.com/api/products?search={quote(q)}",
        ]:
            try:
                r = await client.get(api, headers=h_j)
                try: body = str(r.json())[:300]
                except: body = r.text[:200]
                ss.append({"url": api[30:70], "status": r.status_code, "body": body})
            except Exception as e:
                ss.append({"url": api[30:70], "error": str(e)})

        out["sastasundar"] = ss

    return out

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse("static/index.html")
