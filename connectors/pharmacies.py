"""
Pharmacy Connectors — Final Version
=====================================
PharmEasy : __NEXT_DATA__ SSR (working)
Apollo    : Playwright XHR (working)
1mg       : Playwright — needs browser session for city cookie
NetMeds   : Playwright — needs browser session for auth token
MedKart   : Playwright — needs browser session for API access
"""
import re
import json
import httpx
from urllib.parse import quote
from extractor.network_extractor import (
    extract_network_responses,
    find_products_in_json,
    safe_float,
)

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"

BASE_HEADERS = {
    "User-Agent":      DESKTOP_UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

def make_product(name, price, mrp, link) -> dict | None:
    price = safe_float(price)
    mrp   = safe_float(mrp) or price
    if not name or price <= 0:
        return None
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

def _name(p):
    for f in ["name","productName","product_name","medicineName","title","display_name","skuName"]:
        v = p.get(f)
        if v and isinstance(v, str) and len(v) > 2:
            return v.strip()
    return ""

def _price(p):
    for f in ["selling_price","sellingPrice","salePriceDecimal","offerPrice","price","discountedPrice","effective_price","net_price"]:
        v = p.get(f)
        if v is not None:
            val = safe_float(v)
            if val > 0: return val
    return 0.0

def _mrp(p, price):
    for f in ["mrp","MRP","mrpPrice","mrpDecimal","max_retail_price","marked_price","maxPrice","compare_at_price"]:
        v = p.get(f)
        if v is not None:
            val = safe_float(v)
            if val > 0: return val
    return price

def _slug(p):
    for f in ["slug","urlKey","url_key","handle","sku","productId","product_id","id","skuId"]:
        v = p.get(f)
        if v: return str(v)
    return ""

def build_products(responses, pharmacy, base_url, slug_base) -> list:
    products = []
    for resp in responses:
        items = find_products_in_json(resp["data"])
        if not items:
            continue
        for p in items[:5]:
            name  = _name(p)
            price = _price(p)
            mrp   = _mrp(p, price)
            slug  = _slug(p)
            link  = f"{slug_base}{slug}" if slug else base_url
            prod  = make_product(name, price, mrp, link)
            if prod:
                products.append(prod)
        if products:
            break
    return products


# ── PharmEasy — SSR __NEXT_DATA__ (working) ───────────────────────────────────
async def connect_pharmeasy(query: str, pincode: str = None) -> dict:
    pharmacy  = "PharmEasy"
    url       = f"https://pharmeasy.in/search/all?name={query}"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r    = await client.get(url, headers=BASE_HEADERS)
            html = r.text
            m    = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                data     = json.loads(m.group(1))
                pp       = data.get("props", {}).get("pageProps", {})
                lst      = pp.get("productList") or pp.get("searchResult", {}).get("products") or []
                products = []
                for p in lst[:5]:
                    name  = p.get("name") or p.get("productName") or ""
                    price = p.get("salePriceDecimal") or p.get("sellingPrice") or p.get("price")
                    mrp   = p.get("mrpDecimal") or p.get("mrp") or price
                    slug  = p.get("slug") or p.get("urlKey") or ""
                    prod  = make_product(name, price, mrp, f"https://pharmeasy.in/medicines/all/{slug}")
                    if prod:
                        products.append(prod)
                if products:
                    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None}
    except Exception:
        pass

    # Playwright fallback
    responses = await extract_network_responses(url, wait_ms=6000)
    products  = build_products(responses, pharmacy, url, "https://pharmeasy.in/medicines/all/")
    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── 1mg — Playwright with city session ────────────────────────────────────────
async def connect_1mg(query: str, pincode: str = None) -> dict:
    pharmacy  = "1mg"
    url       = f"https://www.1mg.com/search/all?name={query}"

    # 1mg requires city cookie set by browser session
    # Playwright will automatically handle cookies and session
    async def setup_session(page):
        # Wait for 1mg to set city cookie automatically
        await page.wait_for_timeout(2000)
        # Try to set city via localStorage as fallback
        try:
            await page.evaluate("""
                localStorage.setItem('city', 'New Delhi');
                localStorage.setItem('city_id', '3');
                localStorage.setItem('pincode', '110001');
            """)
        except Exception:
            pass
        # Scroll to trigger product loading
        await page.evaluate("window.scrollTo(0, 300)")
        await page.wait_for_timeout(2000)

    responses = await extract_network_responses(url, wait_ms=8000, extra_actions=setup_session)
    products  = build_products(responses, pharmacy, url, "https://www.1mg.com/drugs/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── NetMeds — Playwright with auth token session ──────────────────────────────
async def connect_netmeds(query: str, pincode: str = None) -> dict:
    pharmacy  = "NetMeds"
    # Use correct search URL — catalogsearch returns 404
    url       = f"https://www.netmeds.com/prescriptions/search-results?q={query}"

    async def wait_for_products(page):
        # Wait for Fynd to initialize and load auth token
        await page.wait_for_timeout(3000)
        # Try to trigger search explicitly
        try:
            await page.evaluate(f"window.location.href = 'https://www.netmeds.com/prescriptions/search-results?q={query}'")
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    responses = await extract_network_responses(url, wait_ms=9000, extra_actions=wait_for_products)
    products  = build_products(responses, pharmacy, url, "https://www.netmeds.com/prescriptions/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── Apollo — Playwright (working) ─────────────────────────────────────────────
async def connect_apollo(query: str, pincode: str = None) -> dict:
    pharmacy  = "Apollo Pharmacy"
    url       = f"https://www.apollopharmacy.in/search-medicines/{query}"

    async def apollo_actions(page):
        if pincode:
            try:
                await page.evaluate(f"""
                    localStorage.setItem('pincode', '{pincode}');
                    localStorage.setItem('userPincode', '{pincode}');
                """)
            except Exception:
                pass
        try:
            await page.wait_for_selector(
                "[class*='ProductCard'],[class*='medicine-card'],[class*='MedicineCard'],[class*='product-card']",
                timeout=10000
            )
        except Exception:
            await page.wait_for_timeout(4000)

    responses = await extract_network_responses(url, wait_ms=8000, extra_actions=apollo_actions)
    products  = build_products(responses, pharmacy, url, "https://www.apollopharmacy.in/medicine/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}


# ── MedKart — Playwright with extended wait ────────────────────────────────────
async def connect_medkart(query: str, pincode: str = None) -> dict:
    pharmacy  = "MedKart"
    url       = f"https://www.medkart.in/search?q={query}"

    async def medkart_actions(page):
        # Wait for Next.js App Router to load
        await page.wait_for_timeout(3000)
        # Scroll to trigger lazy loading
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)
        # Scroll back up
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1000)

    responses = await extract_network_responses(url, wait_ms=9000, extra_actions=medkart_actions)
    products  = build_products(responses, pharmacy, url, "https://www.medkart.in/order-medicine/")

    return {"pharmacy": pharmacy, "products": products, "searchUrl": url, "error": None if products else "No products found"}
