"""Spreadsheet output and run-to-run state."""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .pubmed import Publication
from .trials import Trial

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
NEW_FILL = PatternFill("solid", fgColor="FFF2CC")

PUB_COLUMNS = [
    ("New this run", 12),
    ("Confidence", 12),
    ("PMID", 11),
    ("First seen", 12),
    ("Trial NCT(s)", 26),
    ("Trial name(s)", 34),
    ("Title", 70),
    ("Journal", 22),
    ("Year", 7),
    ("First author", 18),
    ("DOI", 24),
    ("PMC", 12),
    ("Matched by", 20),
    ("QIL evidence", 40),
    ("Publication type", 28),
    ("PubMed link", 38),
]

#: PubMed publication types that mean "this is commentary on the literature",
#: not a report of the trial.
SECONDARY_TYPES = (
    "review", "editorial", "comment", "news", "historical article",
    "practice guideline", "guideline", "meta-analysis",
)

#: Title shapes that recur in the papers that merely cite a trial. Derived from
#: a hand review of the October 2026 abstract-only bucket, where these phrases
#: accounted for most of the false positives.
SECONDARY_TITLE_HINTS = (
    "fda approval summary", "narrative review", "literature review",
    "a review", "systematic review", "indirect comparison",
    "indirect treatment comparison", "network meta-analysis",
    # A trial report does not describe itself as preclinical; a paper that
    # mentions it anywhere in the title is almost always bench work.
    "preclinical",
    "real-world", "real world", "mechanism of action",
    "current evidence", "recent advance", "back to the future",
)


def confidence(pub) -> str:
    """How much a row can be trusted without a human looking at it.

    ``high``  — the trial registry or the sponsor declared the link.
    ``review`` — matched only by a prose mention, and the record looks like
                 commentary rather than trial output. Likeliest to be wrong.
    ``check`` — matched only by a prose mention and not obviously commentary.

    This is triage, not truth: a ``review`` row can still be a real secondary
    analysis, which is why nothing is dropped on the strength of it.
    """
    if {"databank", "ctgov"} & pub.evidence:
        return "high"
    types = (pub.pub_types or "").lower()
    title = (pub.title or "").lower()
    if any(t in types for t in SECONDARY_TYPES) or any(
        h in title for h in SECONDARY_TITLE_HINTS
    ):
        return "review?"
    return "check"


@dataclass
class State:
    """What previous runs already reported, so a run can show only what's new."""

    path: Path
    first_seen: dict[str, str] = field(default_factory=dict)
    last_run: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "State":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read state at %s (%s) — treating all pubs as new", p, exc)
            return cls(path=p)
        return cls(
            path=p,
            first_seen=dict(raw.get("first_seen", {})),
            last_run=raw.get("last_run", ""),
        )

    def is_new(self, pmid: str) -> bool:
        return pmid not in self.first_seen

    def record(self, pmids: list[str], run_date: str) -> list[str]:
        new = [p for p in pmids if self.is_new(p)]
        for pmid in new:
            self.first_seen[pmid] = run_date
        return new

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"last_run": self.last_run, "first_seen": self.first_seen}, indent=1
            )
        )
        tmp.replace(self.path)  # atomic, so a killed pod can't corrupt state


def _style_header(ws, widths: list[tuple[str, int]]) -> None:
    for i, (name, width) in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"


def write_workbook(
    path: str | Path,
    pubs: list[Publication],
    trials_by_nct: dict[str, Trial],
    all_trials: list[Trial],
    state: State,
    new_pmids: set[str],
    run_date: str,
) -> None:
    wb = Workbook()

    ws = wb.active
    ws.title = "Publications"
    _style_header(ws, PUB_COLUMNS)

    ordered = sorted(
        pubs, key=lambda p: (p.pmid not in new_pmids, p.year or "0", p.pmid), reverse=False
    )
    ordered.sort(key=lambda p: (p.pmid not in new_pmids, -int(p.year or 0)))

    for pub in ordered:
        ncts = sorted(pub.ncts)
        names = []
        for nct in ncts:
            trial = trials_by_nct.get(nct)
            if trial and trial.name:
                names.append(trial.name)
        is_new = pub.pmid in new_pmids
        row = [
            "NEW" if is_new else "",
            confidence(pub),
            pub.pmid,
            state.first_seen.get(pub.pmid, run_date),
            ", ".join(ncts),
            "; ".join(names),
            pub.title,
            pub.journal,
            pub.year,
            pub.first_author,
            pub.doi,
            pub.pmcid,
            ", ".join(sorted(pub.evidence)),
            pub.qil_evidence,
            pub.pub_types,
            pub.url,
        ]
        ws.append(row)
        if is_new:
            for cell in ws[ws.max_row]:
                cell.fill = NEW_FILL
        ws.cell(row=ws.max_row, column=7).alignment = Alignment(wrap_text=True, vertical="top")
        link = ws.cell(row=ws.max_row, column=len(PUB_COLUMNS))
        link.hyperlink = pub.url
        link.font = Font(color="0563C1", underline="single")

    ws.auto_filter.ref = f"A1:{get_column_letter(len(PUB_COLUMNS))}{max(ws.max_row, 1)}"

    # One row per trial, so the cancer center can see coverage, not just hits.
    ws2 = wb.create_sheet("By trial")
    cols = [
        ("NCT", 14), ("Trial name", 40), ("Protocol", 46), ("Phase", 10),
        ("Status", 14), ("Publications", 13), ("New this run", 13), ("PMIDs", 60),
    ]
    _style_header(ws2, cols)
    by_nct: dict[str, list[Publication]] = {}
    for pub in pubs:
        for nct in pub.ncts:
            by_nct.setdefault(nct, []).append(pub)
    for nct, trial in sorted(trials_by_nct.items()):
        hits = by_nct.get(nct, [])
        ws2.append([
            nct, trial.name, trial.protocol, trial.phase, trial.status,
            len(hits),
            sum(1 for p in hits if p.pmid in new_pmids),
            ", ".join(sorted(p.pmid for p in hits)),
        ])
    ws2.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(ws2.max_row, 1)}"

    # Rows we could not search, so gaps are visible rather than invisible.
    ws3 = wb.create_sheet("Needs attention")
    cols3 = [("Row", 7), ("Protocol", 60), ("Trial name", 40), ("Status", 14), ("Issue", 50)]
    _style_header(ws3, cols3)
    for trial in all_trials:
        if trial.notes:
            ws3.append([
                trial.row, trial.protocol, trial.name, trial.status,
                "; ".join(trial.notes),
            ])

    ws4 = wb.create_sheet("Run log")
    _style_header(ws4, [("Field", 30), ("Value", 60)])
    searched = len(trials_by_nct)
    tiers = {"high": 0, "review?": 0, "check": 0}
    for pub in pubs:
        tiers[confidence(pub)] += 1
    for field_name, value in [
        ("Run date (UTC)", run_date),
        ("Previous run", state.last_run or "none — first run"),
        ("Trial rows in source list", len(all_trials)),
        ("Trials searched (unique NCT)", searched),
        ("Trial rows not searchable", sum(1 for t in all_trials if not t.effective_nct)),
        ("Publications total", len(pubs)),
        ("Publications new this run", len(new_pmids)),
        ("Trials with >=1 publication", len(by_nct)),
        ("Confidence: high", tiers["high"]),
        ("Confidence: review? (verify)", tiers["review?"]),
        ("Confidence: check (verify)", tiers["check"]),
        ("QIL evidence: acknowledged", sum(
            1 for p in pubs if "acknowledged" in p.qil_evidence)),
        ("QIL evidence: QIL co-author", sum(
            1 for p in pubs if "qil-author" in p.qil_evidence)),
        ("QIL evidence: MCW author only", sum(
            1 for p in pubs
            if p.qil_evidence == "mcw-author")),
        ("QIL evidence: none (not ruled out)", sum(
            1 for p in pubs if not p.qil_evidence)),
    ]:
        ws4.append([field_name, value])

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    log.info("wrote %s", path)


def write_new_csv(path: str | Path, pubs: list[Publication], new_pmids: set[str],
                  trials_by_nct: dict[str, Trial]) -> int:
    rows = [p for p in pubs if p.pmid in new_pmids]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Confidence", "QIL evidence", "PMID", "NCT", "Trial name",
                    "Title", "Journal", "Year", "First author", "DOI",
                    "Matched by", "Link"])
        # Rows needing a human look sort to the top of the month's additions.
        tier = {"check": 0, "review?": 1, "high": 2}
        for p in sorted(rows, key=lambda p: (tier[confidence(p)], -int(p.year or 0))):
            names = [trials_by_nct[n].name for n in sorted(p.ncts) if n in trials_by_nct]
            w.writerow([confidence(p), p.qil_evidence, p.pmid,
                        ", ".join(sorted(p.ncts)), "; ".join(names),
                        p.title, p.journal, p.year, p.first_author, p.doi,
                        ", ".join(sorted(p.evidence)), p.url])
    return len(rows)


def month_stamp(today: date | None = None) -> str:
    return (today or datetime.now(timezone.utc).date()).strftime("%Y-%m")
