"""
Database — SQLite
Stores: medicine info, pharmacy product URLs, search history
"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path("data/medcompare.db")

def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS medicines (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            normalized TEXT NOT NULL,
            salt       TEXT,
            strength   TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS pharmacy_urls (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id   INTEGER REFERENCES medicines(id),
            pharmacy      TEXT NOT NULL,
            product_url   TEXT NOT NULL,
            product_name  TEXT,
            last_verified TEXT DEFAULT (datetime('now')),
            is_active     INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS search_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            query      TEXT NOT NULL,
            pincode    TEXT,
            found_on   INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_medicines_normalized ON medicines(normalized);
        CREATE INDEX IF NOT EXISTS idx_search_query ON search_history(query);
    """)
    conn.commit()
    conn.close()

def save_search(query: str, found_on: int, pincode: str = None):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO search_history (query, pincode, found_on) VALUES (?, ?, ?)",
            (query.lower(), pincode, found_on)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_popular_searches(limit: int = 10) -> list:
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT query, COUNT(*) as count
            FROM search_history
            GROUP BY query
            ORDER BY count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
        return [{"query": r["query"], "count": r["count"]} for r in rows]
    except Exception:
        return []
