"""Command line entry point.

    qil-pubs run --trials trials.xlsx --out-dir ./out
    qil-pubs resolve-ncts --trials trials.xlsx --out missing.csv
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .ctgov import CtGovClient
from .notify import send_email
from .pubmed import Publication, PubMedClient
from .qil_evidence import DEFAULT_QIL_AUTHORS, annotate
from .report import (
    State, confidence, month_stamp, write_new_csv, write_workbook,
)
from .trials import load_trials, searchable


class RingHandler(logging.Handler):
    """Keeps the last N log lines so a failure email can carry them.

    A CronJob failure is otherwise invisible until someone reads pod logs that
    may already have been rotated away.
    """

    def __init__(self, capacity: int = 60) -> None:
        super().__init__()
        self.capacity = capacity
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        if len(self.lines) > self.capacity:
            del self.lines[: -self.capacity]

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.lines[-n:])


RING = RingHandler()


def _log_setup(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    RING.setFormatter(fmt)
    # Only INFO and worse reach the ring; DEBUG would flood it with httpx noise.
    RING.setLevel(logging.INFO)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[stream, RING],
        force=True,
    )


def is_first_week(today: datetime | None = None) -> bool:
    """True if today is in the first 7 days of the month.

    Cron cannot express "first Monday": when both day-of-month and day-of-week
    are restricted, cron ORs them, so `15 6 1-7 * 1` would fire on every Monday
    AND every 1st-7th. So the CronJob runs every Monday and this gate stops the
    runs that are not the first one.
    """
    return (today or datetime.now()).day <= 7


def cmd_run(args: argparse.Namespace) -> int:
    if args.only_first_week and not is_first_week():
        logging.info(
            "not the first Monday of the month (day %d) — nothing to do",
            datetime.now().day,
        )
        return 0

    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - must notify before dying
        logging.exception("run failed")
        if args.email_to:
            send_email(
                to=args.email_to,
                subject=f"QIL trial publications — RUN FAILED ({month_stamp()})",
                body=(
                    "The monthly publication scrape did not finish.\n\n"
                    f"Error: {type(exc).__name__}: {exc}\n\n"
                    "Nothing was written and the state file is unchanged, so the\n"
                    "next run will pick up everything this one would have found.\n\n"
                    "Last log lines:\n\n" + RING.tail()
                ),
            )
        return 1


def _run(args: argparse.Namespace) -> int:
    run_date = datetime.now(timezone.utc).date().isoformat()
    out_dir = Path(args.out_dir)

    all_trials = load_trials(args.trials)
    trials_by_nct = searchable(all_trials)
    ncts = sorted(trials_by_nct)
    logging.info(
        "%d trial rows, %d unique NCT numbers to search", len(all_trials), len(ncts)
    )
    if not ncts:
        logging.error("no searchable NCT numbers in %s", args.trials)
        return 2

    headers = {"User-Agent": f"qil-pubs/0.1 ({args.email or 'MCW QIL'})"}
    with httpx.Client(headers=headers, follow_redirects=True) as http:
        pubmed = PubMedClient(
            http, email=args.email, api_key=args.api_key or os.environ.get("NCBI_API_KEY", "")
        )
        pmids = pubmed.search_ncts(ncts, free_text=not args.databank_only)
        logging.info("PubMed returned %d candidate PMIDs", len(pmids))

        pubs = pubmed.fetch(sorted(pmids), set(ncts))

        # Papers the sponsor registered against the trial. These may not name the
        # NCT anywhere PubMed indexes, so without this step they are missed.
        if not args.skip_ctgov:
            ctgov = CtGovClient(http)
            extra: dict[str, set[str]] = {}
            for i, nct in enumerate(ncts, start=1):
                for ref in ctgov.references(nct, include_background=args.include_background):
                    extra.setdefault(ref.pmid, set()).add(nct)
                if i % 50 == 0:
                    logging.info("ClinicalTrials.gov: %d/%d trials checked", i, len(ncts))
            missing = [p for p in extra if p not in pubs]
            if missing:
                logging.info("fetching %d PMIDs found only via ClinicalTrials.gov", len(missing))
                for pmid, pub in pubmed.fetch(missing, set(ncts)).items():
                    pubs[pmid] = pub
            for pmid, linked in extra.items():
                pub = pubs.get(pmid) or Publication(pmid=pmid)
                pub.ncts |= linked
                pub.evidence.add("ctgov")
                pubs[pmid] = pub

    # A paper can surface in search without naming any trial of ours (PubMed's
    # free-text match is loose). Keep only papers we can actually attribute.
    attributed = [p for p in pubs.values() if p.ncts]
    dropped = len(pubs) - len(attributed)
    if dropped:
        logging.info("dropped %d candidate papers with no attributable trial", dropped)

    # Did the paper actually use the core? Separate question from "does it
    # belong to a QIL-supported trial", and only answerable positively.
    if not args.skip_qil_evidence:
        surnames = tuple(
            s.strip() for s in args.qil_authors.split(",") if s.strip()
        ) or DEFAULT_QIL_AUTHORS
        with httpx.Client(headers=headers, follow_redirects=True) as http:
            pubmed = PubMedClient(
                http, email=args.email,
                api_key=args.api_key or os.environ.get("NCBI_API_KEY", ""),
            )
            tally = annotate(
                attributed, pubmed,
                qil_authors=surnames,
                fetch_full_text=args.full_text,
            )
        logging.info("QIL evidence: %s", tally)

    state = State.load(args.state)
    new_pmids = set(state.record([p.pmid for p in attributed], run_date))
    logging.info("%d publications total, %d new since last run", len(attributed), len(new_pmids))

    xlsx = out_dir / "qil_publications.xlsx"
    write_workbook(xlsx, attributed, trials_by_nct, all_trials, state, new_pmids, run_date)
    csv_path = out_dir / f"new_publications_{month_stamp()}.csv"
    write_new_csv(csv_path, attributed, new_pmids, trials_by_nct)

    if not args.dry_run:
        state.save()
    else:
        logging.info("dry run — state not updated, next run will report the same pubs as new")

    if args.email_to:
        send_email(
            to=args.email_to,
            subject=(
                f"QIL trial publications — OK, {len(new_pmids)} new "
                f"({month_stamp()})"
            ),
            body=_summary(
                attributed, new_pmids, trials_by_nct, len(ncts), all_trials, args
            ),
            attachments=[xlsx, csv_path] if new_pmids else [xlsx],
        )

    return 0


def _summary(pubs, new_pmids, trials_by_nct, searched, all_trials, args) -> str:
    needs_review = [
        p for p in pubs
        if p.pmid in new_pmids and confidence(p) in ("check", "review?")
    ]
    lines = [
        "The monthly publication scrape finished successfully.",
        "",
        f"  Trials searched        {searched}"
        f"  (of {len(all_trials)} rows; "
        f"{sum(1 for t in all_trials if not t.effective_nct)} have no NCT number)",
        f"  Publications total     {len(pubs)}",
        f"  New since last run     {len(new_pmids)}",
        f"  Of those, need review  {len(needs_review)}",
        "",
    ]
    if args.dry_run:
        lines += ["NOTE: dry run — nothing was recorded, so the next run will "
                  "report these as new again.", ""]

    if new_pmids:
        lines.append("New this run:")
        for p in sorted(
            (p for p in pubs if p.pmid in new_pmids),
            key=lambda p: (confidence(p) == "high", -int(p.year or 0)),
        ):
            flag = "" if confidence(p) == "high" else f"  [{confidence(p)}]"
            lines.append(
                f"  [{', '.join(sorted(p.ncts))}]{flag} "
                f"{p.first_author} et al. {p.journal} {p.year}. {p.title}"
            )
            lines.append(f"      {p.url}")
        lines += [
            "",
            "Rows marked [check] or [review?] matched only on a prose mention of",
            "the NCT number and may be citing the trial rather than reporting it.",
        ]
    else:
        lines.append("No new publications this month.")

    lines += ["", "Full running list attached as qil_publications.xlsx."]
    return "\n".join(lines)


def cmd_resolve(args: argparse.Namespace) -> int:
    """Best-effort NCT lookup for the rows the cancer center left blank."""
    import csv

    trials = [t for t in load_trials(args.trials) if not t.nct]
    logging.info("%d rows without an NCT number", len(trials))
    with httpx.Client(follow_redirects=True) as http:
        ctgov = CtGovClient(http)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Row", "Protocol", "Trial name", "Status",
                        "Candidate NCT", "Candidate title", "Confirmed? (y/n)"])
            for trial in trials:
                hits = ctgov.find_nct(trial.protocol)
                if not hits:
                    w.writerow([trial.row, trial.protocol, trial.name, trial.status, "", "", ""])
                for nct, title in hits:
                    w.writerow([trial.row, trial.protocol, trial.name, trial.status,
                                nct, title, ""])
    logging.info("wrote %s — confirm each candidate before adding it to the trial list", args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    # -v is accepted both before and after the subcommand; putting it on a
    # parent parser alone makes `qil-pubs run -v` an error, which is the order
    # most people reach for.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="qil-pubs", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        parents=[common],
        help="search PubMed and write the publication report",
    )
    run.add_argument("--trials", required=True, help="cancer center trial spreadsheet (.xlsx)")
    run.add_argument("--out-dir", default="out")
    run.add_argument("--state", default="out/state.json",
                     help="tracks which PMIDs have already been reported")
    run.add_argument("--email", default=os.environ.get("NCBI_EMAIL", ""),
                     help="contact address NCBI asks API users to send")
    run.add_argument("--api-key", default="", help="NCBI API key (raises rate limit to 10/s)")
    run.add_argument("--email-to", default=os.environ.get("QIL_EMAIL_TO", ""),
                     help="comma-separated recipients for the monthly summary")
    run.add_argument("--databank-only", action="store_true",
                     help="match only declared registry accessions, no free-text")
    run.add_argument("--include-background", action="store_true",
                     help="also count ClinicalTrials.gov BACKGROUND citations")
    run.add_argument("--skip-ctgov", action="store_true")
    run.add_argument(
        "--qil-authors", default=",".join(DEFAULT_QIL_AUTHORS),
        help="comma-separated surnames to treat as QIL staff",
    )
    run.add_argument("--skip-qil-evidence", action="store_true",
                     help="skip core-usage grading entirely")
    run.add_argument(
        "--full-text", action="store_true",
        help="also fetch PMC full text looking for shared-resource "
             "acknowledgments. Off by default: on the October 2026 baseline "
             "this found zero across 340 papers, at 222 requests per run.",
    )
    run.add_argument("--dry-run", action="store_true", help="write reports but not state")
    run.add_argument(
        "--only-first-week", action="store_true",
        help="exit immediately unless today is in the first 7 days of the "
             "month. Pair with a weekly cron schedule to get 'first Monday', "
             "which cron cannot express on its own.",
    )
    run.set_defaults(func=cmd_run)

    res = sub.add_parser(
        "resolve-ncts", parents=[common], help="look up NCT numbers for blank rows"
    )
    res.add_argument("--trials", required=True)
    res.add_argument("--out", default="out/missing_ncts.csv")
    res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    _log_setup(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
