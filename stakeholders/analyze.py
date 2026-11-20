"""Stage 2: enrich what the page declared, then merge into one report.

    page metadata -> publisher on Wikidata -> owners/parents (up to max_depth hops)
                  -> funders and sponsors listed on any of those
                  -> GLEIF legal records and reported parents for the organisations
"""

from __future__ import annotations

from collections import deque

from . import gleif, wikidata
from .http import FetchError, fetch_article
from .metadata import extract
from .models import ROLES, Evidence, Report, Stakeholder, normalise_name

REL_ROLE = {"owned_by": "owner", "parent_org": "parent_company",
            "funder": "funder", "sponsor": "funder"}
REL_TEXT = {"owned_by": "owned by", "parent_org": "parent organisation",
            "funder": "funder", "sponsor": "sponsor"}
VIA = {"owned_by": "owner of", "parent_org": "parent organisation of",
       "funder": "funder of", "sponsor": "sponsor of"}


def analyze_url(url: str, **kwargs) -> Report:
    html, final_url = fetch_article(url)
    return analyze_html(html, final_url, **kwargs)


def analyze_html(html: str, url: str, enrich: bool = True, max_depth: int = 3) -> Report:
    meta = extract(html, url)
    people = list(meta.stakeholders)
    notes, errors = list(meta.notes), []

    if enrich:
        publisher = next((s for s in people if "publisher" in s.roles), None)
        try:
            found = _enrich_wikidata(publisher, meta, max_depth, notes)
            people += found
            for s in people:
                if "syndication_source" in s.roles and "domain" in s.details:
                    _link_by_website(s)
        except FetchError as exc:
            errors.append(f"Wikidata lookup failed, so ownership wasn't traced: {exc}")
        try:
            people += _enrich_gleif(people, notes)
        except FetchError as exc:
            errors.append(f"GLEIF lookup failed, so legal entity records are missing: {exc}")

    merged = merge(people)
    merged.sort(key=lambda s: (min(ROLES.index(r) for r in s.roles), s.depth, s.name.lower()))
    return Report(url=url, title=meta.title, published=meta.published,
                  stakeholders=merged, notes=notes, errors=errors)


# ------------------------------------------------------------------ Wikidata
def _enrich_wikidata(publisher: Stakeholder | None, meta, max_depth: int,
                     notes: list[str]) -> list[Stakeholder]:
    if publisher is None:
        return []
    urls = [u for u in (meta.publisher_url, meta.url) if u]
    match = wikidata.match_publisher(publisher.name, urls, publisher.details.get("domain", ""))
    if not match:
        notes.append(f"Couldn't match {publisher.name} to a Wikidata entry, so its ownership "
                     "couldn't be traced.")
        return []
    qid, conf, how = match
    publisher.ids["wikidata"] = qid
    publisher.evidence.append(Evidence("Wikidata", f"identified as {qid} ({how})",
                                       wikidata.item_url(qid)))
    if conf == "medium":
        publisher.confidence = "medium"

    # Breadth-first walk up the ownership chain, collecting funders on the way.
    found: list[Stakeholder] = []
    names = {qid: publisher.name}
    queue, visited = deque([(qid, 0)]), {qid}
    while queue:
        current, depth = queue.popleft()
        for rel in wikidata.relations(current):
            role = REL_ROLE[rel["rel"]]
            names.setdefault(rel["qid"], rel["label"])
            period = _period(rel)
            details = {k: rel[k] for k in ("start", "end") if rel.get(k)}
            found.append(Stakeholder(
                name=rel["label"], roles=[role], kind="unknown", confidence="medium",
                via=f"{VIA[rel['rel']]} {names[current]}{period}", depth=depth + 1,
                ids={"wikidata": rel["qid"]}, details=details,
                evidence=[Evidence("Wikidata", f"{names[current]} lists {rel['label']} as "
                                   f"'{REL_TEXT[rel['rel']]}'{period}", wikidata.item_url(current))]))
            is_chain = rel["rel"] in ("owned_by", "parent_org")
            if is_chain and rel["qid"] not in visited and depth + 1 < max_depth:
                visited.add(rel["qid"])
                queue.append((rel["qid"], depth + 1))

    # One batched query for labels, countries, LEIs and person/organisation.
    info = wikidata.details([qid] + [s.ids["wikidata"] for s in found])
    for s in [publisher] + found:
        d = info.get(s.ids["wikidata"], {})
        if s is not publisher and d.get("label"):
            s.name = d["label"]
        s.kind = "person" if d.get("human") else "group" if d.get("group") else "organization"
        for key in ("country", "country_code", "description"):
            if d.get(key):
                s.details[key] = d[key]
        if d.get("lei"):
            s.ids["lei"] = d["lei"]
    if not any("funder" in s.roles for s in found):
        notes.append(f"Wikidata lists no funders for {publisher.name} or its owners. That "
                     "usually means none are recorded there, not that none exist.")
    if not any(s.roles[0] in ("owner", "parent_company") for s in found):
        notes.append(f"Wikidata lists no current owner or parent for {publisher.name}.")
    return found


def _period(rel: dict) -> str:
    if rel.get("start") and rel.get("end"):
        return f" ({rel['start']}–{rel['end']})"
    if rel.get("start"):
        return f" (since {rel['start']})"
    if rel.get("end"):
        return f" (until {rel['end']})"
    return ""


def _link_by_website(s: Stakeholder) -> None:
    """Give a republished article's original outlet a proper name if Wikidata knows it."""
    hits = wikidata.find_by_website([s.details["domain"]])
    if not hits:
        return
    d = wikidata.details(hits[:1]).get(hits[0], {})
    s.ids["wikidata"] = hits[0]
    if d.get("label"):
        s.name = d["label"]
    s.evidence.append(Evidence("Wikidata", f"{s.details['domain']} is the website of {s.name}",
                               wikidata.item_url(hits[0])))


# ------------------------------------------------------------------ GLEIF
def _enrich_gleif(people: list[Stakeholder], notes: list[str]) -> list[Stakeholder]:
    found: list[Stakeholder] = []
    not_found: list[str] = []
    chain, seen = [], set()
    for s in people:  # the same company can appear once per role before merging
        key = s.ids.get("wikidata") or normalise_name(s.name)
        if s.kind == "organization" and set(s.roles) & {"publisher", "owner", "parent_company"} \
                and key not in seen:
            seen.add(key)
            chain.append(s)

    for s in chain:
        rec = None
        if "lei" in s.ids:
            rec = gleif.record(s.ids["lei"])
            how, conf = "LEI listed on Wikidata", "high"
            if rec is None:
                notes.append(f"GLEIF has no record for the LEI that Wikidata lists for "
                             f"{s.name} ({s.ids['lei']}).")
        elif "wikidata" in s.ids:  # only search for entities we've already identified
            rec, how = gleif.search_name(s.name, s.details.get("country_code", ""))
            conf = "medium"
            if rec is None and "share this name" in how:
                notes.append(f"GLEIF: {how} ({s.name}).")
            elif rec is None:
                not_found.append(s.name)
        if not rec:
            continue
        s.ids["lei"] = rec["lei"]
        s.details.update({k: rec[k] for k in ("legal_name", "jurisdiction", "registration_status")
                          if rec.get(k)})
        s.evidence.append(Evidence("GLEIF", f"legal entity {rec['legal_name']} ({how})", rec["url"]))
        if conf == "medium" and s.confidence == "high":
            s.confidence = "medium"

        for kind, role in (("direct", "parent_company"), ("ultimate", "ultimate_parent")):
            p = gleif.parent(rec["lei"], kind)
            if p:
                found.append(Stakeholder(
                    name=p["legal_name"], roles=[role], kind="organization", confidence="high",
                    via=f"{kind} parent of {s.name}", depth=s.depth + 1, ids={"lei": p["lei"]},
                    details={k: p[k] for k in ("legal_name", "jurisdiction") if p.get(k)},
                    evidence=[Evidence("GLEIF", f"reported {kind} parent of {rec['legal_name']}",
                                       p["url"])]))

    if not_found:
        names = ", ".join(not_found[:-1]) + (" or " if len(not_found) > 1 else "") + not_found[-1]
        notes.append(f"No LEI record found for {names}. Wikidata lists no LEI, and GLEIF has no "
                     "exact legal-name match. Many private companies and trusts don't have one.")
    return found


# ------------------------------------------------------------------ merging
def merge(people: list[Stakeholder]) -> list[Stakeholder]:
    """Combine entries that are the same entity (same Wikidata ID, LEI or name)."""
    out: list[Stakeholder] = []
    index: dict[str, Stakeholder] = {}
    for s in people:
        keys = [f"wd:{s.ids['wikidata']}" if "wikidata" in s.ids else None,
                f"lei:{s.ids['lei']}" if "lei" in s.ids else None,
                f"name:{normalise_name(s.name)}"]
        keys = [k for k in keys if k]
        target = next((index[k] for k in keys if k in index), None)
        if target is None:
            out.append(s)
            target = s
        else:
            target.merge(s)
        for k in keys + [f"wd:{target.ids['wikidata']}" if "wikidata" in target.ids else "",
                         f"lei:{target.ids['lei']}" if "lei" in target.ids else ""]:
            if k:
                index[k] = target
    return out