# MedCompare India v3 — Playwright XHR Capture 💊

**The right approach:** Instead of parsing HTML, we open a real browser
and intercept the actual JSON API calls pharmacy websites make internally.

---

## 📁 Structure

```
medcompare-v3/
├── Dockerfile                      ← Installs Chromium on Railway
├── main.py                         ← FastAPI app
├── requirements.txt
├── railway.toml                    ← Uses Dockerfile builder
├── .gitignore
│
├── extractor/
│   ├── __init__.py
│   └── network_extractor.py        ← Core Playwright XHR capture engine
│
├── connectors/
│   ├── __init__.py
│   └── pharmacies.py               ← 5 pharmacy connectors
│
├── services/
│   ├── __init__.py
│   └── matcher.py                  ← Medicine name matching
│
├── database/
│   ├── __init__.py
│   └── db.py                       ← SQLite
│
└── static/
    └── index.html                  ← Frontend
```

---

## 🚀 Deploy on Railway

1. Create new GitHub repo → upload all files maintaining folder structure
2. railway.app → New Project → Deploy from GitHub
3. Railway uses `Dockerfile` automatically (installs Chromium)
4. Settings → Domains → Generate domain
5. Done ✅

---

## 🔌 API

```
GET /api/compare?medicine=dolo650
GET /api/compare?medicine=dolo650&pincode=560001
GET /api/pharmacy/1mg?q=dolo650
GET /api/popular
GET /api/health
DELETE /api/cache
```

---

## ⚡ How It Works

```
User searches "Dolo 650"
        ↓
Open 5 pharmacy pages simultaneously in headless Chromium
        ↓
Listen to all XHR/Fetch network responses
        ↓
Capture JSON responses containing product data
        ↓
Extract name, price, MRP, link
        ↓
Apply medicine matching (fuzzy)
        ↓
Return comparison with best price
```

## ⚠️ Note on Speed

Playwright opens a real browser per pharmacy.
Each search takes **15-25 seconds** (5 browsers in parallel).
Results are cached for **30 minutes** so repeat searches are instant.
