"""Stage 1 on saved pages. No network needed."""

from pathlib import Path

from stakeholders.metadata import extract, site_key, split_names, trim_org_name

FIX = Path(__file__).parent / "fixtures"


def load(name, url="https://www.example.com/article"):
    return extract((FIX / name).read_text(), url)


def by_role(meta, role):
    return [s.name for s in meta.stakeholders if role in s.roles]


def test_jsonld_graph_with_id_references():
    m = load("guardian_like.html", "https://www.theguardian.com/global-development/x")
    assert by_role(m, "author") == ["Jane Doe", "Sam Lee"]
    assert by_role(m, "publisher") == ["The Guardian"]
    assert m.published == "2026-11-20T06:00:00Z"
    assert all(s.confidence == "high" for s in m.stakeholders if "author" in s.roles)
    assert not m.notes  # canonical is on the same site


def test_sponsor_named_only_by_logo_alt_text():
    m = load("guardian_like.html", "https://www.theguardian.com/global-development/x")
    assert by_role(m, "sponsor") == ["Bill & Melinda Gates Foundation"]


def test_disclosure_statement_and_partner_section_ignored():
    m = load("conversation_like.html", "https://theconversation.com/why-coal-12345")
    assert by_role(m, "declared_interest") == [
        "Australian Research Council", "Grattan Institute", "Climate Council"]
    assert "University of Sydney" not in by_role(m, "declared_interest")
    author = next(s for s in m.stakeholders if "author" in s.roles)
    assert "Energy Economics" in author.details["job_title"]


def test_wire_copy_and_republication():
    m = load("syndicated_aap.html", "https://www.rivertonledger.com.au/rates")
    assert "Australian Associated Press" in by_role(m, "syndication_source")
    assert "originalpaper.com.au" in by_role(m, "syndication_source")
    assert not by_role(m, "author")  # "AAP" is an agency, not a person
    assert any("republished" in n for n in m.notes)


def test_broken_jsonld_falls_back_to_meta_tags():
    m = load("meta_only.html")
    assert by_role(m, "author") == ["Jane Doe", "John Smith"]
    assert by_role(m, "publisher") == ["Harbour Times"]
    assert by_role(m, "syndication_source") == ["Reuters"]
    assert all(s.confidence == "medium" for s in m.stakeholders)


def test_trailing_comma_jsonld_and_sponsor_label():
    m = load("sponsored.html", "https://www.dailyplanet.co.uk/retirement")
    assert by_role(m, "sponsor") == ["Acme Bank"]
    assert next(s for s in m.stakeholders if "author" in s.roles).kind == "organization"


def test_unnamed_sponsor_label_becomes_a_note():
    html = (FIX / "sponsored.html").read_text().replace("Presented by Acme Bank. Terms apply.", "")
    m = extract(html, "https://www.dailyplanet.co.uk/retirement")
    assert not by_role(m, "sponsor")
    assert any("Sponsored content" in n for n in m.notes)


def test_page_with_nothing_still_names_a_publisher():
    m = extract("<html><body><p>hello</p></body></html>", "https://news.example.co.uk/a")
    pub = next(s for s in m.stakeholders if "publisher" in s.roles)
    assert pub.name == "example.co.uk" and pub.confidence == "low"


def test_helpers():
    assert site_key("https://www.smh.com.au/national/x") == "smh.com.au"
    assert site_key("https://edition.cnn.com/") == "cnn.com"
    assert site_key("news.bbc.co.uk") == "bbc.co.uk"
    assert split_names("Jane Doe and John Smith") == ["Jane Doe", "John Smith"]
    assert split_names("Johnson & Johnson") == ["Johnson & Johnson"]
    assert trim_org_name("the Wellcome Trust. Read more about our partners") == "Wellcome Trust"
    assert trim_org_name("tomorrow") is None
