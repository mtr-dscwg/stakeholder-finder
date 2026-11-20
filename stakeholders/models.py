"""Data shapes shared by every stage of the pipeline.

A Stakeholder is one person or organisation connected to the article.
Every claim about it carries Evidence saying where it came from, so the
report can always answer "how do you know that?".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Roles, roughly in the order the report lists them.
ROLES = [
    "author",
    "publisher",
    "syndication_source",
    "sponsor",
    "owner",
    "parent_company",
    "ultimate_parent",
    "funder",
    "declared_interest",
]

ROLE_LABELS = {
    "author": "Author",
    "publisher": "Publisher",
    "syndication_source": "Syndication source",
    "sponsor": "Sponsor or partner",
    "owner": "Owner",
    "parent_company": "Parent company",
    "ultimate_parent": "Ultimate parent",
    "funder": "Funder",
    "declared_interest": "Author's declared interest",
}

CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Evidence:
    source: str             # e.g. "article JSON-LD", "Wikidata", "GLEIF"
    detail: str             # what exactly was found
    url: str | None = None  # where to check it


@dataclass
class Stakeholder:
    name: str
    roles: list[str]
    kind: str = "unknown"          # "person", "organization" or "unknown"
    confidence: str = "medium"     # "high", "medium" or "low"
    via: str | None = None         # e.g. "owner of The Guardian"
    depth: int = 0                 # hops from the article (0 = named on the page)
    ids: dict[str, str] = field(default_factory=dict)      # wikidata, lei
    details: dict[str, str] = field(default_factory=dict)  # country, legal name, ...
    evidence: list[Evidence] = field(default_factory=list)

    def key(self) -> str:
        """Identity used for merging: a registry ID if we have one, else the name."""
        if "wikidata" in self.ids:
            return "wd:" + self.ids["wikidata"]
        if "lei" in self.ids:
            return "lei:" + self.ids["lei"]
        return "name:" + normalise_name(self.name)

    def merge(self, other: Stakeholder) -> None:
        for role in other.roles:
            if role not in self.roles:
                self.roles.append(role)
        if CONFIDENCE_ORDER[other.confidence] > CONFIDENCE_ORDER[self.confidence]:
            self.confidence = other.confidence
        if self.kind == "unknown":
            self.kind = other.kind
        self.depth = min(self.depth, other.depth)
        self.via = self.via or other.via
        for k, v in other.ids.items():
            self.ids.setdefault(k, v)
        for k, v in other.details.items():
            self.details.setdefault(k, v)
        seen = {(e.source, e.detail) for e in self.evidence}
        self.evidence += [e for e in other.evidence if (e.source, e.detail) not in seen]


@dataclass
class ArticleMeta:
    """Everything read from the article page itself, before any lookups."""
    url: str
    canonical_url: str | None = None
    title: str | None = None
    published: str | None = None
    site_name: str | None = None
    publisher_url: str | None = None
    stakeholders: list[Stakeholder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    url: str
    title: str | None
    published: str | None
    stakeholders: list[Stakeholder]
    notes: list[str]     # neutral observations, e.g. "no funders listed"
    errors: list[str]    # lookups that failed, so gaps aren't mistaken for absences

    def to_dict(self) -> dict:
        return asdict(self)


LEGAL_SUFFIXES = {
    "ltd", "limited", "plc", "inc", "incorporated", "llc", "llp", "lp", "pty",
    "corp", "corporation", "co", "company", "group", "holdings", "the", "gmbh",
    "ag", "sa", "nv", "bv", "ulc", "pte",
}


def normalise_name(name: str, strip_legal: bool = False) -> str:
    """Lowercase, drop punctuation, optionally drop words like Ltd or plc."""
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in name.replace("&", " and "))
    words = cleaned.split()
    if strip_legal:
        words = [w for w in words if w not in LEGAL_SUFFIXES] or words
    return " ".join(words)
