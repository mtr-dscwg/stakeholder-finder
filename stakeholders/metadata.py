"""Stage 1: read stakeholders that the article page itself declares.

Sources, most to least reliable:
  - JSON-LD structured data (the publisher's own machine-readable metadata)
  - meta tags (og:site_name, author, article:section, ...)
  - visible text patterns (wire credits, "Supported by ...", disclosure statements)
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound

from .models import ArticleMeta, Evidence, Stakeholder, normalise_name

ARTICLE_TYPES = {
    "newsarticle", "article", "reportagenewsarticle", "analysisnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "backgroundnewsarticle",
    "blogposting", "socialmediaposting", "scholarlyarticle", "liveblogposting",
}

# Wire services and syndication agencies. Patterns match credits like
# "(Reuters) -", "— AAP" or an author field of "Associated Press".
WIRE_SERVICES = {
    "Reuters": r"reuters",
    "Associated Press": r"associated press|\bap\b",
    "Agence France-Presse": r"agence france[- ]presse|\bafp\b",
    "Australian Associated Press": r"australian associated press|\baap\b",
    "Bloomberg": r"bloomberg",
    "PA Media": r"\bpa media\b|press association",
    "The Canadian Press": r"canadian press",
    "Deutsche Presse-Agentur": r"\bdpa\b",
}

SPONSOR_PHRASES = r"(?:sponsored|supported|funded|presented|paid for|underwritten|brought to you|produced in partnership with|in partnership with|in association with)"
SPONSOR_LEADS = re.compile(SPONSOR_PHRASES + r"(?:\s+by)?\s*:?\s*", re.I)
SPONSOR_LABELS = re.compile(
    r"\b(sponsored content|sponsored post|paid content|paid post|partner content|"
    r"branded content|advertorial|advertisement feature|promoted content|in partnership)\b", re.I)
CONNECTORS = {"&", "and", "of", "for", "the", "de", "du", "von", "van", "on", "in"}

# Two-part domain suffixes in the countries v1 targets.
TWO_PART_SUFFIXES = {
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "co.nz", "org.nz", "net.nz", "govt.nz", "ac.nz",
    "gc.ca", "qc.ca", "on.ca", "bc.ca",
    "co.ie", "gov.ie",
}


# ------------------------------------------------------------------ helpers
def make_soup(html: str) -> BeautifulSoup:
    """lxml is faster and more forgiving; Python's built-in parser works if it's missing."""
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def site_key(url_or_host: str) -> str:
    """Registrable domain: news.example.co.uk -> example.co.uk."""
    host = urlparse(url_or_host).hostname if "//" in url_or_host else url_or_host
    labels = (host or "").lower().strip(".").split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    n = 3 if ".".join(labels[-2:]) in TWO_PART_SUFFIXES else 2
    return ".".join(labels[-n:])


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {str(x).lower() for x in (t if isinstance(t, list) else [t])}


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value) -> str | None:
    """Pull a plain string out of a JSON-LD value that may be a dict or list."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value"))
    if isinstance(value, list) and value:
        return _text(value[0])
    return None


def trim_org_name(raw: str) -> str | None:
    """'Bill & Melinda Gates Foundation. Read more' -> 'Bill & Melinda Gates Foundation'."""
    raw = re.split(r"[.|•·\n\r;:(\[]| - | – | — ", raw.strip(), maxsplit=1)[0]
    kept = []
    for word in raw.split():
        bare = word.strip(",'\"")
        if not bare:
            break
        if bare[0].isupper() or bare[0].isdigit() or bare.lower() in CONNECTORS:
            kept.append(bare)
            if word.endswith(","):
                break
        else:
            break
    while kept and kept[-1].lower() in CONNECTORS:
        kept.pop()
    while kept and kept[0].lower() in CONNECTORS:
        kept.pop(0)
    name = " ".join(kept)
    return name if len(name) >= 3 else None


def split_names(raw: str) -> list[str]:
    """'Jane Doe and John Smith' -> two names; 'Smith & Sons' stays whole."""
    parts = [p.strip() for p in re.split(r",\s*|\s+and\s+|\s*&\s*", raw) if p.strip()]
    if len(parts) > 1 and all(1 < len(p.split()) <= 4 for p in parts):
        return parts
    return [raw.strip()]


def wire_service(text: str) -> str | None:
    lowered = text.lower()
    for name, pattern in WIRE_SERVICES.items():
        if re.search(pattern, lowered):
            return name
    return None


# ------------------------------------------------------------------ JSON-LD
def parse_jsonld(soup: BeautifulSoup) -> list[dict]:
    """Every JSON-LD node on the page, with @graph flattened and @id refs resolvable."""
    nodes: list[dict] = []
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except ValueError:
            try:  # some sites leave control characters or trailing commas
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", re.sub(r"[\x00-\x1f]", " ", raw)))
            except ValueError:
                continue
        stack = _as_list(data)
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            nodes.append(item)
            stack += _as_list(item.get("@graph"))
    return nodes


def _resolve(value, by_id: dict) -> list[dict]:
    out = []
    for v in _as_list(value):
        if isinstance(v, str):
            out.append({"name": v})
        elif isinstance(v, dict):
            ref = v.get("@id")
            out.append({**by_id.get(ref, {}), **v} if ref and ref in by_id and len(v) == 1
                       else v)
    return out


def _pick_article(nodes: list[dict]) -> dict | None:
    articles = [n for n in nodes if _types(n) & ARTICLE_TYPES]
    # Prefer nodes that actually carry a headline or author.
    articles.sort(key=lambda n: (bool(n.get("author")), bool(n.get("headline"))), reverse=True)
    return articles[0] if articles else None


# ------------------------------------------------------------------ main
def extract(html: str, url: str) -> ArticleMeta:
    soup = make_soup(html)
    meta = ArticleMeta(url=url)
    nodes = parse_jsonld(soup)
    by_id = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str)}
    article = _pick_article(nodes) or {}

    def tag(*names: str) -> str | None:
        for n in names:
            el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if el and el.get("content", "").strip():
                return el["content"].strip()
        return None

    # Basic facts
    meta.title = _text(article.get("headline")) or tag("og:title") or (
        soup.title.get_text(strip=True) if soup.title else None)
    meta.published = _text(article.get("datePublished")) or tag(
        "article:published_time", "datePublished", "pubdate")
    canon = soup.find("link", rel="canonical")
    meta.canonical_url = urljoin(url, canon["href"]) if canon and canon.get("href") else tag("og:url")

    body_el = soup.find("article") or soup.body or soup
    for junk in body_el.find_all(["script", "style", "noscript"]):
        junk.decompose()
    body_text = re.sub(r"\s+", " ", body_el.get_text(" ")).strip()

    _authors(meta, article, by_id, tag)
    _publisher(meta, article, by_id, tag, url)
    _syndication(meta, article, by_id, body_text, url)
    _sponsors(meta, article, by_id, soup, tag)
    _disclosures(meta, body_text)
    return meta


def _authors(meta, article, by_id, tag) -> None:
    found = _resolve(article.get("author"), by_id)
    source = "article JSON-LD"
    if not found:
        raw = tag("author", "article:author", "dc.creator", "parsely-author", "sailthru.author")
        if raw and not raw.startswith("http"):
            found = [{"name": n} for n in split_names(raw)]
            source = "meta tag"
    for a in found:
        name = _text(a.get("name"))
        if not name or name.startswith("http"):
            continue
        for single in split_names(name) if source == "meta tag" else [name]:
            wire = wire_service(single) if len(single.split()) <= 4 else None
            is_org = "organization" in _types(a) or "newsmediaorganization" in _types(a)
            if wire and not is_org:
                is_org = True
            details = {k: v for k, v in {
                "job_title": _text(a.get("jobTitle")),
                "profile": a.get("url") if isinstance(a.get("url"), str) else None,
            }.items() if v}
            meta.stakeholders.append(Stakeholder(
                name=wire or single,
                roles=["syndication_source"] if wire else ["author"],
                kind="organization" if is_org else "person",
                confidence="high" if source == "article JSON-LD" else "medium",
                details=details,
                evidence=[Evidence(source, f"listed as author: {single}", details.get("profile"))],
            ))


def _publisher(meta, article, by_id, tag, url) -> None:
    pubs = _resolve(article.get("publisher"), by_id)
    pub = pubs[0] if pubs else {}
    name = _text(pub.get("name"))
    pub_url = pub.get("url") if isinstance(pub.get("url"), str) else None
    source, conf = "article JSON-LD", "high"
    if not name:
        name = tag("og:site_name", "application-name")
        source, conf = "meta tag", "medium"
    if not name:
        name = site_key(url)
        source, conf = "page address", "low"
    meta.site_name = name
    meta.publisher_url = pub_url or f"https://{urlparse(url).hostname}/"
    meta.stakeholders.append(Stakeholder(
        name=name, roles=["publisher"], kind="organization", confidence=conf,
        details={"domain": site_key(url)},
        evidence=[Evidence(source, f"publisher named as {name}", meta.publisher_url)],
    ))


def _syndication(meta, article, by_id, body_text, url) -> None:
    # Organisations the article says it came from.
    for prop in ("sourceOrganization", "provider", "creditText", "copyrightHolder"):
        for org in _resolve(article.get(prop), by_id):
            name = _text(org.get("name"))
            if not name or normalise_name(name) == normalise_name(meta.site_name or ""):
                continue
            wire = wire_service(name)
            meta.stakeholders.append(Stakeholder(
                name=wire or name, roles=["syndication_source"], kind="organization",
                confidence="high" if wire else "medium",
                evidence=[Evidence("article JSON-LD", f"{prop}: {name}")],
            ))

    # Wire credits in the text: a dateline "(Reuters) -" or a closing "- AAP".
    head, tail = body_text[:600], body_text[-300:]
    for m in re.finditer(r"\((Reuters|AP|AFP|AAP|Bloomberg|PA)\)", head):
        name = wire_service(m.group(1))
        meta.stakeholders.append(Stakeholder(
            name=name, roles=["syndication_source"], kind="organization", confidence="medium",
            evidence=[Evidence("article text", f"dateline credit '{m.group(0)}'")]))
    m = re.search(r"(?:with|via|source:)?\s*\b(AAP|AP|Reuters|AFP|PA)\s*$", tail)
    if m:
        meta.stakeholders.append(Stakeholder(
            name=wire_service(m.group(1)), roles=["syndication_source"], kind="organization",
            confidence="medium",
            evidence=[Evidence("article text", f"closing credit '{m.group(0).strip()}'")]))

    # A canonical link to another site usually means a republished article.
    if meta.canonical_url and site_key(meta.canonical_url) != site_key(url):
        original = site_key(meta.canonical_url)
        meta.notes.append(f"The page's canonical link points to {original}, so this is "
                          "probably a republished copy.")
        meta.stakeholders.append(Stakeholder(
            name=original, roles=["syndication_source"], kind="organization", confidence="medium",
            details={"domain": original},
            evidence=[Evidence("canonical link", "original version is on another site",
                               meta.canonical_url)]))


def _sponsors(meta, article, by_id, soup, tag) -> None:
    # schema.org lets a CreativeWork name a sponsor or funder directly.
    for prop in ("sponsor", "funder"):
        for org in _resolve(article.get(prop), by_id):
            name = _text(org.get("name"))
            if name:
                meta.stakeholders.append(Stakeholder(
                    name=name, roles=["sponsor"], kind="organization", confidence="high",
                    evidence=[Evidence("article JSON-LD", f"{prop}: {name}")]))

    # "Supported by X" / "In partnership with X" labels. The name is often a logo,
    # so fall back to nearby image alt text or link text.
    for node in soup.find_all(string=SPONSOR_LEADS):
        container = node.parent
        if container is None or container.name in ("script", "style"):
            continue
        text = " ".join(container.get_text(" ").split())
        m = SPONSOR_LEADS.search(text)
        if not m or m.start() > 40:  # the phrase should lead the label, not sit mid-paragraph
            continue
        name = trim_org_name(text[m.end():])
        source_detail = f"label '{text[:80]}'"
        if not name:
            scope = container.parent or container
            img = scope.find("img", alt=True)
            link = scope.find("a")
            name = trim_org_name(img["alt"]) if img else None
            if not name and link:
                name = trim_org_name(link.get_text(" "))
        if name:
            if source_detail.endswith(f"{m.group(0).strip()}'") or len(text) <= len(m.group(0)) + 2:
                source_detail = f"label '{m.group(0).strip()}' next to a logo or link for {name}"
            meta.stakeholders.append(Stakeholder(
                name=name, roles=["sponsor"], kind="organization", confidence="medium",
                evidence=[Evidence("page label", source_detail)]))

    # Generic labels with no name attached.
    labelled = " ".join(filter(None, [
        tag("article:section", "section"), tag("keywords", "news_keywords"),
        _text(article.get("articleSection")), " ".join(map(str, _as_list(article.get("keywords")))),
        meta.title]))
    m = SPONSOR_LABELS.search(labelled)
    if m and not any("sponsor" in s.roles for s in meta.stakeholders):
        meta.notes.append(f"The page is labelled '{m.group(0)}', but no sponsor is named.")


DISCLOSURE = re.compile(r"(?:disclosure statement|disclosures?|conflicts? of interest|"
                        r"competing interests?)\s*:?\s*(.{20,1200})", re.I)
FUNDING_FROM = re.compile(
    r"(?:receives?|received|has received|is) (?:research )?(?:funding|grants?|support) "
    r"(?:from|by) (.+?)(?:\.|;|$)", re.I)
ROLE_AT = re.compile(r"(?:is a (?:member|director|fellow|board member|adviser|advisor|"
                     r"trustee|consultant) (?:of|at|to|for)|works for|consults for|"
                     r"owns shares in|is employed by) (.+?)(?:\.|;|$)", re.I)


def _disclosures(meta, body_text) -> None:
    """Author interest statements, as published by outlets like The Conversation."""
    m = DISCLOSURE.search(body_text)
    if not m:
        return
    section = m.group(1)
    section = re.split(r"\b(?:Partners|Republish this article|Read more|Related)\b", section)[0]
    for pattern, verb in ((FUNDING_FROM, "receives funding from"), (ROLE_AT, "has a role at")):
        for hit in pattern.finditer(section):
            for chunk in re.split(r",\s*|\s+and\s+(?=(?:the\s+)?[A-Z])", hit.group(1)):
                name = trim_org_name(chunk)
                if name:
                    meta.stakeholders.append(Stakeholder(
                        name=name, roles=["declared_interest"], kind="organization",
                        confidence="medium",
                        evidence=[Evidence("disclosure statement",
                                           f"author {verb} {name}: '{hit.group(0).strip()[:160]}'")]))
    if "declared_interest" not in {r for s in meta.stakeholders for r in s.roles}:
        if re.search(r"do(?:es)? not work for, consult, own shares in or receive funding", section, re.I):
            meta.notes.append("The author's disclosure statement declares no relevant interests.")
