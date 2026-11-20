"""Stage 2 with Wikidata and GLEIF faked, so the tests run offline and stay stable.

The fake payloads follow the public response formats (SPARQL JSON results,
wbsearchentities, GLEIF JSON:API). The entities are invented on purpose.
"""

from pathlib import Path

import pytest

from stakeholders import analyze, gleif, wikidata
from stakeholders.http import FetchError
from stakeholders.report import to_json, to_text

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.theguardian.com/global-development/x"


# ------------------------------------------------------------------ parsers
def test_sparql_rows_are_flattened(monkeypatch):
    payload = {"head": {"vars": ["item", "itemLabel"]}, "results": {"bindings": [
        {"item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q42"},
         "itemLabel": {"type": "literal", "value": "Example"}}]}}
    monkeypatch.setattr(wikidata, "get_json", lambda url, params=None: payload)
    assert wikidata.sparql("SELECT ...") == [
        {"item": "http://www.wikidata.org/entity/Q42", "itemLabel": "Example"}]


def test_url_variants_cover_scheme_www_and_slash():
    v = wikidata.url_variants(["https://www.example.com/news/"])
    assert {"https://www.example.com/", "http://example.com", "https://example.com/news"} <= v


GLEIF_RECORD = {"data": {"type": "lei-records", "id": "549300AAAAAAAAAAAA01", "attributes": {
    "lei": "549300AAAAAAAAAAAA01",
    "entity": {"legalName": {"name": "Example Media Group plc", "language": "en"},
               "jurisdiction": "GB", "status": "ACTIVE",
               "legalAddress": {"country": "GB", "city": "London"}},
    "registration": {"status": "ISSUED"}}}}


def test_gleif_record_summary():
    s = gleif._summary(GLEIF_RECORD)
    assert s["legal_name"] == "Example Media Group plc"
    assert s["jurisdiction"] == "GB" and s["registration_status"] == "ISSUED"
    assert s["url"].endswith("549300AAAAAAAAAAAA01")
    assert gleif._summary(None) is None


def _fuzzy(*pairs):
    return {"data": [{"type": "fuzzycompletions", "attributes": {"value": name},
                      "relationships": {"lei-records": {"data": {"type": "lei-records", "id": lei}}}}
                     for name, lei in pairs]}


def test_gleif_name_search_needs_one_exact_match(monkeypatch):
    records = {"LEI_GB": ("Example Media Group plc", "GB"), "LEI_AU": ("Example Media Group Pty Ltd", "AU")}

    def fake(url, params=None):
        if "fuzzycompletions" in url:
            return _fuzzy(("Example Media Group PLC", "LEI_GB"), ("Example Media Group Pty Ltd", "LEI_AU"),
                          ("Example Media Services Ltd", "LEI_X"))
        lei = url.rsplit("/", 1)[-1]
        name, country = records[lei]
        rec = {"data": {"id": lei, "attributes": {"lei": lei, "entity": {
            "legalName": {"name": name}, "jurisdiction": country, "legalAddress": {"country": country}},
            "registration": {"status": "ISSUED"}}}}
        return rec

    monkeypatch.setattr(gleif, "get_json", fake)
    rec, how = gleif.search_name("Example Media Group", "AU")
    assert rec["lei"] == "LEI_AU" and how == "exact legal-name match"
    rec, how = gleif.search_name("Example Media Group")  # no country: ambiguous
    assert rec is None and "share this name" in how


# ------------------------------------------------------------------ end to end
GRAPH = {  # qid -> relations
    "Q100": [{"rel": "owned_by", "qid": "Q200", "label": "Example Media Group"}],
    "Q200": [{"rel": "owned_by", "qid": "Q300", "label": "Example Trust"},
             {"rel": "funder", "qid": "Q400", "label": "Example Foundation"}],
    "Q300": [{"rel": "owned_by", "qid": "Q900", "label": "Too Far Holdings"}],
}
INFO = {
    "Q100": {"label": "The Guardian", "country": "United Kingdom", "country_code": "GB"},
    "Q200": {"label": "Example Media Group", "country": "United Kingdom", "country_code": "GB",
             "lei": "549300AAAAAAAAAAAA01"},
    "Q300": {"label": "Example Trust", "country": "United Kingdom", "country_code": "GB",
             "lei": "549300BBBBBBBBBBBB02"},
    "Q400": {"label": "Example Foundation"},
    "Q900": {"label": "Too Far Holdings"},
}


@pytest.fixture
def fake_world(monkeypatch):
    calls = {"relations": []}
    monkeypatch.setattr(wikidata, "find_by_website",
                        lambda urls: ["Q100"] if any("theguardian.com" in u for u in urls) else [])
    monkeypatch.setattr(wikidata, "search_by_name", lambda name, limit=7: [])

    def relations(qid):
        calls["relations"].append(qid)
        return GRAPH.get(qid, [])

    monkeypatch.setattr(wikidata, "relations", relations)
    monkeypatch.setattr(wikidata, "details", lambda qids: {
        q: {"description": "", "websites": set(), "human": False, "lei": "", "country": "",
            "country_code": "", **INFO.get(q, {})} for q in qids})
    monkeypatch.setattr(gleif, "record", lambda lei: {
        "lei": lei, "legal_name": {"549300AAAAAAAAAAAA01": "Example Media Group plc",
                                   "549300BBBBBBBBBBBB02": "Example Trust Limited"}[lei],
        "jurisdiction": "GB", "registration_status": "ISSUED", "url": gleif.record_url(lei)})
    # The group reports the trust as its parent: should merge with the Wikidata owner.
    monkeypatch.setattr(gleif, "parent", lambda lei, kind: {
        "lei": "549300BBBBBBBBBBBB02", "legal_name": "Example Trust Limited", "jurisdiction": "GB",
        "url": gleif.record_url("549300BBBBBBBBBBBB02")} if lei == "549300AAAAAAAAAAAA01" else None)
    monkeypatch.setattr(gleif, "search_name", lambda name, cc="": (None, "no exact legal-name match"))
    return calls


def run(depth=3):
    return analyze.analyze_html((FIX / "guardian_like.html").read_text(), URL, max_depth=depth)


def find(report, name):
    return next(s for s in report.stakeholders if s.name == name)


def test_ownership_chain_funders_and_gleif(fake_world):
    r = run()
    pub = find(r, "The Guardian")
    assert pub.ids["wikidata"] == "Q100" and pub.confidence == "high"

    group = find(r, "Example Media Group")
    assert group.roles == ["owner"] and group.via == "owner of The Guardian"
    assert group.details["legal_name"] == "Example Media Group plc"

    trust = find(r, "Example Trust")
    # Wikidata owner and GLEIF parent are the same entity: merged, not listed twice.
    assert set(trust.roles) == {"owner", "parent_company", "ultimate_parent"}
    assert {e.source for e in trust.evidence} == {"Wikidata", "GLEIF"}
    assert not any(s.name == "Example Trust Limited" for s in r.stakeholders)

    assert "funder" in find(r, "Example Foundation").roles
    assert not any("no funders" in n for n in r.notes)
    assert r.errors == []


def test_depth_cap_stops_the_walk(fake_world):
    r = run(depth=2)
    assert "Q300" not in fake_world["relations"]  # trust found at depth 2 but not expanded
    assert not any(s.name == "Too Far Holdings" for s in r.stakeholders)


def test_unmatched_publisher_is_a_note_not_an_error(fake_world, monkeypatch):
    monkeypatch.setattr(wikidata, "find_by_website", lambda urls: [])
    r = run()
    assert any("Couldn't match The Guardian" in n for n in r.notes)
    assert r.errors == []
    assert find(r, "Bill & Melinda Gates Foundation")  # page-level results survive


def test_api_outage_is_reported_and_page_results_survive(monkeypatch):
    def boom(*a, **k):
        raise FetchError("query.wikidata.org: HTTP 503")
    monkeypatch.setattr(wikidata, "find_by_website", boom)
    monkeypatch.setattr(gleif, "search_name", boom)
    r = run()
    assert any("Wikidata lookup failed" in e for e in r.errors)
    assert find(r, "Jane Doe") and find(r, "The Guardian")


def test_renderers(fake_world):
    r = run()
    text = to_text(r)
    assert "── Owner" in text and "Example Trust" in text and "not evidence" in text
    assert '"stakeholders"' in to_json(r)
