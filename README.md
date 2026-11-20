# Stakeholder finder, v1

Give it a news article and it lists the people and organisations connected to it:
authors, publisher, wire services, sponsors, owners and parent companies, funders,
and interests the author has declared. Every entry shows where the claim came from
and how confident the tool is.

v1 covers the **production side** (who made and paid for the article). The content
side (who the article quotes and mentions) is the next stage.

## Setup

    pip install -r requirements.txt
    export STAKEHOLDERS_CONTACT="you@example.com"   # Wikidata asks callers to identify themselves
    python -m stakeholders --check-wikidata         # run once: confirms the property IDs

## Use

    python -m stakeholders https://www.theguardian.com/...
    python -m stakeholders URL --json        # machine-readable, for the web demo later
    python -m stakeholders URL --offline     # page metadata only, no lookups
    python -m stakeholders URL --brief       # hide evidence lines
    python -m stakeholders --html saved.html --url URL   # for sites that block scripts

In Python: `from stakeholders import analyze_url, analyze_html`.

## How it works

1. **The page** (`metadata.py`): JSON-LD structured data first, then meta tags, then
   text patterns for wire credits ("(Reuters) -", a closing "AAP"), sponsor labels
   ("Supported by", "In partnership with", including logo alt text), disclosure
   statements, and canonical links that point to another site (republished copies).
2. **Wikidata** (`wikidata.py`): matches the publisher by official website, falling back
   to a name search. Then walks "owned by" and "parent organization" up to 3 hops,
   collecting "funder" and "sponsor" claims on the way. Ended and deprecated claims
   are skipped.
3. **GLEIF** (`gleif.py`): for organisations in the chain, fetches the legal entity record
   (by the LEI on Wikidata, or an unambiguous exact-name match) and its reported direct
   and ultimate parents.
4. **Merge** (`analyze.py`): the same entity found by several routes becomes one entry
   with several roles and all its evidence.

Confidence: publisher-declared metadata and GLEIF parents are high, community-edited
Wikidata claims and text patterns are medium, guesses from the page address are low.

## Deliberate limits

- Authors are listed with their byline details only. They're never looked up in
  registries or Wikidata, since a name match could easily be the wrong person.
- People who own outlets are listed, but their own ties aren't followed.
- "No funders found" means none are recorded in these sources, not that none exist.
  The report says so in its notes.
- Failed lookups are reported separately from empty results, so an outage isn't
  mistaken for "nothing to find".

## Tests

    python -m pytest -q

The tests run offline: saved pages in `tests/fixtures/`, and Wikidata and GLEIF replaced
by fakes that follow their real response formats.
