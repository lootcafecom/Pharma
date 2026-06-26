"""
Pharmacy Connectors — Shared Browser Pool Edition
===================================================
All pharmacies use a single shared Playwright browser.
Each gets its own context (isolated session/cookies).
This prevents RAM overflow and context conflicts on Railway.

Parsing strategy per pharmacy:
  PharmEasy : __NEXT_DATA__ SSR JSON (direct HTTP, no browser)
  1mg       : data.search_results (prices as "₹32.13" strings)
  NetMeds   : Fynd catalog API captured via browser
  Apollo    : XHR capture via browser
  MedKart   : XHR capture via browser
"""
import re
import json
import httpx
from extractor.browser_pool import capture_pharmacy
from extractor.network_extractor import find_products_in_json, safe_float

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
BASE_HEADERS = {
    "User-Agent":      DESKTOP_UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

def parse_price_str(s) -> float:
    """Parse price strings like '₹32.13' or '29' or 32.0"""
    if s is None: return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(s))
    try: return float(cleaned)
    except: return 0.0

def make_product(name, price, mrp, link) -> dict | None:
    price = safe_float(price) if isinstance(price, (int, float)) else parse_price_str(price)
    mrp   = safe_float(mrp)   if isinstance(mrp,   (int, float)) else parse_price_str(mrp)
    mrp   = mrp or price
    if not name or price <= 0: return None
    if price > 50000: price /= 100
    if mrp   > 50000: mrp   /= 100
    return {
        "name":     str(name).strip(),
        "price":    round(price, 2),
        "mrp":      round(mrp,   2),
        "discount": round((1 - price/mrp)*100) if mrp > price else 0,
        "link":     link,
        "inStock":  True,
    }

def _name(p):
    for f in ["name","productName","product_name","medicineName","title","display_name","skuName"]:
        v = p.get(f)
        if v and isinstance(v, str) and len(v) > 2: return v.strip()
    return ""

def _price(p):
    for f in ["selling_price","sellingPrice","salePriceDecimal","offerPrice","price","discountedPrice"]:
        v = p.get(f)
        if v is not None:
            val = parse_price_str(v)
            if val > 0: return val
    return 0.0

def _mrp(p, price):
    for f in ["mrp","MRP","mrpPrice","mrpDecimal","max_retail_price","marked_price","maxPrice"]:
        v = p.get(f)
        if v is not None:
            val = parse_price_str(v)
            if val > 0: return val
    return price

def _slug(p):
    for f in ["slug","urlKey","url_key","handle","sku","productId","product_id","id","skuId"]:
        v = p.get(f)
        if v: return str(v)
    return ""

def build_from_responses(responses, pharmacy, base_url, slug_base) -> list:
    """Generic builder from XHR responses"""
    for resp in responses:
        items = find_products_in_json(resp.get("data", {}))
        if not items: continue
        products = []
        for p in items[:5]:
            name  = _name(p)
            price = _price(p)
            mrp   = _mrp(p, price)
            slug  = _slug(p)
            link  = f"{slug_base}{slug}" if slug else base_url
            prod  = make_product(name, price, mrp, link)
            if prod: products.append(prod)
        if products: return products
    return []


# ── PharmEasy — Direct HTTP + __NEXT_DATA__ ───────────────────────────────────
async def connect_pharmeasy(query: str, pincode: str = None) -> dict:
    pharmacy = "PharmEasy"
    url      = f"https://pharmeasy.in/search/all?name={query}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r    = await client.get(url, headers=BASE_HEADERS)
            m    = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
            if m:
                pp  = json.loads(m.group(1)).get("props",{}).get("pageProps",{})
                lst = pp.get("productList") or pp.get("searchResult",{}).get("products") or []
                products = []
                for p in lst[:5]:
                    name  = p.get("name") or p.get("productName") or ""
                    price = p.get("salePriceDecimal") or p.get("sellingPrice") or p.get("price")
                    mrp   = p.get("mrpDecimal") or p.get("mrp") or price
                    slug  = p.get("slug") or p.get("urlKey") or ""
                    prod  = make_product(name, price, mrp, f"https://pharmeasy.in/medicines/all/{slug}")
                    if prod: products.append(prod)
                if products:
                    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None}
    except Exception:
        pass
    # Browser fallback
    responses = await capture_pharmacy(url, wait_ms=6000)
    products  = build_from_responses(responses, pharmacy, url, "https://pharmeasy.in/medicines/all/")
    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── 1mg — Browser capture + custom parser for search_results ─────────────────
async def connect_1mg(query: str, pincode: str = None) -> dict:
    pharmacy = "1mg"
    url      = f"https://www.1mg.com/search/all?name={query}"
    captured = {}

    async def scroll(page):
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 300)")
        await page.wait_for_timeout(1000)

    responses = await capture_pharmacy(url, wait_ms=7000, actions=scroll)

    # Find the search/all response specifically
    for resp in responses:
        resp_url = resp.get("url", "")
        if "search/all" in resp_url and "pwa-dweb-api" in resp_url:
            captured = resp.get("data", {})
            break

    # Parse 1mg specific structure: data.search_results
    # Prices are strings like "₹32.13"
    products = []
    items    = captured.get("data", {}).get("search_results", [])

    for item in items[:5]:
        name   = item.get("name", "")
        prices = item.get("prices", {})
        price  = parse_price_str(prices.get("discounted_price") or prices.get("mrp"))
        mrp    = parse_price_str(prices.get("mrp") or prices.get("discounted_price"))
        path   = item.get("url", "")
        link   = f"https://www.1mg.com{path}" if path else url
        prod   = make_product(name, price, mrp, link)
        if prod: products.append(prod)

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── NetMeds — Browser capture with correct URL ────────────────────────────────
async def connect_netmeds(query: str, pincode: str = None) -> dict:
    pharmacy = "NetMeds"
    url      = f"https://www.netmeds.com/prescriptions/search-results?q={query}"

    async def scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    responses = await capture_pharmacy(url, wait_ms=8000, actions=scroll)
    products  = build_from_responses(responses, pharmacy, url, "https://www.netmeds.com/prescriptions/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── Apollo — Browser capture (was working) ────────────────────────────────────
async def connect_apollo(query: str, pincode: str = None) -> dict:
    pharmacy = "Apollo Pharmacy"
    url      = f"https://www.apollopharmacy.in/search-medicines/{query}"

    async def wait_load(page):
        if pincode:
            try:
                await page.evaluate(f"localStorage.setItem('pincode', '{pincode}')")
            except Exception:
                pass
        try:
            await page.wait_for_selector(
                "[class*='ProductCard'],[class*='medicine-card'],[class*='MedicineCard']",
                timeout=8000
            )
        except Exception:
            await page.wait_for_timeout(4000)

    responses = await capture_pharmacy(url, wait_ms=8000, actions=wait_load)
    products  = build_from_responses(responses, pharmacy, url, "https://www.apollopharmacy.in/medicine/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── MedKart — Browser capture with scroll ─────────────────────────────────────
async def connect_medkart(query: str, pincode: str = None) -> dict:
    pharmacy = "MedKart"
    url      = f"https://www.medkart.in/search?q={query}"

    async def scroll(page):
        await page.wait_for_timeout(3000)
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)

    responses = await capture_pharmacy(url, wait_ms=8000, actions=scroll)
    products  = build_from_responses(responses, pharmacy, url, "https://www.medkart.in/order-medicine/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}
