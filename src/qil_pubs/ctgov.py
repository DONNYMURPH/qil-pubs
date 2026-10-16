"""ClinicalTrials.gov API v2 client.

Two jobs:

* ``references`` — the publications the sponsor registered against a trial.
  PubMed's ``[si]`` index misses papers whose publisher never declared the
  registry accession, so this catches results papers PubMed alone would not tie
  to the trial.
* ``find_nct`` — a best-effort lookup of the NCT number for the rows the cancer
  center left blank (mostly cooperative-group protocols). Output is for human
  review, never trusted silently.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

BASE = "https://clinicaltrials.gov/api/v2"

#: Reference types ClinicalTrials.gov assigns. BACKGROUND citations are prior
#: literature the sponsor cited, not output of the trial, so they are excluded
#: by default.
RESULT_TYPES = {"RESULT", "DERIVED"}


@dataclass
class Reference:
    pmid: str
    type: str
    citation: str


class CtGovClient:
    def __init__(self, client: httpx.Client, requests_per_second: float = 3.0) -> None:
        self.client = client
        self.min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                r = self.client.get(f"{BASE}{path}", params=params or {}, timeout=60.0)
                self._last_call = time.monotonic()
                if r.status_code == 404:
                    return {}
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{r.status_code} from {path}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                self._last_call = time.monotonic()
                time.sleep(2.0 * (2**attempt))
                log.warning("ctgov %s failed (%d/3): %s", path, attempt + 1, exc)
        log.error("ctgov %s failed permanently: %s", path, last_error)
        return {}

    def references(
        self, nct: str, include_background: bool = False
    ) -> list[Reference]:
        data = self._get(
            f"/studies/{nct}",
            {"fields": "protocolSection.referencesModule"},
        )
        refs = (
            data.get("protocolSection", {})
            .get("referencesModule", {})
            .get("references", [])
        )
        out = []
        for ref in refs:
            pmid = str(ref.get("pmid", "")).strip()
            kind = str(ref.get("type", "")).upper()
            if not pmid:
                continue
            if not include_background and kind not in RESULT_TYPES:
                continue
            out.append(Reference(pmid=pmid, type=kind, citation=ref.get("citation", "")))
        return out

    def find_nct(self, protocol: str) -> list[tuple[str, str]]:
        """Guess NCT numbers for a protocol identifier. Returns (nct, title)."""
        term = _protocol_token(protocol)
        if not term:
            return []
        data = self._get(
            "/studies",
            {
                "query.term": term,
                "fields": "protocolSection.identificationModule",
                "pageSize": "5",
            },
        )
        hits = []
        for study in data.get("studies", []):
            ident = study.get("protocolSection", {}).get("identificationModule", {})
            nct = ident.get("nctId", "")
            if nct:
                hits.append((nct, ident.get("briefTitle", "")))
        return hits


def _protocol_token(protocol: str) -> str:
    """Pull the likely study identifier out of a protocol string.

    'mcwqil: ALLIANCE-A071102' -> 'A071102'
    'mcwqil: Alliance A071801 PRO#036673  OCRICC20-0116' -> 'A071801'
    """
    text = re.sub(r"^\s*mcwqil\s*:\s*", "", protocol, flags=re.I)
    # Drop local tracking numbers, which are noise to ClinicalTrials.gov.
    text = re.sub(r"\b(PRO|IRB|OCRICC|CIRB|RR)\s*#?\s*[\w-]+", " ", text, flags=re.I)
    text = re.sub(r"#\s*[\w-]+", " ", text)
    parts = [p for p in re.split(r"[\s,;]+", text) if p]
    # A cooperative-group protocol id carries digits, e.g. A071102, E4512.
    for part in parts:
        token = part.strip("-_.")
        if any(c.isdigit() for c in token) and len(token) >= 4:
            return token
    return parts[0] if parts else ""
