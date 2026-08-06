"""
MedCompare India — Clean Production Version
4 Working Pharmacies: PharmEasy, Truemeds, 1mg, Apollo
"""
import asyncio
import time
import re
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from cachetools import TTLCache
import httpx

from services.matcher import group_by_medicine, normalize
from database.db import init_db, save_search, get_popular_searches

# ── Shared browser ────────────────────────────────────────────────────────────
_browser    = None
_playwright = None

async def get_browser():
    global _browser, _playwright
    if _browser and _browser.is_connected():
        return _browser
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    _browser    = await _playwright.chromium.launch(
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
        print(f"⚠️ Browser: {e}")
    yield
    global _browser, _playwright
    if _browser:
        try: await _browser.close()
        except: pass
    if _playwright:
        try: await _playwright.stop()
        except: pass

app = FastAPI(title="MedCompare India", version="4.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# 30-minute cache
cache: TTLCache = TTLCache(maxsize=1000, ttl=1800)

# ── Helpers ───────────────────────────────────────────────────────────────────
def px(s) -> float:
    if s is None: return 0.0
    try: return float(re.sub(r"[^\d.]", "", str(s)))
    except: return 0.0

def mk(name, price, mrp, link, image=None) -> dict | None:
    price = px(price); mrp = px(mrp) or price
    if not name or price <= 0: return None
    if price > 50000: price /= 100
    if mrp   > 50000: mrp   /= 100
    return {
        "name":     str(name).strip(),
        "price":    round(price, 2),
        "mrp":      round(mrp, 2),
        "discount": round((1 - price/mrp)*100) if mrp > price else 0,
        "link":     link,
        "image":    image,
        "inStock":  True,
    }

def find_list(data, depth=0):
    if depth > 6: return []
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict):
            keys = {k.lower() for k in data[0]}
            if {"price","mrp","sellingprice","saleprice","selling_price","offerprice"} & keys and \
               {"name","productname","title","display_name","medicinename"} & keys:
                return data
        return []
    if isinstance(data, dict):
        for k in ["products","skus","items","results","data","productList",
                  "medicines","search_results","hits","elasticProductDetails"]:
            if k in data:
                r = find_list(data[k], depth+1)
                if r: return r
        for v in data.values():
            if isinstance(v, (dict, list)):
                r = find_list(v, depth+1)
                if r: return r
    return []

BASE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ── Browser page capture ──────────────────────────────────────────────────────
async def browser_capture(url: str, wait_ms: int = 7000, actions=None) -> list[dict]:
    browser  = await get_browser()
    captured = []
    try:
        ctx = await browser.new_context(
            user_agent=BASE_HEADERS["User-Agent"],
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"}
        )
        await ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,ico}",
            lambda r: r.abort()
        )

        async def on_resp(resp):
            try:
                if resp.status != 200: return
                if "json" not in resp.headers.get("content-type", ""): return
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
        print(f"Browser capture error: {e}")
    return captured


# ── PharmEasy — __NEXT_DATA__ SSR ────────────────────────────────────────────
async def fetch_pharmeasy(query: str) -> dict:
    pharmacy = "PharmEasy"
    url      = f"https://pharmeasy.in/search/all?name={query}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers=BASE_HEADERS)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
            if m:
                pp  = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
                lst = pp.get("productList") or pp.get("searchResult", {}).get("products") or []
                products = []
                for p in lst[:5]:
                    name  = p.get("name") or p.get("productName") or ""
                    price = p.get("salePriceDecimal") or p.get("sellingPrice") or p.get("price")
                    mrp   = p.get("mrpDecimal") or p.get("mrp") or price
                    slug  = p.get("slug") or p.get("urlKey") or ""
                    image = p.get("image")
                    prod  = mk(name, price, mrp, f"https://pharmeasy.in/online-medicine-order/{slug}", image)                    
                    if prod: products.append(prod)
                if products:
                    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None}
    except Exception: pass
    return {"pharmacy": pharmacy, "products": [], "searchUrl": url, "error": "Could not fetch"}


# ── Truemeds — JWT token + search API ────────────────────────────────────────
async def fetch_truemeds(query: str) -> dict:
    pharmacy = "Truemeds"
    from urllib.parse import quote
    base_url = f"https://www.truemeds.in/search?query={quote(query)}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            h = {"User-Agent": BASE_HEADERS["User-Agent"],
                 "Referer": "https://www.truemeds.in/",
                 "Accept": "application/json", "Content-Type": "application/json"}

            # Get JWT token
            tr    = await client.post("https://www.truemeds.in/api/getSessionToken", headers=h, json={})
            token = tr.text.strip().strip('"')
            if not token or len(token) < 20:
                return {"pharmacy": pharmacy, "products": [], "searchUrl": base_url, "error": "Token failed"}

            # Search API
            from urllib.parse import quote
            api = (
                f"https://nal.tmmumbai.in/SearchService/getSearchResult"
                f"?warehouseId=20&elasticSearchType=SKU_BRAND_SEARCH"
                f"&searchString={quote(query)}&isMultiSearch=true"
                f"&platform=D_WEB&versionName=TM_WEBSITE_V_4.25.9"
                f"&sessionToken={token}"
            )
            r = await client.get(api, headers={**h, "sessionToken": token})
            if r.status_code != 200:
                return {"pharmacy": pharmacy, "products": [], "searchUrl": base_url, "error": f"API {r.status_code}"}

            items    = r.json().get("responseData", {}).get("elasticProductDetails", [])
            products = []
            for item in items[:5]:
                p     = item.get("product", {})
                name  = p.get("skuName") or p.get("productName") or ""
                price = px(p.get("sellingPrice") or p.get("discountedPrice") or p.get("mrp"))
                mrp   = px(p.get("mrp") or price)
                # Use productUrlSuffix directly — confirmed exact field from API
                # e.g. "otc/dolo-650-mg-tablet-15-tm-tacr1-011691"
                url_suffix = p.get("productUrlSuffix") or ""
                if url_suffix:
                    link = f"https://www.truemeds.in/{url_suffix}"
                else:
                    link = f"https://www.truemeds.in/search?query={quote(name)}"
                prod  = mk(name, price, mrp, link)
                if prod: products.append(prod)

            return {"pharmacy": pharmacy, "products": products,
                    "searchUrl": base_url, "error": None if products else "No products found"}
    except Exception as e:
        return {"pharmacy": pharmacy, "products": [], "searchUrl": base_url, "error": str(e)}


# ── 1mg — Browser XHR capture with smart wait ────────────────────────────────
async def fetch_1mg(query: str) -> dict:
    pharmacy = "1mg"
    url      = f"https://www.1mg.com/search/all?name={query}"
    browser  = await get_browser()
    captured = []

    try:
        ctx = await browser.new_context(
            user_agent=BASE_HEADERS["User-Agent"],
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"}
        )
        await ctx.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,ico}",
            lambda r: r.abort()
        )

        search_done = asyncio.Event()

        async def on_resp(resp):
            try:
                if resp.status != 200: return
                if "json" not in resp.headers.get("content-type", ""): return
                data = await resp.json()
                if data:
                    captured.append({"url": resp.url, "data": data})
                    # Signal early exit when search API responds
                    if "search/all" in resp.url and "pwa-dweb-api" in resp.url:
                        search_done.set()
            except Exception: pass

        page = await ctx.new_page()
        page.on("response", on_resp)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception: pass

        # Scroll to trigger search
        await page.wait_for_timeout(1000)
        await page.evaluate("window.scrollTo(0, 300)")

        # Wait for search API response OR timeout — whichever comes first
        try:
            await asyncio.wait_for(search_done.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            # Extra scroll + wait as fallback
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(2000)

        await ctx.close()
    except Exception as e:
        print(f"1mg error: {e}")

    # Parse results
    found = {}
    for r in captured:
        if "search/all" in r["url"] and "pwa-dweb-api" in r["url"]:
            found = r["data"]
            break

    items    = found.get("data", {}).get("search_results", [])
    products = []
    for item in items[:5]:
        name   = item.get("name", "")
        prices = item.get("prices", {})
        price  = px(prices.get("discounted_price") or prices.get("mrp"))
        mrp    = px(prices.get("mrp") or prices.get("discounted_price"))
        path   = item.get("url", "")
        link   = f"https://www.1mg.com{path}" if path else url
        prod   = mk(name, price, mrp, link)
        if prod: products.append(prod)

    return {"pharmacy": pharmacy, "products": products,
            "searchUrl": url, "error": None if products else "No products found"}


# ── Apollo — Fresh standalone browser (this is what worked originally) ───────
async def fetch_apollo(query: str) -> dict:
    pharmacy = "Apollo Pharmacy"
    url      = f"https://www.apollopharmacy.in/search-medicines/{query}"
    from playwright.async_api import async_playwright

    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        try:
            ctx = await browser.new_context(
                user_agent=BASE_HEADERS["User-Agent"],
                locale="en-IN", timezone_id="Asia/Kolkata",
            )
            await ctx.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,ico}",
                lambda r: r.abort()
            )

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

            try:
                await page.wait_for_selector(
                    "[class*='ProductCard'],[class*='medicine-card'],[class*='MedicineCard']",
                    timeout=6000
                )
                # Selector found fast — short settle time is enough
                await page.wait_for_timeout(2500)
            except Exception:
                # Selector never appeared — give XHR more time as fallback
                await page.wait_for_timeout(5000)

            await ctx.close()
        finally:
            await browser.close()

    products = []
    for resp in captured:
        items = find_list(resp.get("data", {}))
        if not items: continue
        for p in items[:5]:
            name  = next((p.get(f) for f in ["name","productName","title"] if p.get(f)), "")
            price = next((px(p.get(f)) for f in ["offerPrice","sellingPrice","price"] if p.get(f) and px(p.get(f)) > 0), 0.0)
            mrp   = next((px(p.get(f)) for f in ["mrpPrice","mrp","MRP"] if p.get(f) and px(p.get(f)) > 0), price)
            slug  = next((str(p.get(f)) for f in ["urlKey","slug","productId","id"] if p.get(f)), "")
            link  = f"https://www.apollopharmacy.in/medicine/{slug}" if slug else url
            prod  = mk(name, price, mrp, link)
            if prod: products.append(prod)
        if products: break

    return {"pharmacy": pharmacy, "products": products,
            "searchUrl": url, "error": None if products else f"No products found ({len(captured)} responses)"}


# ── Compare engine ────────────────────────────────────────────────────────────
HTTP_FETCHERS    = [("PharmEasy", fetch_pharmeasy), ("Truemeds", fetch_truemeds)]
BROWSER_FETCHERS = [("1mg", fetch_1mg), ("Apollo Pharmacy", fetch_apollo)]
ALL_FETCHERS     = HTTP_FETCHERS + BROWSER_FETCHERS


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    b = await get_browser()
    return {
        "status":     "ok",
        "version":    "4.0.0",
        "browser":    "connected" if b.is_connected() else "disconnected",
        "cache_size": len(cache),
        "pharmacies": [n for n, _ in ALL_FETCHERS],
    }


@app.get("/api/compare")
async def compare(medicine: str = Query(..., min_length=1), pincode: str = Query(None)):
    q         = medicine.strip()
    cache_key = f"{normalize(q)}_{pincode or 'all'}"

    if cache_key in cache:
        r = dict(cache[cache_key]); r["cached"] = True; return r

    start = time.time()

    # Run HTTP fetchers concurrently (fast — no browser)
    http_tasks   = [asyncio.wait_for(fn(q), timeout=12) for _, fn in HTTP_FETCHERS]
    http_results = await asyncio.gather(*http_tasks, return_exceptions=True)

    raw = []
    for (name, _), r in zip(HTTP_FETCHERS, http_results):
        if isinstance(r, Exception):
            raw.append({"pharmacy": name, "products": [], "searchUrl": "", "error": str(r)})
        else:
            raw.append(r)

    # Run browser fetchers sequentially (avoid RAM crash)
    for name, fn in BROWSER_FETCHERS:
        try:
            result = await asyncio.wait_for(fn(q), timeout=22)
        except asyncio.TimeoutError:
            result = {"pharmacy": name, "products": [], "searchUrl": "", "error": "Timeout"}
        except Exception as e:
            result = {"pharmacy": name, "products": [], "searchUrl": "", "error": str(e)}
        raw.append(result)

    matched    = group_by_medicine(raw, q, threshold=70.0)
    best_price = best_ph = None
    max_price  = 0.0
    found_on   = 0

    for r in matched:
        prods = r.get("products", [])
        if prods:
            found_on += 1
            p = float(prods[0]["price"])
            if best_price is None or p < best_price:
                best_price = p; best_ph = r["pharmacy"]
            if p > max_price: max_price = p

    response = {
        "medicine":      q,
        "pincode":       pincode,
        "results":       matched,
        "best_price":    best_price,
        "best_pharmacy": best_ph,
        "max_savings":   round(max_price - best_price, 2) if best_price and max_price > best_price else 0,
        "found_on":      found_on,
        "total":         len(ALL_FETCHERS),
        "time_taken":    round(time.time() - start, 2),
        "cached":        False,
    }

    cache[cache_key] = response
    save_search(q, found_on, pincode)
    return response


@app.get("/api/popular")
async def popular():
    return {"popular": get_popular_searches(10)}

@app.delete("/api/cache")
async def clear_cache():
    cache.clear(); return {"message": "Cache cleared"}


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse("static/index.html")
