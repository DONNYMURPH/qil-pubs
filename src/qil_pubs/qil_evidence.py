"""Grade how likely a publication actually used the Quantitative Imaging Lab.

The NCT match tells us a paper belongs to a QIL-supported *trial*. It says
nothing about whether that paper's work went through the core. Three signals
get us closer:

``acknowledged``
    The paper's full text carries MCW Cancer Center shared-resource
    acknowledgment wording. MCW asks authors for "Supported in part by the
    Medical College of Wisconsin Cancer Center ___ Shared Resource", and QIL
    sits under the Translational Metabolomics Shared Resource. Strongest
    available signal, and only visible in full text — PubMed indexes abstracts.

``qil-author``
    A QIL staff member is a co-author. Independent of PMC, so it works for
    paywalled papers too.

``mcw-author``
    Some author lists an MCW affiliation, i.e. MCW was a participating site on
    the paper. Not proof the core was used, but it is the one signal that fires
    in practice.

A critical asymmetry: **a paper with no signal has not been ruled out.** PMC
holds only deposited papers, acknowledgment compliance is uneven, and MCW's own
policy folds QIL into TraMSR without giving it a distinct string, so a fully
compliant acknowledgment may never say "Quantitative Imaging". Treat these as
positive evidence only; the absence means "unknown", not "no".

Measured on the October 2026 baseline (340 publications, 222 with PMCIDs):

===================  =====  ====================================================
signal               count  finding
===================  =====  ====================================================
``acknowledged``         0  Full text fetched fine — 15/15 bodies, 13/15 with an
                            ``<ack>`` section. Of 40 papers sampled, 8 mentioned
                            Wisconsin and every one was an author affiliation or
                            a cooperative-group site roster ("Froedtert and the
                            Medical College of Wisconsin LAPS"). No paper credits
                            an MCW shared resource.
``qil-author``           0  Expected: these are pharma registration trials, where
                            authorship goes to investigators and sponsor staff,
                            not to the imaging core doing central reads.
``mcw-author``          66  MCW was a participating site.
===================  =====  ====================================================

Because of that, full-text fetching is **off by default** — it was 222 requests
a run for zero signal. ``fetch_full_text=True`` re-enables it, worth rerunning
once a year in case acknowledgment practice changes.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from .pubmed import Publication, PubMedClient

log = logging.getLogger(__name__)

#: Default QIL staff surnames to match in author lists.
DEFAULT_QIL_AUTHORS = ("LaViolette", "Bobholz")

#: Acknowledgment wording, in descending order of specificity. Matched against
#: full text, case-insensitively, with whitespace collapsed first.
ACK_PATTERNS = (
    r"quantitative imaging (?:lab|laboratory)",
    r"translational metabolomics shared resource",
    r"\btramsr\b",
    r"medical college of wisconsin cancer center[^.]{0,80}shared resource",
    r"shared resource[^.]{0,80}medical college of wisconsin cancer center",
)

MCW_AFFILIATION = re.compile(r"medical college of wisconsin", re.I)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def acknowledgment_hit(full_text: str) -> str:
    """Return the acknowledgment pattern that matched, or ''."""
    haystack = _collapse(full_text)
    for pattern in ACK_PATTERNS:
        if (m := re.search(pattern, haystack, re.I)):
            return m.group()[:120]
    return ""


def extract_text(pmc_xml: str) -> str:
    """Pull acknowledgment-bearing text out of a PMC article.

    Acknowledgments live in ``<ack>``, but funding statements also turn up in
    ``<fn>`` footnotes and ``<funding-statement>``, and some journals bury the
    core mention in the methods. We concatenate those rather than the whole
    article, which keeps the match specific and the memory small.
    """
    try:
        root = ET.fromstring(pmc_xml)
    except ET.ParseError as exc:
        log.debug("unparseable PMC XML: %s", exc)
        return ""
    parts: list[str] = []
    for tag in ("ack", "funding-statement", "fn", "notes"):
        for node in root.iter(tag):
            parts.append(" ".join(node.itertext()))
    # Methods sections mention cores often enough to be worth scanning.
    for sec in root.iter("sec"):
        title = sec.find("title")
        if title is not None and re.search(
            r"method|material|acknowled|funding", " ".join(title.itertext()), re.I
        ):
            parts.append(" ".join(sec.itertext()))
    return _collapse(" ".join(parts))


def author_hit(pub: Publication, surnames: tuple[str, ...]) -> str:
    for surname in surnames:
        if re.search(rf"\b{re.escape(surname)}\b", pub.authors, re.I):
            return surname
    return ""


def annotate(
    pubs: list[Publication],
    client: PubMedClient,
    qil_authors: tuple[str, ...] = DEFAULT_QIL_AUTHORS,
    fetch_full_text: bool = False,
) -> dict[str, int]:
    """Set ``qil_evidence`` on each publication. Returns a tally by signal."""
    tally = {
        "acknowledged": 0, "qil-author": 0, "mcw-author": 0, "no signal": 0
    }

    with_pmc = [p for p in pubs if p.pmcid] if fetch_full_text else []
    full_text: dict[str, str] = {}
    if with_pmc:
        log.info(
            "checking full text of %d of %d papers for acknowledgments "
            "(the rest are not in PMC)",
            len(with_pmc), len(pubs),
        )
        for i, pub in enumerate(with_pmc, start=1):
            try:
                xml = client._get(
                    "efetch.fcgi", db="pmc", id=pub.pmcid, retmode="xml"
                ).text
            except RuntimeError as exc:
                # One unavailable paper must not sink the run.
                log.warning("could not fetch %s: %s", pub.pmcid, exc)
                continue
            full_text[pub.pmid] = extract_text(xml)
            if i % 25 == 0:
                log.info("full text: %d/%d", i, len(with_pmc))

    for pub in pubs:
        signals: list[str] = []
        if (hit := acknowledgment_hit(full_text.get(pub.pmid, ""))):
            signals.append(f"acknowledged ({hit})")
            tally["acknowledged"] += 1
        if (who := author_hit(pub, qil_authors)):
            signals.append(f"qil-author ({who})")
            tally["qil-author"] += 1
        if MCW_AFFILIATION.search(pub.affiliations):
            signals.append("mcw-author")
            tally["mcw-author"] += 1
        pub.qil_evidence = "; ".join(signals)
        if not signals:
            tally["no signal"] += 1

    return tally
