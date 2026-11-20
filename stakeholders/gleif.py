"""GLEIF lookups: legal entity records and reported parent companies.

GLEIF publishes the global Legal Entity Identifier (LEI) system. Parent links
come from the entities themselves (accounting consolidation), so they're
treated as high confidence. Many small outlets and nonprofits have no LEI.
"""

from __future__ import annotations

from .http import get_json
from .models import normalise_name

BASE = "https://api.gleif.org/api/v1"


def record_url(lei: str) -> str:
    return f"https://search.gleif.org/#/record/{lei}"


def _summary(data: dict | None) -> dict | None:
    """Flatten one JSON:API lei-record into the fields the report uses."""
    if not data:
        return None
    rec = data.get("data", data)
    if isinstance(rec, list):
        rec = rec[0] if rec else None
    if not rec:
        return None
    attrs = rec.get("attributes", {})
    entity = attrs.get("entity", {})
    lei = attrs.get("lei") or rec.get("id")
    return {
        "lei": lei,
        "legal_name": (entity.get("legalName") or {}).get("name", ""),
        "jurisdiction": entity.get("jurisdiction") or "",
        "country": (entity.get("legalAddress") or {}).get("country", ""),
        "entity_status": entity.get("status") or "",
        "registration_status": (attrs.get("registration") or {}).get("status", ""),
        "url": record_url(lei),
    }


def record(lei: str) -> dict | None:
    return _summary(get_json(f"{BASE}/lei-records/{lei}"))


def parent(lei: str, kind: str) -> dict | None:
    """kind is "direct" or "ultimate". None means no parent was reported."""
    found = _summary(get_json(f"{BASE}/lei-records/{lei}/{kind}-parent"))
    return found if found and found["lei"] != lei else None


def search_name(name: str, country_code: str = "") -> tuple[dict | None, str]:
    """Find an LEI by exact legal-name match. Returns (record, explanation).

    Only accepts a single unambiguous match, optionally narrowed by country,
    because similar company names are common across jurisdictions.
    """
    data = get_json(f"{BASE}/fuzzycompletions", {"field": "entity.legalName", "q": name}) or {}
    target = normalise_name(name, strip_legal=True)
    leis = []
    for item in data.get("data", []):
        value = (item.get("attributes") or {}).get("value", "")
        rel = ((item.get("relationships") or {}).get("lei-records") or {}).get("data") or {}
        if rel.get("id") and normalise_name(value, strip_legal=True) == target:
            leis.append(rel["id"])
    records = [r for r in (record(lei) for lei in dict.fromkeys(leis[:5])) if r]
    if country_code:
        local = [r for r in records if r["country"] == country_code
                 or r["jurisdiction"].split("-")[0] == country_code]
        records = local or records
    active = [r for r in records if r["registration_status"] in ("ISSUED", "PENDING_TRANSFER", "PENDING_ARCHIVAL")]
    records = active or records
    if len(records) == 1:
        return records[0], "exact legal-name match"
    if len(records) > 1:
        return None, f"{len(records)} entities share this name, so none was picked"
    return None, "no exact legal-name match"
