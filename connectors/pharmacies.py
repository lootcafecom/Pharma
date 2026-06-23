"""
Pharmacy Connectors — Dual Strategy
=====================================
Strategy 1: Direct JSON API calls (fast, no browser needed)
Strategy 2: Playwright network capture (fallback for complex sites)

We try Strategy 1 first — if it works, great.
If not, fall back to Playwright.
"""
import re
import json
import httpx
import asyncio
from extractor.network_extractor import (
    extract_network_responses,
    find_products_in_json,
    extract_price_fields,
    extract_name_fields,
    extract_slug_fields,
    safe_float,
)

# ── Shared HTTP headers ───────────────────────────────────────────────────────
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
MOBILE_UA  = "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 Chrome/112.0.0.0 Mobile Safari/537.36"

BASE_HEADERS = {
    "User-Agent":      DESKTOP_UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

JSON_HEADERS = {
    **BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
}

def make_product(name, price, mrp, link) -> dict | None:
    price = safe_float(price)
    mrp   = safe_float(mrp) or price
    if not name or price <= 0:
        return None
    # Shopify stores in paise
    if price > 50000:
        price /= 100
        mrp   /= 100
    return {
        "name":     str(name).strip(),
        "price":    round(price, 2),
        "mrp":      round(mrp, 2),
        "discount": round((1 - price/mrp)*100) if mrp > price else 0,
        "link":     link,
        "inStock":  True,
    }

def build_from_list(items: list, pharmacy: str, url: str, slug_base: str) -> list:
    products = []
    for p in items[:5]:
        name       = extract_name_fields(p)
        price, mrp = extract_price_fields(p)
        slug       = extract_slug_fields(p)
        link       = f"{slug_base}{slug}" if slug else url
        prod       = make_product(name, price, mrp, link)
        if prod:
            products.append(prod)
    return products

async def playwright_fallback(pharmacy: str, url: str, slug_base: str, pincode: str = None) -> dict:
    """Use Playwright as fallback when direct API fails"""
    async def actions(page):
        if pincode:
            try:
                await page.evaluate(f"localStorage.setItem('pincode', '{pincode}')")
            except Exception:
                pass
        await page.evaluate("window.scrollTo(0, 400)")
        await page.wait_for_timeout(2000)

    responses = await extract_network_responses(url, wait_ms=7000, extra_actions=actions)

    products = []
    for resp in responses:
        items = find_products_in_json(resp["data"])
        if items:
            products = build_from_list(items, pharmacy, url, slug_base)
            if products:
                break

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found",
    }


# ── PharmEasy ─────────────────────────────────────────────────────────────────
async def connect_pharmeasy(query: str, pincode: str = None) -> dict:
    pharmacy  = "PharmEasy"
    url       = f"https://pharmeasy.in/search/all?name={query}"
    slug_base = "https://pharmeasy.in/medicines/all/"

    # Strategy 1: Direct API — PharmEasy __NEXT_DATA__ is reliable
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r    = await client.get(url, headers=BASE_HEADERS)
            html = r.text

            # Extract __NEXT_DATA__
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
                pp   = data.get("props", {}).get("pageProps", {})
                lst  = pp.get("productList") or pp.get("searchResult", {}).get("products") or []
                if lst:
                    products = []
                    for p in lst[:5]:
                        name  = p.get("name") or p.get("productName") or ""
                        price = p.get("salePriceDecimal") or p.get("sellingPrice") or p.get("price")
                        mrp   = p.get("mrpDecimal") or p.get("mrp") or price
                        slug  = p.get("slug") or p.get("urlKey") or ""
                        prod  = make_product(name, price, mrp, f"{slug_base}{slug}")
                        if prod:
                            products.append(prod)
                    if products:
                        return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None}
    except Exception:
        pass

    # Strategy 2: Playwright fallback
    return await playwright_fallback(pharmacy, url, slug_base, pincode)


# ── 1mg ───────────────────────────────────────────────────────────────────────
async def connect_1mg(query: str, pincode: str = None) -> dict:
    pharmacy  = "1mg"
    url       = f"https://www.1mg.com/search/all?name={query}"
    slug_base = "https://www.1mg.com/drugs/"

    # Strategy 1: Internal pharmacy_api_gateway
    api_urls = [
        f"https://www.1mg.com/pharmacy_api_gateway/v4/drug_skus/search_by_name?name={query}&page=1&per_page=10",
        f"https://www.1mg.com/pharmacy_api_gateway/v4/drug_skus/search_by_name?name={query}&page=1&per_page=10&lang=en",
    ]
    h = {
        **JSON_HEADERS,
        "Referer":       "https://www.1mg.com/",
        "Origin":        "https://www.1mg.com",
        "x-app-version": "2.0.3",
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for api in api_urls:
                r = await client.get(api, headers=h)
                if r.status_code == 200:
                    data = r.json()
                    raw  = (
                        data.get("data", {}).get("skus") or
                        data.get("data", {}).get("products") or
                        data.get("skus") or data.get("products") or []
                    )
                    if raw:
                        products = []
                        for p in raw[:5]:
                            name  = p.get("name") or p.get("product_name") or ""
                            price = p.get("selling_price") or p.get("price") or p.get("salePriceDecimal")
                            mrp   = p.get("mrp") or p.get("mrpDecimal") or price
                            slug  = p.get("slug") or p.get("url_key") or str(p.get("sku_id") or "")
                            prod  = make_product(name, price, mrp, f"{slug_base}{slug}")
                            if prod:
                                products.append(prod)
                        if products:
                            return {"pharmacy": pharmacy, "products": products, "searchUrl": api, "error": None}
    except Exception:
        pass

    # Strategy 2: Playwright
    return await playwright_fallback(pharmacy, url, slug_base, pincode)


# ── NetMeds ───────────────────────────────────────────────────────────────────
async def connect_netmeds(query: str, pincode: str = None) -> dict:
    pharmacy  = "NetMeds"
    url       = f"https://www.netmeds.com/catalogsearch/result?q={query}"
    slug_base = "https://www.netmeds.com/prescriptions/"

    # Strategy 1: NetMeds Fynd API
    api_urls = [
        f"https://www.netmeds.com/api/v1/listing/products?q={query}&limit=5&page_no=1",
        f"https://www.netmeds.com/api/search?q={query}&limit=5",
        f"https://www.netmeds.com/api/v1/search?q={query}",
    ]
    h = {**JSON_HEADERS, "Referer": "https://www.netmeds.com/"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for api in api_urls:
                r = await client.get(api, headers=h)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data  = r.json()
                    items = find_products_in_json(data)
                    if items:
                        products = build_from_list(items, pharmacy, url, slug_base)
                        if products:
                            return {"pharmacy": pharmacy, "products": products, "searchUrl": api, "error": None}
    except Exception:
        pass

    # Strategy 2: Playwright
    return await playwright_fallback(pharmacy, url, slug_base, pincode)


# ── Apollo Pharmacy ───────────────────────────────────────────────────────────
async def connect_apollo(query: str, pincode: str = None) -> dict:
    pharmacy  = "Apollo Pharmacy"
    url       = f"https://www.apollopharmacy.in/search-medicines/{query}"
    slug_base = "https://www.apollopharmacy.in/medicine/"

    # Apollo works with Playwright — go straight to it
    # But try direct API first
    api_urls = [
        f"https://www.apollopharmacy.in/api/product/search?q={query}&limit=5",
        f"https://www.apollopharmacy.in/api/v1/search?q={query}",
    ]
    h = {**JSON_HEADERS, "Referer": "https://www.apollopharmacy.in/"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for api in api_urls:
                r = await client.get(api, headers=h)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data  = r.json()
                    items = find_products_in_json(data)
                    if items:
                        products = build_from_list(items, pharmacy, url, slug_base)
                        if products:
                            return {"pharmacy": pharmacy, "products": products, "searchUrl": api, "error": None}
    except Exception:
        pass

    # Apollo works well with Playwright — use it
    async def apollo_actions(page):
        if pincode:
            try:
                await page.evaluate(f"""
                    localStorage.setItem('pincode', '{pincode}');
                    localStorage.setItem('userPincode', '{pincode}');
                """)
            except Exception:
                pass
        # Wait for product cards to appear
        try:
            await page.wait_for_selector(
                "[class*='ProductCard'], [class*='medicine-card'], [class*='MedicineCard'], [class*='product-card']",
                timeout=10000
            )
        except Exception:
            await page.wait_for_timeout(3000)

    responses = await extract_network_responses(url, wait_ms=8000, extra_actions=apollo_actions)
    products  = []
    for resp in responses:
        items = find_products_in_json(resp["data"])
        if items:
            products = build_from_list(items, pharmacy, url, slug_base)
            if products:
                break

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found",
    }


# ── MedKart ───────────────────────────────────────────────────────────────────
async def connect_medkart(query: str, pincode: str = None) -> dict:
    pharmacy  = "MedKart"
    url       = f"https://medkart.in/search?q={query}"
    slug_base = "https://medkart.in/products/"

    # Strategy 1: Shopify API endpoints
    api_urls = [
        f"https://medkart.in/search?type=product&q={query}&view=json",
        f"https://medkart.in/search.json?type=product&q={query}&limit=5",
        f"https://medkart.in/api/search?q={query}",
    ]
    h = {**JSON_HEADERS, "Referer": "https://medkart.in/"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for api in api_urls:
                r = await client.get(api, headers=h)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data  = r.json()
                    items = find_products_in_json(data)
                    if items:
                        products = build_from_list(items, pharmacy, url, slug_base)
                        if products:
                            return {"pharmacy": pharmacy, "products": products, "searchUrl": api, "error": None}
    except Exception:
        pass

    # Strategy 2: Playwright
    return await playwright_fallback(pharmacy, url, slug_base, pincode)
