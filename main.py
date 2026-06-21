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
