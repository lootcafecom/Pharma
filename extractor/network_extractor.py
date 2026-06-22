"""
Network Extractor — Core Engine
================================
Opens pharmacy pages in real Chromium browser and intercepts
all XHR/Fetch network responses to capture product JSON data.
"""
import asyncio
import json
import re
from typing import Callable
from playwright.async_api import async_playwright, Response

# Keywords that indicate useful API responses
SEARCH_KEYWORDS = [
    "search", "product", "catalog", "drug", "sku",
    "medicine", "listing", "item", "result", "query",
    "/api/", ".json", "autocomplete", "suggestion"
]

# Skip these — analytics, ads, tracking
SKIP_KEYWORDS = [
    "analytics", "tracking", "gtm", "facebook", "google-analytics",
    "hotjar", "segment", "mixpanel", "clarity", "amplitude",
    "ads", "pixel", "beacon", "log", "event", "sentry",
    "datadog", "newrelic", "bugsnag", "rollbar"
]

def is_relevant_url(url: str) -> bool:
    url_lower = url.lower()
    if any(skip in url_lower for skip in SKIP_KEYWORDS):
        return False
    if any(kw in url_lower for kw in SEARCH_KEYWORDS):
        return True
    return False

def has_product_data(data: any) -> bool:
    if not data:
        return False
    try:
        text = json.dumps(data).lower()
        return any(kw in text for kw in [
            "price", "mrp", "sellingprice", "saleprice",
            "selling_price", "offerprice", "productname",
            "medicine", "drug", "tablet", "capsule"
        ])
    except Exception:
        return False


async def extract_network_responses(
    url: str,
    wait_ms: int = 6000,
    extra_actions: Callable = None,
) -> list[dict]:
    """
    Open URL in headless Chromium, capture ALL JSON network responses.
    Returns list of {url, data} dicts.
    """
    captured     = []
    all_responses = []  # For debugging — captures everything

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
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-mobile":"?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )

        # Block images and fonts to speed up loading
        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}", lambda route: route.abort())

        async def handle_response(response: Response):
            try:
                resp_url = response.url
                status   = response.status

                # Track ALL responses for debug
                all_responses.append({
                    "url":    resp_url,
                    "status": status,
                    "ct":     response.headers.get("content-type", ""),
                })

                if status not in (200, 201):
                    return

                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return

                data = await response.json()
                if data:
                    captured.append({
                        "url":  resp_url,
                        "data": data,
                    })

            except Exception:
                pass

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            try:
                # Fallback — just wait for domcontentloaded
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass

        # Extra actions (scroll, set pincode etc)
        if extra_actions:
            try:
                await extra_actions(page)
            except Exception:
                pass

        # Wait for async XHR calls
        await page.wait_for_timeout(wait_ms)

        # Store all_responses on context for debug access
        context._all_responses = all_responses

        await browser.close()

    return captured


async def extract_network_responses_debug(
    url: str,
    wait_ms: int = 6000,
) -> dict:
    """
    Debug version — returns ALL captured responses with full details.
    """
    captured      = []
    all_json_urls = []
    all_urls      = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        await context.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}", lambda route: route.abort())

        async def handle_response(response: Response):
            try:
                resp_url = response.url
                status   = response.status
                ct       = response.headers.get("content-type", "")

                all_urls.append(f"[{status}] {resp_url[:120]}")

                if "json" in ct and status == 200:
                    all_json_urls.append(resp_url)
                    try:
                        data = await response.json()
                        if data:
                            products = find_products_in_json(data)
                            captured.append({
                                "url":           resp_url,
                                "has_products":  len(products) > 0,
                                "product_count": len(products),
                                "first_product": products[0] if products else None,
                                "top_keys":      list(data.keys())[:10] if isinstance(data, dict) else type(data).__name__,
                            })
                    except Exception as e:
                        captured.append({"url": resp_url, "error": str(e)})
            except Exception:
                pass

        page = await context.new_page()
        page.on("response", handle_response)

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass

        await page.wait_for_timeout(wait_ms)
        await browser.close()

    return {
        "url":              url,
        "total_responses":  len(all_urls),
        "json_responses":   len(all_json_urls),
        "json_urls":        all_json_urls[:30],
        "parsed_responses": captured,
        "all_urls_sample":  all_urls[:50],
    }


def safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except Exception:
        return default


def find_products_in_json(data: any, depth: int = 0) -> list[dict]:
    """
    Recursively search JSON for arrays that look like product lists.
    Checks for presence of both a name field AND a price field.
    """
    if depth > 8:
        return []

    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, dict):
            keys_lower = {k.lower() for k in first.keys()}
            price_keys = {
                "price", "mrp", "sellingprice", "saleprice", "selling_price",
                "offerprice", "offer_price", "discountedprice", "net_price",
                "effectiveprice", "salesprice", "unitprice", "amount"
            }
            name_keys = {
                "name", "productname", "product_name", "medicinename",
                "medicine_name", "title", "display_name", "drugname",
                "itemname", "brandname"
            }
            if price_keys & keys_lower and name_keys & keys_lower:
                return data
        return []

    if isinstance(data, dict):
        # Try high-priority keys first
        priority = [
            "products", "skus", "items", "results", "data",
            "productList", "medicines", "drugs", "catalog",
            "searchResult", "hits", "records", "list",
            "product_list", "medicine_list", "drug_list",
            "response", "payload", "body",
        ]
        for key in priority:
            if key in data:
                result = find_products_in_json(data[key], depth + 1)
                if result:
                    return result

        # Try all other keys
        for val in data.values():
            if isinstance(val, (dict, list)):
                result = find_products_in_json(val, depth + 1)
                if result:
                    return result

    return []


def extract_price_fields(product: dict) -> tuple[float, float]:
    price_fields = [
        "selling_price", "sellingPrice", "saleprice", "salePriceDecimal",
        "offerPrice", "offer_price", "price", "discountedPrice",
        "effective_price", "net_price", "unitPrice", "amount"
    ]
    mrp_fields = [
        "mrp", "MRP", "mrpPrice", "mrpDecimal", "max_retail_price",
        "marked_price", "original_price", "maxPrice", "compare_at_price",
        "listPrice", "standardPrice"
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
        "medicine_name", "title", "display_name", "drugName",
        "itemName", "brandName", "skuName"
    ]
    for f in name_fields:
        v = product.get(f)
        if v and isinstance(v, str) and len(v) > 2:
            return v.strip()
    return ""


def extract_slug_fields(product: dict) -> str:
    slug_fields = [
        "slug", "urlKey", "url_key", "handle", "sku",
        "productId", "product_id", "id", "skuId", "itemId"
    ]
    for f in slug_fields:
        v = product.get(f)
        if v:
            return str(v)
    return ""
