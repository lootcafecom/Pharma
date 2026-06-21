"""
Network Extractor — Core Engine
================================
Instead of reading HTML, we open a real browser and
intercept all XHR/Fetch network responses.

This captures the actual JSON API calls pharmacies make
internally to load product data — bypassing all anti-bot,
JS rendering, and React/Vue issues.
"""
import asyncio
import json
import re
from typing import Callable
from playwright.async_api import async_playwright, Page, Response, BrowserContext

# Keywords that indicate a response contains product/search data
SEARCH_KEYWORDS = [
    "search", "product", "catalog", "drug", "sku",
    "medicine", "listing", "item", "result", "query"
]

# Keywords to skip — tracking, analytics, ads
SKIP_KEYWORDS = [
    "analytics", "tracking", "gtm", "facebook", "google-analytics",
    "hotjar", "segment", "mixpanel", "clarity", "amplitude",
    "ads", "pixel", "beacon", "log", "event"
]

def is_relevant_url(url: str) -> bool:
    url_lower = url.lower()
    if any(skip in url_lower for skip in SKIP_KEYWORDS):
        return False
    if any(kw in url_lower for kw in SEARCH_KEYWORDS):
        return True
    # Also capture API calls
    if "/api/" in url_lower or ".json" in url_lower:
        return True
    return False

def has_product_data(data: any) -> bool:
    """Check if JSON response likely contains product/price data"""
    if not data:
        return False
    text = json.dumps(data).lower()
    return any(kw in text for kw in [
        "price", "mrp", "sellingprice", "saleprice",
        "selling_price", "offerprice", "productname",
        "medicine", "drug", "tablet", "capsule"
    ])


async def extract_network_responses(
    url: str,
    wait_ms: int = 5000,
    extra_actions: Callable = None,
) -> list[dict]:
    """
    Open a URL in a headless browser, capture all relevant
    XHR/Fetch network responses, and return them as a list.

    Args:
        url: The pharmacy search URL to open
        wait_ms: How long to wait for network calls (ms)
        extra_actions: Optional async function to perform on page
                      (e.g. scroll, click, set pincode)
    """
    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
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
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        # Intercept responses
        async def handle_response(response: Response):
            try:
                url_r = response.url
                if not is_relevant_url(url_r):
                    return
                ct = response.headers.get("content-type", "")
                if "json" not in ct and "javascript" not in ct:
                    return
                if response.status not in (200, 201):
                    return
                data = await response.json()
                if has_product_data(data):
                    captured.append({
                        "url":  url_r,
                        "data": data,
                    })
            except Exception:
                pass

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Perform any extra actions (scroll, set pincode etc)
            if extra_actions:
                await extra_actions(page)

            # Wait for XHR calls to complete
            await page.wait_for_timeout(wait_ms)

        except Exception:
            pass
        finally:
            await browser.close()

    return captured


def safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except Exception:
        return default


def find_products_in_json(data: any, depth: int = 0) -> list[dict]:
    """
    Recursively search JSON for product arrays.
    Returns the first list that looks like products.
    """
    if depth > 6:
        return []

    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, dict):
            keys = set(k.lower() for k in first.keys())
            # Check if this looks like a product
            price_keys = {"price", "mrp", "sellingprice", "saleprice", "selling_price", "offerprice"}
            name_keys  = {"name", "productname", "title", "medicinename", "display_name"}
            if price_keys & keys and name_keys & keys:
                return data
        return []

    if isinstance(data, dict):
        # Common product list keys
        priority_keys = [
            "products", "skus", "items", "results", "data",
            "productList", "medicines", "drugs", "catalog",
            "searchResult", "hits", "records"
        ]
        for key in priority_keys:
            if key in data:
                result = find_products_in_json(data[key], depth + 1)
                if result:
                    return result
        # Recurse into all values
        for val in data.values():
            if isinstance(val, (dict, list)):
                result = find_products_in_json(val, depth + 1)
                if result:
                    return result

    return []


def extract_price_fields(product: dict) -> tuple[float, float]:
    """Extract price and MRP from a product dict"""
    price_fields = [
        "selling_price", "sellingPrice", "saleprice", "salePriceDecimal",
        "offerPrice", "offer_price", "price", "discountedPrice",
        "effective_price", "net_price"
    ]
    mrp_fields = [
        "mrp", "MRP", "mrpPrice", "mrpDecimal", "max_retail_price",
        "marked_price", "original_price", "maxPrice", "compare_at_price"
    ]

    price = 0.0
    for f in price_fields:
        v = product.get(f)
        if v is not None:
            price = safe_float(v)
            if price > 0:
                break

    mrp = 0.0
    for f in mrp_fields:
        v = product.get(f)
        if v is not None:
            mrp = safe_float(v)
            if mrp > 0:
                break

    return price, mrp or price


def extract_name_fields(product: dict) -> str:
    name_fields = [
        "name", "productName", "product_name", "medicineName",
        "medicine_name", "title", "display_name", "drugName"
    ]
    for f in name_fields:
        v = product.get(f)
        if v and isinstance(v, str) and len(v) > 2:
            return v.strip()
    return ""


def extract_slug_fields(product: dict) -> str:
    slug_fields = [
        "slug", "urlKey", "url_key", "handle", "sku",
        "productId", "product_id", "id", "skuId"
    ]
    for f in slug_fields:
        v = product.get(f)
        if v:
            return str(v)
    return ""
