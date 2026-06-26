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

# ── NetMeds ───────────────────────────────────────────────────────────────────
async def fetch_netmeds(query: str) -> dict:
    pharmacy = "NetMeds"
    url      = f"https://www.netmeds.com/prescriptions/search-results?q={query}"

    async def scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    resps = await capture(url, 7000, scroll)
    return parse_generic(resps, pharmacy, url, "https://www.netmeds.com/prescriptions/")

# ── Apollo ────────────────────────────────────────────────────────────────────
async def fetch_apollo(query: str) -> dict:
    pharmacy = "Apollo Pharmacy"
    url      = f"https://www.apollopharmacy.in/search-medicines/{query}"

    async def wait_cards(page):
        try:
            await page.wait_for_selector(
                "[class*='ProductCard'],[class*='medicine-card'],[class*='MedicineCard']",
                timeout=8000
            )
        except Exception:
            await page.wait_for_timeout(4000)

    resps = await capture(url, 7000, wait_cards)
    return parse_generic(resps, pharmacy, url, "https://www.apollopharmacy.in/medicine/")

# ── MedKart ───────────────────────────────────────────────────────────────────
async def fetch_medkart(query: str) -> dict:
    pharmacy = "MedKart"
    url      = f"https://www.medkart.in/search?q={query}"

    async def scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    resps = await capture(url, 7000, scroll)
    return parse_generic(resps, pharmacy, url, "https://www.medkart.in/order-medicine/")

# ── Generic parser ────────────────────────────────────────────────────────────
def find_list(data, depth=0):
    if depth > 6: return []
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict):
            keys = {k.lower() for k in data[0]}
            price_k = {"price","mrp","sellingprice","saleprice","selling_price","offerprice","discountedprice"}
            name_k  = {"name","productname","title","display_name","medicinename"}
            if price_k & keys and name_k & keys:
                return data
        return []
    if isinstance(data, dict):
        for k in ["products","skus","items","results","data","productList","medicines","search_results","hits"]:
            if k in data:
                r = find_list(data[k], depth+1)
                if r: return r
        for v in data.values():
            if isinstance(v, (dict,list)):
                r = find_list(v, depth+1)
                if r: return r
    return []

def parse_generic(resps, pharmacy, base_url, slug_base) -> dict:
    for resp in resps:
        items = find_list(resp.get("data",{}))
        if not items: continue
        products = []
        for p in items[:5]:
            name  = next((p.get(f) for f in ["name","productName","title","display_name"] if p.get(f)), "")
            price = next((px(p.get(f)) for f in ["selling_price","sellingPrice","salePriceDecimal","offerPrice","price","discountedPrice"] if p.get(f) is not None and px(p.get(f)) > 0), 0.0)
            mrp   = next((px(p.get(f)) for f in ["mrp","MRP","mrpPrice","mrpDecimal","max_retail_price","maxPrice"] if p.get(f) is not None and px(p.get(f)) > 0), price)
            slug  = next((str(p.get(f)) for f in ["slug","urlKey","url_key","handle","id","productId"] if p.get(f)), "")
            link  = f"{slug_base}{slug}" if slug else base_url
            prod  = mk(name, price, mrp, link)
            if prod: products.append(prod)
        if products:
            return {"pharmacy": pharmacy, "products": products, "searchUrl": base_url, "error": None}
    return {"pharmacy": pharmacy, "products": [], "searchUrl": base_url, "error": "No products found"}

# ── FETCHERS list ─────────────────────────────────────────────────────────────
FETCHERS = [
    ("PharmEasy",       fetch_pharmeasy),
    ("1mg",             fetch_1mg),
    ("NetMeds",         fetch_netmeds),
    ("Apollo Pharmacy", fetch_apollo),
    ("MedKart",         fetch_medkart),
]

# ── API Routes ────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    browser = await get_browser()
    return {
        "status":       "ok",
        "version":      "3.0.0",
        "browser":      "connected" if browser.is_connected() else "disconnected",
        "cache_size":   len(cache),
        "pharmacies":   [n for n,_ in FETCHERS],
    }

@app.get("/api/compare")
async def compare(
    medicine: str = Query(..., min_length=1),
    pincode:  str = Query(None),
):
    q         = medicine.strip()
    cache_key = f"{normalize(q)}_{pincode or 'all'}"

    if cache_key in cache:
        r = dict(cache[cache_key]); r["cached"] = True; return r

    start = time.time()

    # Run sequentially to avoid RAM overflow
    # PharmEasy is HTTP-only so run it concurrently with first browser task
    raw = []
    for name, fn in FETCHERS:
        try:
            result = await asyncio.wait_for(fn(q), timeout=30)
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
        "total":         len(FETCHERS),
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


# ── NetMeds + MedKart deep debug ─────────────────────────────────────────────
@app.get("/api/nmdebug")
async def nm_debug(q: str = Query(...)):
    """Debug NetMeds and MedKart XHR capture"""
    results = {}

    # NetMeds
    nm_url  = f"https://www.netmeds.com/prescriptions/search-results?q={q}"
    nm_captured = []

    async def nm_scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    nm_resps = await capture(nm_url, 7000, nm_scroll)
    for r in nm_resps:
        nm_captured.append({
            "url":      r["url"][:120],
            "top_keys": list(r["data"].keys())[:10] if isinstance(r["data"], dict) else type(r["data"]).__name__,
            "sample":   str(r["data"])[:300],
        })

    results["netmeds"] = {
        "url":        nm_url,
        "responses":  len(nm_resps),
        "captured":   nm_captured,
    }

    # MedKart
    mk_url = f"https://www.medkart.in/search?q={q}"
    mk_captured = []

    async def mk_scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    mk_resps = await capture(mk_url, 7000, mk_scroll)
    for r in mk_resps:
        mk_captured.append({
            "url":      r["url"][:120],
            "top_keys": list(r["data"].keys())[:10] if isinstance(r["data"], dict) else type(r["data"]).__name__,
            "sample":   str(r["data"])[:300],
        })

    results["medkart"] = {
        "url":        mk_url,
        "responses":  len(mk_resps),
        "captured":   mk_captured,
    }

    return results


# ── NetMeds + MedKart direct API hunt ────────────────────────────────────────
@app.get("/api/hunt")
async def hunt(q: str = Query(...)):
    """Try every possible API for NetMeds and MedKart"""
    import httpx
    from urllib.parse import quote
    results = {}
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

    # ── NetMeds ──────────────────────────────────────────────────────────────
    nm_tests = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:

        # Try POST search (Fynd uses POST for search)
        post_apis = [
            "https://www.netmeds.com/api/service/application/catalog/v1.0/search/",
            "https://www.netmeds.com/api/service/application/catalog/v2.0/search/",
        ]
        for api in post_apis:
            try:
                r = await client.post(api,
                    headers={"User-Agent": UA, "Accept": "application/json",
                             "Content-Type": "application/json",
                             "Referer": "https://www.netmeds.com/"},
                    json={"q": q, "page_no": 1, "page_size": 5}
                )
                nm_tests.append({"method":"POST","url":api[:80],"status":r.status_code,"body":r.text[:200]})
            except Exception as e:
                nm_tests.append({"method":"POST","url":api[:80],"error":str(e)})

        # Try GET with different headers
        get_apis = [
            f"https://www.netmeds.com/api/service/application/catalog/v1.0/search/?q={quote(q)}&page_no=1&page_size=5",
            f"https://www.netmeds.com/api/service/application/catalog/v2.0/search/?q={quote(q)}&page_no=1&page_size=5",
            f"https://www.netmeds.com/catalogsearch/result/index/?q={quote(q)}&format=json",
            f"https://www.netmeds.com/api/v1/search?q={quote(q)}",
        ]
        for api in get_apis:
            try:
                # Get app token from homepage first
                home = await client.get("https://www.netmeds.com/",
                    headers={"User-Agent": UA})
                import re
                token_m = re.search(r'"x-application-token"\s*:\s*"([^"]+)"', home.text)
                app_token = token_m.group(1) if token_m else None

                hdrs = {"User-Agent": UA, "Accept": "application/json",
                        "Referer": "https://www.netmeds.com/"}
                if app_token:
                    hdrs["x-application-token"] = app_token

                r = await client.get(api, headers=hdrs)
                nm_tests.append({"method":"GET","url":api[:80],"status":r.status_code,
                                  "token":bool(app_token),"body":r.text[:200]})
            except Exception as e:
                nm_tests.append({"method":"GET","url":api[:80],"error":str(e)})

    results["netmeds"] = nm_tests

    # ── MedKart ──────────────────────────────────────────────────────────────
    mk_tests = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        mk_apis = [
            f"https://www.medkart.in/search?q={quote(q)}&_rsc=1",
            f"https://www.medkart.in/api/medicines?name={quote(q)}",
            f"https://app.medkart.in/api/v2/products?search={quote(q)}&limit=10",
            f"https://app.medkart.in/api/v2/drug/search?q={quote(q)}",
            f"https://app.medkart.in/api/v2/catalog/search?q={quote(q)}",
            f"https://app.medkart.in/api/v3/search?q={quote(q)}",
            f"https://www.medkart.in/_next/data/search?q={quote(q)}",
        ]
        for api in mk_apis:
            try:
                r = await client.get(api, headers={
                    "User-Agent": UA, "Accept": "application/json",
                    "Referer": "https://www.medkart.in/",
                    "Origin": "https://www.medkart.in"
                })
                mk_tests.append({"url":api[:90],"status":r.status_code,"body":r.text[:200]})
            except Exception as e:
                mk_tests.append({"url":api[:90],"error":str(e)})

    results["medkart"] = mk_tests
    return results


# ── Test new pharmacies ───────────────────────────────────────────────────────
@app.get("/api/testpharmacy")
async def test_pharmacy(q: str = Query(...)):
    """Test Truemeds and MrMed scraping"""
    import httpx, re, json
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    results = {}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:

        # Test Truemeds
        truemeds_url = f"https://www.truemeds.in/search?query={q}"
        r = await client.get(truemeds_url, headers={"User-Agent": UA, "Accept-Language": "en-IN"})
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                pp   = data.get("props",{}).get("pageProps",{})
                results["truemeds"] = {
                    "status":        r.status_code,
                    "has_next_data": True,
                    "pageProps_keys": list(pp.keys()),
                    "sample":        str(pp)[:500],
                }
            except Exception as e:
                results["truemeds"] = {"status": r.status_code, "error": str(e)}
        else:
            results["truemeds"] = {
                "status":        r.status_code,
                "has_next_data": False,
                "html_size":     len(r.text),
                "html_sample":   r.text[:300],
            }

        # Test MrMed
        mrmed_url = f"https://www.mrmed.in/search?q={q}"
        r2 = await client.get(mrmed_url, headers={"User-Agent": UA, "Accept-Language": "en-IN"})
        m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r2.text, re.DOTALL)
        if m2:
            try:
                data2 = json.loads(m2.group(1))
                pp2   = data2.get("props",{}).get("pageProps",{})
                results["mrmed"] = {
                    "status":         r2.status_code,
                    "has_next_data":  True,
                    "pageProps_keys": list(pp2.keys()),
                    "sample":         str(pp2)[:500],
                }
            except Exception as e:
                results["mrmed"] = {"status": r2.status_code, "error": str(e)}
        else:
            results["mrmed"] = {
                "status":        r2.status_code,
                "has_next_data": False,
                "html_size":     len(r2.text),
                "html_sample":   r2.text[:300],
            }

        # Test Flipkart Health+
        fk_url = f"https://www.flipkart.com/search?q={q}&marketplace=HEALTH"
        r3 = await client.get(fk_url, headers={"User-Agent": UA, "Accept-Language": "en-IN"})
        results["flipkart_health"] = {
            "status":    r3.status_code,
            "html_size": len(r3.text),
            "sample":    r3.text[:200],
        }

    return results


# ── Truemeds + MrMed Playwright test ─────────────────────────────────────────
@app.get("/api/testplawright")
async def test_playwright_new(q: str = Query(...)):
    """Test Truemeds and MrMed via Playwright"""
    results = {}

    # Test Truemeds
    tm_url   = f"https://www.truemeds.in/search?query={q}"
    tm_resps = await capture(tm_url, 6000)
    tm_found = []
    for r in tm_resps:
        items = find_list(r.get("data", {}))
        tm_found.append({
            "url":          r["url"][:100],
            "has_products": len(items) > 0,
            "top_keys":     list(r["data"].keys())[:8] if isinstance(r["data"], dict) else "list",
            "sample":       str(r["data"])[:200],
        })
    results["truemeds"] = {
        "url":       tm_url,
        "responses": len(tm_resps),
        "captured":  tm_found,
    }

    # Test MrMed
    mm_url   = f"https://www.mrmed.in/search?q={q}"
    mm_resps = await capture(mm_url, 6000)
    mm_found = []
    for r in mm_resps:
        items = find_list(r.get("data", {}))
        mm_found.append({
            "url":          r["url"][:100],
            "has_products": len(items) > 0,
            "top_keys":     list(r["data"].keys())[:8] if isinstance(r["data"], dict) else "list",
            "sample":       str(r["data"])[:200],
        })
    results["mrmed"] = {
        "url":       mm_url,
        "responses": len(mm_resps),
        "captured":  mm_found,
    }

    return results

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse("static/index.html")
