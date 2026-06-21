"""
Pharmacy Connectors — Playwright Network Capture
=================================================
Each connector:
1. Opens the pharmacy search page in real Chromium browser
2. Intercepts all XHR/Fetch network responses
3. Finds the response containing product/price data
4. Returns normalized product list

This approach works because:
- Browser looks like a real user (bypasses 403, anti-bot)
- Captures actual JSON API calls (no HTML parsing needed)
- Works regardless of React/Vue/Next.js framework
"""
import asyncio
from extractor.network_extractor import (
    extract_network_responses,
    find_products_in_json,
    extract_price_fields,
    extract_name_fields,
    extract_slug_fields,
    safe_float,
)

def make_product(name, price, mrp, link, pharmacy) -> dict | None:
    price = safe_float(price)
    mrp   = safe_float(mrp) or price
    if not name or price <= 0:
        return None
    # Shopify stores prices in paise
    if price > 50000:
        price = price / 100
        mrp   = mrp / 100
    return {
        "name":     name.strip(),
        "price":    round(price, 2),
        "mrp":      round(mrp, 2),
        "discount": round((1 - price/mrp)*100) if mrp > price else 0,
        "link":     link,
        "inStock":  True,
    }


def build_products(responses: list[dict], pharmacy: str, base_url: str, slug_prefix: str) -> list[dict]:
    """
    Given captured network responses, find and return product list.
    Tries each response until products are found.
    """
    for resp in responses:
        raw_list = find_products_in_json(resp["data"])
        if not raw_list:
            continue

        products = []
        for p in raw_list[:5]:
            name      = extract_name_fields(p)
            price, mrp = extract_price_fields(p)
            slug      = extract_slug_fields(p)
            link      = f"{slug_prefix}{slug}" if slug else base_url
            prod      = make_product(name, price, mrp, link, pharmacy)
            if prod:
                products.append(prod)

        if products:
            return products

    return []


# ── PharmEasy ─────────────────────────────────────────────────────────────────
async def connect_pharmeasy(query: str, pincode: str = None) -> dict:
    pharmacy = "PharmEasy"
    url      = f"https://pharmeasy.in/search/all?name={query}"

    async def set_pincode(page):
        if pincode:
            try:
                # PharmEasy stores pincode in localStorage
                await page.evaluate(f"localStorage.setItem('pincode', '{pincode}')")
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
            except Exception:
                pass

    responses = await extract_network_responses(url, wait_ms=5000, extra_actions=set_pincode)
    products  = build_products(responses, pharmacy, url, "https://pharmeasy.in/medicines/all/")

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found in network responses",
    }


# ── 1mg ───────────────────────────────────────────────────────────────────────
async def connect_1mg(query: str, pincode: str = None) -> dict:
    pharmacy = "1mg"
    url      = f"https://www.1mg.com/search/all?name={query}"

    async def set_pincode(page):
        if pincode:
            try:
                await page.evaluate(f"""
                    localStorage.setItem('pincode', '{pincode}');
                    document.cookie = 'pincode={pincode}; path=/';
                """)
                await page.wait_for_timeout(1000)
            except Exception:
                pass
        # Scroll to trigger lazy loading
        await page.evaluate("window.scrollTo(0, 300)")
        await page.wait_for_timeout(1000)

    responses = await extract_network_responses(url, wait_ms=6000, extra_actions=set_pincode)
    products  = build_products(responses, pharmacy, url, "https://www.1mg.com/drugs/")

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found in network responses",
    }


# ── NetMeds ───────────────────────────────────────────────────────────────────
async def connect_netmeds(query: str, pincode: str = None) -> dict:
    pharmacy = "NetMeds"
    url      = f"https://www.netmeds.com/catalogsearch/result?q={query}"

    async def actions(page):
        # Scroll to trigger product loading
        await page.evaluate("window.scrollTo(0, 500)")
        await page.wait_for_timeout(2000)

    responses = await extract_network_responses(url, wait_ms=6000, extra_actions=actions)
    products  = build_products(responses, pharmacy, url, "https://www.netmeds.com/prescriptions/")

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found in network responses",
    }


# ── Apollo Pharmacy ───────────────────────────────────────────────────────────
async def connect_apollo(query: str, pincode: str = None) -> dict:
    pharmacy = "Apollo Pharmacy"
    url      = f"https://www.apollopharmacy.in/search-medicines/{query}"

    async def set_pincode(page):
        if pincode:
            try:
                # Apollo uses pincode in cookies and localStorage
                await page.evaluate(f"""
                    localStorage.setItem('pincode', '{pincode}');
                    localStorage.setItem('userPincode', '{pincode}');
                    document.cookie = 'pincode={pincode}; path=/';
                """)
                await page.wait_for_timeout(500)
            except Exception:
                pass
        # Wait for Apollo to load products
        try:
            await page.wait_for_selector("[class*='ProductCard'], [class*='medicine-card'], [class*='MedicineCard']", timeout=8000)
        except Exception:
            pass

    responses = await extract_network_responses(url, wait_ms=7000, extra_actions=set_pincode)
    products  = build_products(responses, pharmacy, url, "https://www.apollopharmacy.in/medicine/")

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found in network responses",
    }


# ── MedKart ───────────────────────────────────────────────────────────────────
async def connect_medkart(query: str, pincode: str = None) -> dict:
    pharmacy = "MedKart"
    url      = f"https://medkart.in/search?q={query}"

    async def actions(page):
        await page.evaluate("window.scrollTo(0, 300)")
        await page.wait_for_timeout(2000)

    responses = await extract_network_responses(url, wait_ms=6000, extra_actions=actions)
    products  = build_products(responses, pharmacy, url, "https://medkart.in/products/")

    return {
        "pharmacy":  pharmacy,
        "products":  products,
        "searchUrl": url,
        "error":     None if products else "No products found in network responses",
    }
