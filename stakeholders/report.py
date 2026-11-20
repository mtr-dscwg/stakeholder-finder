"""Render a Report as terminal text (matching the detector's CLI look) or JSON."""

from __future__ import annotations

import json
import textwrap

from .models import ROLE_LABELS, ROLES, Report

MARK = {"high": "●●●", "medium": "●●○", "low": "●○○"}


def to_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def to_text(report: Report, show_evidence: bool = True, width: int = 88) -> str:
    out = [f"$ stakeholders {report.url}", ""]
    if report.title:
        out.append(f"  {report.title}")
    if report.published:
        out.append(f"  published {report.published}")
    out.append(f"  {len(report.stakeholders)} stakeholders found   "
               f"confidence: ●●● high  ●●○ medium  ●○○ low")

    for role in ROLES:
        group = [s for s in report.stakeholders if s.roles[0] == role]
        if not group:
            continue
        out += ["", f"── {ROLE_LABELS[role]} " + "─" * max(0, width - len(ROLE_LABELS[role]) - 4)]
        for s in group:
            extra_roles = [ROLE_LABELS[r].lower() for r in s.roles[1:]]
            line = f"  {MARK[s.confidence]} {s.name}"
            if s.kind == "person" and role != "author":
                line += "  (person)"
            elif s.kind == "group":
                line += "  (people)"
            if extra_roles:
                line += f"  [also {', '.join(extra_roles)}]"
            out.append(line)
            facts = []
            if s.via:
                facts.append(s.via)
            for key, label in (("job_title", ""), ("legal_name", "legal name: "),
                               ("jurisdiction", "jurisdiction: "), ("country", "country: "),
                               ("registration_status", "LEI status: ")):
                if s.details.get(key):
                    facts.append(label + s.details[key])
            for key in ("wikidata", "lei"):
                if key in s.ids:
                    facts.append(f"{key}: {s.ids[key]}")
            if facts:
                out += textwrap.wrap(" · ".join(facts), width, initial_indent="      ",
                                     subsequent_indent="      ")
            if show_evidence:
                for e in s.evidence:
                    text = f"↳ {e.source}: {e.detail}" + (f"  <{e.url}>" if e.url else "")
                    out += textwrap.wrap(text, width, initial_indent="      ",
                                         subsequent_indent="        ")

    if report.notes:
        out += ["", "── Notes " + "─" * (width - 9)]
        for n in report.notes:
            out += textwrap.wrap(n, width, initial_indent="  - ", subsequent_indent="    ")
    if report.errors:
        out += ["", "── Lookups that failed " + "─" * (width - 23)]
        for e in report.errors:
            out += textwrap.wrap(e, width, initial_indent="  ! ", subsequent_indent="    ")
    out += ["", "  A stakeholder is anyone connected to the article. Being listed here is not "
                "evidence", "  that they shaped what it says."]
    return "\n".join(out)