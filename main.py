"""
MedCompare India — Phase 1.5
Playwright Network Capture Edition
====================================
Uses real Chromium browser to intercept XHR/Fetch calls
from pharmacy websites — gets actual JSON API responses.
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

from connectors import (
    connect_pharmeasy,
    connect_1mg,
    connect_netmeds,
    connect_apollo,
    connect_medkart,
)
from services.matcher import group_by_medicine, normalize
from database.db import init_db, save_search, get_popular_searches

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="MedCompare India API",
    description="Live medicine price comparison — Playwright XHR capture",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 30-minute cache
cache: TTLCache = TTLCache(maxsize=1000, ttl=1800)

CONNECTORS = [
    ("PharmEasy",       connect_pharmeasy),
    ("1mg",             connect_1mg),
    ("NetMeds",         connect_netmeds),
    ("Apollo Pharmacy", connect_apollo),
    ("MedKart",         connect_medkart),
]

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status":     "ok",
        "version":    "2.0.0",
        "engine":     "Playwright XHR Capture",
        "pharmacies": [n for n, _ in CONNECTORS],
        "cache_size": len(cache),
    }

# ── Main compare endpoint ─────────────────────────────────────────────────────
@app.get("/api/compare")
async def compare(
    medicine: str = Query(..., min_length=1),
    pincode:  str = Query(None),
):
    q_clean   = medicine.strip()
    cache_key = f"{normalize(q_clean)}_{pincode or 'all'}"

    # Serve from cache
    if cache_key in cache:
        result = dict(cache[cache_key])
        result["cached"] = True
        return result

    start = time.time()

    # Run all pharmacy connectors in parallel
    tasks   = [fn(q_clean, pincode) for _, fn in CONNECTORS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Clean exceptions
    raw = []
    for (name, _), r in zip(CONNECTORS, results):
        if isinstance(r, Exception):
            raw.append({"pharmacy": name, "products": [], "searchUrl": "", "error": str(r)})
        else:
            raw.append(r)

    # Apply medicine matching
    matched = group_by_medicine(raw, q_clean, threshold=70.0)

    # Stats
    best_price = best_pharmacy = None
    max_price  = 0.0
    found_on   = 0

    for r in matched:
        prods = r.get("products", [])
        if prods:
            found_on += 1
            p = float(prods[0]["price"])
            if best_price is None or p < best_price:
                best_price    = p
                best_pharmacy = r["pharmacy"]
            if p > max_price:
                max_price = p

    response = {
        "medicine":      q_clean,
        "pincode":       pincode,
        "results":       matched,
        "best_price":    best_price,
        "best_pharmacy": best_pharmacy,
        "max_savings":   round(max_price - best_price, 2) if best_price and max_price > best_price else 0,
        "found_on":      found_on,
        "total":         len(CONNECTORS),
        "time_taken":    round(time.time() - start, 2),
        "cached":        False,
    }

    cache[cache_key] = response
    save_search(q_clean, found_on, pincode)
    return response

# ── Single pharmacy ───────────────────────────────────────────────────────────
@app.get("/api/pharmacy/{name}")
async def single(name: str, q: str = Query(...), pincode: str = Query(None)):
    connector_map = {
        "pharmeasy": connect_pharmeasy,
        "1mg":       connect_1mg,
        "netmeds":   connect_netmeds,
        "apollo":    connect_apollo,
        "medkart":   connect_medkart,
    }
    key = name.lower().replace(" ", "").replace("pharmacy", "")
    fn  = connector_map.get(key)
    if not fn:
        raise HTTPException(404, f"Unknown pharmacy: {name}")
    return await fn(q.strip(), pincode)

# ── Popular searches ──────────────────────────────────────────────────────────
@app.get("/api/popular")
async def popular():
    return {"popular": get_popular_searches(10)}

# ── Cache management ──────────────────────────────────────────────────────────
@app.delete("/api/cache")
async def clear_cache():
    cache.clear()
    return {"message": "Cache cleared"}


# ── Playwright debug — shows ALL captured network responses ─────────────────
@app.get("/api/debug/{pharmacy_name}")
async def debug_playwright(pharmacy_name: str, q: str = Query(..., min_length=1)):
    """Shows exactly what Playwright captures — all JSON responses"""
    from extractor.network_extractor import extract_network_responses_debug

    urls = {
        "pharmeasy": f"https://pharmeasy.in/search/all?name={q}",
        "1mg":       f"https://www.1mg.com/search/all?name={q}",
        "netmeds":   f"https://www.netmeds.com/catalogsearch/result?q={q}",
        "apollo":    f"https://www.apollopharmacy.in/search-medicines/{q}",
        "medkart":   f"https://medkart.in/search?q={q}",
    }

    url = urls.get(pharmacy_name.lower())
    if not url:
        raise HTTPException(404, "Unknown pharmacy. Use: pharmeasy, 1mg, netmeds, apollo, medkart")

    return await extract_network_responses_debug(url, wait_ms=7000)


# ── Raw API debug — fetch exact JSON from pharmacy API ───────────────────────
@app.get("/api/rawdebug/{pharmacy_name}")
async def raw_debug(pharmacy_name: str, q: str = Query(..., min_length=1)):
    """Fetch raw JSON from pharmacy API and show full structure"""
    import httpx
    from urllib.parse import quote

    configs = {
        "1mg": {
            "url": f"https://www.1mg.com/pwa-dweb-api/api/v4/search/all?q={quote(q)}&city=New%20Delhi&filter=&page_number=0&scroll_id=&per_page=10&types=sku,allopathy&sort=relevance&fetch_eta=true&is_city_serviceable=true",
            "headers": {
                "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept":          "application/json",
                "Referer":         "https://www.1mg.com/",
                "Origin":          "https://www.1mg.com",
                "Accept-Language": "en-IN,en;q=0.9",
            }
        },
        "netmeds": {
            "url": f"https://www.netmeds.com/prescriptions/search-results?q={quote(q)}",
            "headers": {
                "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept":          "application/json",
                "Referer":         "https://www.netmeds.com/",
                "x-domain":        "www.netmeds.com",
                "Accept-Language": "en-IN,en;q=0.9",
            }
        },
        "medkart": {
            "url": f"https://app.medkart.in/api/v2/search?q={quote(q)}&limit=10",
            "headers": {
                "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept":          "application/json",
                "Referer":         "https://www.medkart.in/",
                "Origin":          "https://www.medkart.in",
                "Accept-Language": "en-IN,en;q=0.9",
            }
        },
    }

    config = configs.get(pharmacy_name.lower())
    if not config:
        raise HTTPException(404, "Use: 1mg, netmeds, medkart")

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(config["url"], headers=config["headers"])

    def summarize(obj, depth=0):
        if depth > 4: return "..."
        if isinstance(obj, dict):
            return {k: summarize(v, depth+1) for k, v in list(obj.items())[:15]}
        elif isinstance(obj, list):
            return f"LIST[{len(obj)}] first={summarize(obj[0], depth+1) if obj else None}"
        return str(obj)[:100]

    try:
        data = r.json()
        return {
            "url":        config["url"],
            "status":     r.status_code,
            "structure":  summarize(data),
            "raw_sample": str(data)[:2000],
        }
    except Exception as e:
        return {
            "url":    config["url"],
            "status": r.status_code,
            "error":  str(e),
            "body":   r.text[:500],
        }


# ── Raw API debug v2 ──────────────────────────────────────────────────────────
@app.get("/api/rawdebug2/{pharmacy_name}")
async def raw_debug2(pharmacy_name: str, q: str = Query(..., min_length=1)):
    """Test fixed API calls for 1mg, netmeds, medkart"""
    import httpx
    from urllib.parse import quote

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

    results = []

    if pharmacy_name == "1mg":
        # 1mg needs city cookie — try with cookie header
        tests = [
            {
                "url": f"https://www.1mg.com/pwa-dweb-api/api/v4/search/all?q={quote(q)}&page_number=0&per_page=10&types=sku,allopathy&sort=relevance",
                "headers": {
                    "User-Agent": UA, "Accept": "application/json",
                    "Referer": "https://www.1mg.com/",
                    "Cookie": "city=Delhi; city_id=Delhi",
                }
            },
            {
                "url": f"https://www.1mg.com/pwa-dweb-api/api/v4/search/all?q={quote(q)}&city_id=Delhi&page_number=0&per_page=10&types=sku,allopathy&sort=relevance",
                "headers": {"User-Agent": UA, "Accept": "application/json", "Referer": "https://www.1mg.com/"}
            },
            {
                "url": f"https://www.1mg.com/pwa-dweb-api/api/v4/search/all?q={quote(q)}&city=Delhi&page_number=0&per_page=10&types=sku,allopathy&sort=relevance&fetch_eta=false",
                "headers": {"User-Agent": UA, "Accept": "application/json", "Referer": "https://www.1mg.com/",
                            "Cookie": "city=Delhi"}
            },
        ]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for t in tests:
                r = await client.get(t["url"], headers=t["headers"])
                try:
                    data = r.json()
                except Exception:
                    data = r.text[:200]
                results.append({"url": t["url"][:100], "status": r.status_code, "data": str(data)[:300]})

    elif pharmacy_name == "netmeds":
        # NetMeds needs auth token — first fetch page to get token
        tests = [
            # Try without token first
            f"https://www.netmeds.com/api/service/application/catalog/v1.0/search/?q={quote(q)}&page_no=1&page_size=5",
            # Try with x-location-detail header
            f"https://www.netmeds.com/api/service/application/catalog/v2.0/search/?q={quote(q)}&page_no=1&page_size=5",
            # Try Fynd direct
            f"https://api.fynd.com/service/application/catalog/v1.0/search/?q={quote(q)}&page_no=1&page_size=5",
        ]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # First get token from page
            page_r = await client.get("https://www.netmeds.com", headers={"User-Agent": UA})
            token_match = __import__('re').search(r'"application_token"\s*:\s*"([^"]+)"', page_r.text)
            token = token_match.group(1) if token_match else None

            for url in tests:
                h = {"User-Agent": UA, "Accept": "application/json", "Referer": "https://www.netmeds.com/"}
                if token:
                    h["x-application-token"] = token
                r = await client.get(url, headers=h)
                try:
                    data = r.json()
                except Exception:
                    data = r.text[:200]
                results.append({"url": url[:100], "status": r.status_code, "token_found": bool(token), "data": str(data)[:300]})

    elif pharmacy_name == "medkart":
        # MedKart — try different API paths
        tests = [
            f"https://app.medkart.in/api/v1/search?q={quote(q)}&limit=10",
            f"https://app.medkart.in/api/medicines/search?q={quote(q)}",
            f"https://app.medkart.in/api/v2/medicine/search?name={quote(q)}",
            f"https://app.medkart.in/api/v2/medicines?search={quote(q)}",
            f"https://www.medkart.in/api/search?q={quote(q)}",
        ]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for url in tests:
                h = {"User-Agent": UA, "Accept": "application/json",
                     "Referer": "https://www.medkart.in/", "Origin": "https://www.medkart.in"}
                r = await client.get(url, headers=h)
                try:
                    data = r.json()
                except Exception:
                    data = r.text[:200]
                results.append({"url": url[:100], "status": r.status_code, "data": str(data)[:300]})

    return {"pharmacy": pharmacy_name, "results": results}


# ── Playwright deep debug — shows ALL captured URLs ──────────────────────────
@app.get("/api/deepdebug/{pharmacy_name}")
async def deep_debug(pharmacy_name: str, q: str = Query(..., min_length=1)):
    """Shows every single response Playwright captures including non-JSON"""
    from extractor.network_extractor import extract_network_responses_debug

    urls = {
        "1mg":      f"https://www.1mg.com/search/all?name={q}",
        "netmeds":  f"https://www.netmeds.com/prescriptions/search-results?q={q}",
        "medkart":  f"https://www.medkart.in/search?q={q}",
    }

    url = urls.get(pharmacy_name.lower())
    if not url:
        raise HTTPException(404, "Use: 1mg, netmeds, medkart")

    result = await extract_network_responses_debug(url, wait_ms=9000)

    # Filter to show only interesting URLs — skip assets/analytics
    interesting = [
        u for u in result.get("all_urls_sample", [])
        if not any(skip in u.lower() for skip in [
            ".js", ".css", ".png", ".jpg", ".svg", ".woff",
            "google", "facebook", "analytics", "gtm", "sentry",
            "doubleclick", "rudder", "segment", "hotjar"
        ])
    ]

    return {
        "pharmacy":         pharmacy_name,
        "url":              url,
        "total_responses":  result["total_responses"],
        "json_responses":   result["json_responses"],
        "json_urls":        result["json_urls"],
        "interesting_urls": interesting[:30],
        "parsed_with_products": [
            r for r in result["parsed_responses"]
            if r.get("has_products")
        ],
        "all_parsed":       result["parsed_responses"][:20],
    }


# ── 1mg specific deep dive ────────────────────────────────────────────────────
@app.get("/api/1mgdebug")
async def mg_debug(q: str = Query(...)):
    """Capture 1mg search response and show full nested structure"""
    from playwright.async_api import async_playwright
    import json

    captured = None
    url = f"https://www.1mg.com/search/all?name={q}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"
        ])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            locale="en-IN", timezone_id="Asia/Kolkata"
        )

        async def handle(response):
            nonlocal captured
            if "search/all" in response.url and "pwa-dweb-api" in response.url:
                try:
                    captured = await response.json()
                except Exception:
                    pass

        page = await context.new_page()
        page.on("response", handle)
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)
        await browser.close()

    if not captured:
        return {"error": "No search API response captured"}

    # Show full structure recursively
    def show(obj, depth=0, max_depth=5):
        if depth > max_depth: return "..."
        if isinstance(obj, dict):
            return {k: show(v, depth+1) for k, v in list(obj.items())[:20]}
        elif isinstance(obj, list):
            if len(obj) > 0 and isinstance(obj[0], dict):
                return f"LIST[{len(obj)}] keys={list(obj[0].keys())[:15]} sample={show(obj[0], depth+1)}"
            return f"LIST[{len(obj)}] = {str(obj[:2])[:200]}"
        return str(obj)[:150]

    return {
        "url_captured": "1mg search/all",
        "top_keys":     list(captured.keys()),
        "structure":    show(captured),
    }

# ── Static frontend ───────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse("static/index.html")
