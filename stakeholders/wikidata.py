"""Wikidata lookups: who is the publisher, who owns it, who funds it.

Wikidata is community-edited, so identity matches by website are treated as
reliable, while ownership and funding claims are reported as medium confidence
with a link so the reader can check them.

Run `python -m stakeholders --check-wikidata` once to confirm the property IDs
below still carry the labels this code expects.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .http import get_json
from .models import normalise_name

SPARQL = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"

PROPS = {
    "owned_by": "P127",
    "parent_org": "P749",
    "funder": "P8324",
    "sponsor": "P859",
    "website": "P856",
    "lei": "P1278",
    "country": "P17",
    "iso_code": "P297",
    "instance_of": "P31",
    "end_time": "P582",
    "start_time": "P580",
    "has_part": "P527",
}
EXPECTED_LABELS = {
    "P127": "owned by", "P749": "parent organization or unit", "P8324": "funder",
    "P859": "sponsor", "P856": "official website", "P1278": "Legal Entity Identifier",
    "P17": "country", "P297": "ISO 3166-1 alpha-2 code", "P31": "instance of",
    "P582": "end time", "P580": "start time", "P527": "has part(s)",
    # Items, not properties, but checked the same way.
    "Q5": "human", "Q16334295": "group of humans",
}
HUMAN = "Q5"
GROUP_OF_HUMANS = "Q16334295"
NEWSY = ("news", "newspaper", "media", "magazine", "broadcaster", "broadcasting",
         "publisher", "publishing", "website", "journal", "radio", "television", "outlet")


def item_url(qid: str) -> str:
    return f"https://www.wikidata.org/wiki/{qid}"


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def sparql(query: str) -> list[dict[str, str]]:
    data = get_json(SPARQL, {"query": query, "format": "json"}) or {}
    rows = data.get("results", {}).get("bindings", [])
    return [{k: v.get("value", "") for k, v in row.items()} for row in rows]


def _values(qids) -> str:
    return " ".join(f"wd:{q}" for q in sorted(set(qids)))


# ------------------------------------------------------------------ finding the publisher
def url_variants(urls: list[str]) -> set[str]:
    """Official-website values are stored inconsistently, so try the common spellings."""
    out = set()
    for u in urls:
        p = urlparse(u if "//" in u else "https://" + u)
        if not p.hostname:
            continue
        bare = p.hostname.removeprefix("www.")
        path = p.path.rstrip("/")
        for host in (bare, "www." + bare):
            for scheme in ("https", "http"):
                for tail in {"", "/", path, path + "/"} if path else {"", "/"}:
                    out.add(f"{scheme}://{host}{tail}")
    return out


def find_by_website(urls: list[str]) -> list[str]:
    variants = url_variants(urls)
    if not variants:
        return []
    values = " ".join(f"<{v}>" for v in sorted(variants))
    rows = sparql(f"SELECT DISTINCT ?item WHERE {{ VALUES ?site {{ {values} }} "
                  f"?item wdt:{PROPS['website']} ?site . }}")
    return [_qid(r["item"]) for r in rows]


def search_by_name(name: str, limit: int = 7) -> list[dict]:
    data = get_json(API, {"action": "wbsearchentities", "search": name, "language": "en",
                          "uselang": "en", "type": "item", "limit": limit, "format": "json"}) or {}
    return [{"id": r.get("id"), "label": r.get("label", ""), "description": r.get("description", "")}
            for r in data.get("search", []) if r.get("id")]


def details(qids) -> dict[str, dict]:
    """Label, description, country, LEI, websites and whether it's a person."""
    qids = list(set(qids))
    if not qids:
        return {}
    rows = sparql(f"""
SELECT ?item ?itemLabel ?itemDescription ?countryLabel ?iso ?lei ?site ?type ?member WHERE {{
  VALUES ?item {{ {_values(qids)} }}
  OPTIONAL {{ ?item wdt:{PROPS['country']} ?country .
             OPTIONAL {{ ?country wdt:{PROPS['iso_code']} ?iso }} }}
  OPTIONAL {{ ?item wdt:{PROPS['lei']} ?lei }}
  OPTIONAL {{ ?item wdt:{PROPS['website']} ?site }}
  OPTIONAL {{ ?item wdt:{PROPS['instance_of']} ?type }}
  OPTIONAL {{ ?item wdt:{PROPS['has_part']} ?member . ?member wdt:{PROPS['instance_of']} wd:{HUMAN} }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}""")
    out: dict[str, dict] = {}
    for r in rows:
        q = _qid(r["item"])
        d = out.setdefault(q, {"label": r.get("itemLabel", q), "description": r.get("itemDescription", ""),
                               "country": "", "country_code": "", "lei": "", "websites": set(),
                               "human": False, "group": False, "members": set()})
        d["country"] = d["country"] or r.get("countryLabel", "")
        d["country_code"] = d["country_code"] or r.get("iso", "")
        d["lei"] = d["lei"] or r.get("lei", "")
        if r.get("site"):
            d["websites"].add(r["site"])
        if r.get("type") and _qid(r["type"]) == HUMAN:
            d["human"] = True
        if r.get("type") and _qid(r["type"]) == GROUP_OF_HUMANS:
            d["group"] = True
        if r.get("member"):
            d["members"].add(r["member"])
    for d in out.values():
        # e.g. one item for two siblings: made of people, but not a person itself
        if not d["human"] and len(d["members"]) >= 2:
            d["group"] = True
    return out


def match_publisher(name: str, urls: list[str], domain: str) -> tuple[str, str, str] | None:
    """Returns (qid, confidence, how it matched), or None.

    1. An item whose official website is this site: high confidence.
    2. A name-search hit whose website is on this domain: high confidence.
    3. A name-search hit with the exact name and a news-like description: medium.
    """
    from .metadata import site_key  # local import avoids a cycle

    hits = find_by_website(urls)
    if hits:
        if len(hits) > 1:  # a paper and its company sometimes share a website
            info = details(hits)
            target = normalise_name(name, strip_legal=True)
            hits.sort(key=lambda q: normalise_name(info.get(q, {}).get("label", ""),
                                                   strip_legal=True) != target)
        return hits[0], "high", "official website matches this site"

    candidates = search_by_name(name)
    if not candidates:
        return None
    info = details([c["id"] for c in candidates])
    for c in candidates:
        sites = info.get(c["id"], {}).get("websites", set())
        if any(site_key(s) == domain for s in sites):
            return c["id"], "high", "name search, website on this domain"
    target = normalise_name(name, strip_legal=True)
    for c in candidates:
        if (normalise_name(c["label"], strip_legal=True) == target
                and any(w in c["description"].lower() for w in NEWSY)):
            return c["id"], "medium", "exact name match with a news-related description"
    return None


# ------------------------------------------------------------------ relationships
def relations(qid: str) -> list[dict]:
    """Current owners, parent organisations, funders and sponsors of one item.

    Skips deprecated statements and ones with an end date in the past.
    """
    rows = sparql(f"""
SELECT ?rel ?target ?targetLabel ?start ?end WHERE {{
  VALUES (?rel ?p ?ps) {{
    ("owned_by"   p:{PROPS['owned_by']}   ps:{PROPS['owned_by']})
    ("parent_org" p:{PROPS['parent_org']} ps:{PROPS['parent_org']})
    ("funder"     p:{PROPS['funder']}     ps:{PROPS['funder']})
    ("sponsor"    p:{PROPS['sponsor']}    ps:{PROPS['sponsor']})
  }}
  wd:{qid} ?p ?st . ?st ?ps ?target . ?st wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
  FILTER(isIRI(?target))
  OPTIONAL {{ ?st pq:{PROPS['start_time']} ?start }}
  OPTIONAL {{ ?st pq:{PROPS['end_time']} ?end }}
  FILTER(!BOUND(?end) || ?end > NOW())
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}""")
    found: dict[tuple, dict] = {}
    for r in rows:
        key = (r["rel"], _qid(r["target"]))
        rel = found.setdefault(key, {"rel": r["rel"], "qid": key[1],
                                     "label": r.get("targetLabel", key[1]), "start": "", "end": ""})
        # Several statements can link the same pair; keep the earliest start.
        start, end = _year(r.get("start", "")), _year(r.get("end", ""))
        if start and (not rel["start"] or start < rel["start"]):
            rel["start"] = start
        rel["end"] = rel["end"] or end
    return list(found.values())


def _year(value: str) -> str:
    """'+2015-01-01T00:00:00Z' or '2015-01-01T...' -> '2015'. Only the year is shown,
    because Wikidata often stores year-precision dates as 1 January."""
    value = value.lstrip("+")
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else ""


def check_properties() -> list[tuple[str, str, str, bool]]:
    """(property, expected label, actual label, ok) for every property used here."""
    rows = sparql(f"""SELECT ?prop ?propLabel WHERE {{
  VALUES ?prop {{ {' '.join('wd:' + p for p in EXPECTED_LABELS)} }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}""")
    actual = {_qid(r["prop"]): r.get("propLabel", "") for r in rows}
    return [(p, exp, actual.get(p, "(missing)"), actual.get(p, "").lower() == exp.lower())
            for p, exp in EXPECTED_LABELS.items()]