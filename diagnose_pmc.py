"""Did PMC actually give us full text, or just metadata stubs?

Run:  python3 diagnose_pmc.py out/qil_publications.xlsx [sample_size]

Distinguishes two very different explanations for "0 acknowledged":
  (a) the papers carry no MCW acknowledgment  -> a real finding
  (b) PMC returned no body text to search     -> a dead end, not a finding
"""
from __future__ import annotations

import re
import sys
import time
import xml.etree.ElementTree as ET

import httpx
import openpyxl

sys.path.insert(0, "src")
from qil_pubs.qil_evidence import acknowledgment_hit, extract_text  # noqa: E402

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "out/qil_publications.xlsx"
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    ws = openpyxl.load_workbook(path)["Publications"]
    hdr = [c.value for c in ws[1]]
    pmc_i, pmid_i, auth_i = hdr.index("PMC"), hdr.index("PMID"), hdr.index("First author")
    rows = [
        (r[pmid_i].value, r[pmc_i].value, r[auth_i].value)
        for r in ws.iter_rows(min_row=2)
        if r[pmc_i].value
    ]
    print(f"{len(rows)} of {ws.max_row - 1} publications have a PMCID")
    if not rows:
        print("No PMCIDs at all — the acknowledgment signal cannot work. Stop here.")
        return 0

    stats = {"body": 0, "ack": 0, "wisconsin": 0, "empty": 0, "error": 0}
    with httpx.Client(follow_redirects=True) as http:
        for pmid, pmcid, author in rows[:sample]:
            try:
                r = http.get(
                    f"{BASE}/efetch.fcgi",
                    params={"db": "pmc", "id": pmcid, "retmode": "xml",
                            "tool": "qil-pubs-diag"},
                    timeout=60.0,
                )
                r.raise_for_status()
                xml = r.text
            except Exception as exc:  # noqa: BLE001 - diagnostic, report anything
                print(f"  {pmcid:<12} ERROR {exc}")
                stats["error"] += 1
                time.sleep(0.4)
                continue

            has_body = "<body" in xml
            has_ack = "<ack" in xml
            scanned = extract_text(xml)
            wis = bool(re.search(r"wisconsin", xml, re.I))
            hit = acknowledgment_hit(scanned)

            stats["body"] += has_body
            stats["ack"] += has_ack
            stats["wisconsin"] += wis
            if not scanned:
                stats["empty"] += 1

            print(
                f"  {pmcid:<12} body={'Y' if has_body else 'n'} "
                f"ack={'Y' if has_ack else 'n'} "
                f"scanned={len(scanned):>6}ch "
                f"wisconsin={'Y' if wis else 'n'}"
                + (f"  HIT: {hit}" if hit else "")
            )
            time.sleep(0.4)

    n = min(sample, len(rows))
    print(f"\nOf {n} sampled:")
    print(f"  full text (<body>) returned : {stats['body']}")
    print(f"  acknowledgment section      : {stats['ack']}")
    print(f"  mentions 'Wisconsin' anywhere: {stats['wisconsin']}")
    print(f"  nothing scannable           : {stats['empty']}")
    print(f"  fetch errors                : {stats['error']}")

    print("\nVerdict:")
    if stats["body"] == 0:
        print("  PMC returned NO full text. These papers are outside the open-access")
        print("  subset, so acknowledgments are unreachable this way. '0 acknowledged'")
        print("  is a dead end, not a finding. Use --skip-full-text.")
    elif stats["ack"] == 0:
        print("  Full text came through but no acknowledgment sections exist in it.")
        print("  Odd; worth eyeballing one article on the PMC website.")
    elif stats["wisconsin"] == 0:
        print("  Full text and acknowledgments are present, and MCW is not mentioned.")
        print("  That IS a real finding: these papers don't credit the institution.")
    else:
        print("  'Wisconsin' appears but no pattern matched — the acknowledgment")
        print("  wording differs from what we search for. Paste a sample and we'll")
        print("  widen the patterns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
