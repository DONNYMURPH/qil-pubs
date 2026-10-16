"""Where exactly does 'Wisconsin' appear in these papers?

Run:  python3 diagnose_wisconsin.py out/qil_publications.xlsx [sample_size]

An MCW mention in an author affiliation means nothing about core usage — half
the trials have an MCW site investigator. A mention inside the acknowledgment
or funding statement is the signal we want. This prints each occurrence with
its surrounding section so the difference is visible.
"""
from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET

import httpx
import openpyxl

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
WIS = re.compile(r"wisconsin", re.I)


def sections(xml: str) -> list[tuple[str, str]]:
    """Return (where, text) for the parts of the article worth labelling."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out: list[tuple[str, str]] = []
    front = root.find(".//front")
    if front is not None:
        out.append(("FRONT (affiliations/metadata)", " ".join(front.itertext())))
    for tag, label in (
        ("ack", "ACKNOWLEDGMENT"),
        ("funding-statement", "FUNDING STATEMENT"),
        ("fn", "FOOTNOTE"),
        ("notes", "NOTES"),
    ):
        for node in root.iter(tag):
            out.append((label, " ".join(node.itertext())))
    body = root.find(".//body")
    if body is not None:
        out.append(("BODY", " ".join(body.itertext())))
    return [(w, re.sub(r"\s+", " ", t)) for w, t in out]


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "out/qil_publications.xlsx"
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    ws = openpyxl.load_workbook(path)["Publications"]
    hdr = [c.value for c in ws[1]]
    pmc_i, pmid_i = hdr.index("PMC"), hdr.index("PMID")
    rows = [(r[pmid_i].value, r[pmc_i].value) for r in ws.iter_rows(min_row=2)
            if r[pmc_i].value]

    print(f"scanning up to {sample} of {len(rows)} papers with PMCIDs\n")
    where_counts: dict[str, int] = {}
    found = 0

    with httpx.Client(follow_redirects=True) as http:
        for pmid, pmcid in rows[:sample]:
            try:
                xml = http.get(
                    f"{BASE}/efetch.fcgi",
                    params={"db": "pmc", "id": pmcid, "retmode": "xml",
                            "tool": "qil-pubs-diag"},
                    timeout=60.0,
                ).text
            except Exception as exc:  # noqa: BLE001
                print(f"{pmcid}: ERROR {exc}")
                time.sleep(0.4)
                continue

            if not WIS.search(xml):
                time.sleep(0.4)
                continue

            found += 1
            print(f"=== {pmcid} (PMID {pmid}) " + "=" * 30)
            for where, text in sections(xml):
                for m in WIS.finditer(text):
                    start, end = max(0, m.start() - 160), min(len(text), m.end() + 160)
                    where_counts[where] = where_counts.get(where, 0) + 1
                    print(f"  [{where}] …{text[start:end]}…")
                    break  # one example per section is enough
            print()
            time.sleep(0.4)

    print(f"{found} of {min(sample, len(rows))} papers mention Wisconsin")
    print("occurrences by location:")
    for where, n in sorted(where_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {where}")
    print("\nIf these are all FRONT, MCW appears only as an author affiliation")
    print("and the acknowledgment signal genuinely isn't there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
