"""
Medicine Matching Engine — Phase 1
Fuzzy string matching to group same medicine across pharmacies
"""
import re
from rapidfuzz import fuzz

REMOVE_WORDS = [
    "tablet", "tablets", "tab", "tabs", "capsule", "capsules",
    "cap", "caps", "strip", "strips", "pack", "packs",
    "injection", "syrup", "cream", "gel", "ointment",
    "drops", "solution", "suspension", "of", "with",
    "mg", "ml", "mcg", "gm", "gms",
]

def normalize(name: str) -> str:
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"['\-/\\]", " ", name)
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    words = [w for w in name.split() if w not in REMOVE_WORDS]
    cleaned = []
    for i, w in enumerate(words):
        if w.isdigit() and int(w) <= 10 and i > 1:
            continue
        cleaned.append(w)
    return re.sub(r"\s+", " ", " ".join(cleaned)).strip()

def match_score(name1: str, name2: str) -> float:
    n1, n2 = normalize(name1), normalize(name2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 100.0
    return max(
        fuzz.ratio(n1, n2),
        fuzz.partial_ratio(n1, n2),
        fuzz.token_sort_ratio(n1, n2),
        fuzz.token_set_ratio(n1, n2),
    )

def group_by_medicine(results: list, query: str, threshold: float = 70.0) -> list:
    matched = []
    for r in results:
        products  = r.get("products", [])
        filtered  = []
        for p in products:
            score = match_score(query, p.get("name", ""))
            if score >= threshold or (len(products) == 1 and score > 50):
                p["match_score"] = round(score, 1)
                filtered.append(p)
        filtered.sort(key=lambda x: x.get("match_score", 0), reverse=True)
        matched.append({**r, "products": filtered})
    return matched
