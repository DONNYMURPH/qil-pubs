"""PubMed E-utilities client.

Discovery is done with batched ``esearch`` queries, then a single ``efetch`` per
batch of PMIDs supplies both the citation metadata and the NCT attribution.

Two things in a PubMed record can tie a paper to a trial:

* ``<DataBankList>`` — the registry accession numbers the publisher declared.
  This is what the ``[si]`` search field indexes. It is authoritative but
  incomplete: plenty of secondary analyses never get the bank entry.
* the abstract text — many papers only name the NCT number in prose.

We search both ways and attribute hits from whichever source names the trial, so
a paper found by free text still lands against the right NCT.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from .trials import NCT_RE

log = logging.getLogger(__name__)

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@dataclass
class Publication:
    pmid: str
    title: str = ""
    journal: str = ""
    year: str = ""
    doi: str = ""
    pmcid: str = ""
    first_author: str = ""
    authors: str = ""
    pub_types: str = ""
    #: Author affiliation strings, joined. Free with the record we already
    #: fetch, and the cheapest hint that MCW was involved at all.
    affiliations: str = ""
    #: Why this paper looks like it used the core; see qil_evidence.
    qil_evidence: str = ""
    #: NCT numbers from our trial list that this paper names
    ncts: set[str] = field(default_factory=set)
    #: how each NCT was matched: "databank", "abstract", "ctgov"
    evidence: set[str] = field(default_factory=set)

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    def merge(self, other: "Publication") -> None:
        self.ncts |= other.ncts
        self.evidence |= other.evidence
        for attr in (
            "title", "journal", "year", "doi", "pmcid",
            "first_author", "authors", "pub_types", "affiliations", "qil_evidence",
        ):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))


class PubMedClient:
    """Thin E-utilities wrapper that respects NCBI's rate and courtesy rules."""

    def __init__(
        self,
        client: httpx.Client,
        tool: str = "qil-pubs",
        email: str = "",
        api_key: str = "",
        requests_per_second: float | None = None,
    ) -> None:
        self.client = client
        self.tool = tool
        self.email = email
        self.api_key = api_key
        # NCBI allows 3 requests/sec anonymously, 10/sec with an API key.
        self.min_interval = 1.0 / (requests_per_second or (10.0 if api_key else 3.0))
        self._last_call = 0.0

    def _params(self, **kw: str | int) -> dict[str, str]:
        p = {"tool": self.tool, **{k: str(v) for k, v in kw.items()}}
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def _get(self, endpoint: str, **kw: str | int) -> httpx.Response:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                r = self.client.get(
                    f"{BASE}/{endpoint}", params=self._params(**kw), timeout=60.0
                )
                self._last_call = time.monotonic()
                # 429 and 5xx are transient; NCBI throttles aggressively at peak.
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{r.status_code} from {endpoint}", request=r.request, response=r
                    )
                r.raise_for_status()
                return r
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                self._last_call = time.monotonic()
                backoff = 2.0 * (2**attempt)
                log.warning(
                    "%s failed (attempt %d/4): %s — retrying in %.0fs",
                    endpoint, attempt + 1, exc, backoff,
                )
                time.sleep(backoff)
        raise RuntimeError(f"{endpoint} failed after 4 attempts") from last_error

    def search_ncts(
        self, ncts: list[str], batch_size: int = 40, free_text: bool = True
    ) -> set[str]:
        """Return PMIDs of papers naming any of these NCT numbers.

        ``[si]`` matches the declared registry accession; the bare term matches
        anywhere in the record, which is how prose mentions are caught. Queries
        are OR-batched because one request per trial would be 346 requests a run
        for no benefit.
        """
        pmids: set[str] = set()
        for i in range(0, len(ncts), batch_size):
            chunk = ncts[i : i + batch_size]
            if free_text:
                term = " OR ".join(f"{n}[si] OR {n}" for n in chunk)
            else:
                term = " OR ".join(f"{n}[si]" for n in chunk)
            retstart = 0
            while True:
                data = self._get(
                    "esearch.fcgi",
                    db="pubmed",
                    term=term,
                    retmode="json",
                    retmax=500,
                    retstart=retstart,
                ).json()
                result = data.get("esearchresult", {})
                if err := result.get("ERROR"):
                    raise RuntimeError(f"esearch error: {err}")
                ids = result.get("idlist", [])
                pmids.update(ids)
                retstart += len(ids)
                if len(ids) < 500 or retstart >= int(result.get("count", 0)):
                    break
            log.info(
                "searched trials %d-%d of %d: %d PMIDs so far",
                i + 1, i + len(chunk), len(ncts), len(pmids),
            )
        return pmids

    def fetch(
        self, pmids: list[str], known_ncts: set[str], batch_size: int = 200
    ) -> dict[str, Publication]:
        """Fetch citation metadata and attribute each paper to our trials."""
        out: dict[str, Publication] = {}
        ordered = sorted(pmids)
        for i in range(0, len(ordered), batch_size):
            chunk = ordered[i : i + batch_size]
            xml = self._get(
                "efetch.fcgi", db="pubmed", id=",".join(chunk), retmode="xml"
            ).text
            for pub in parse_efetch(xml, known_ncts):
                if pub.pmid in out:
                    out[pub.pmid].merge(pub)
                else:
                    out[pub.pmid] = pub
            log.info("fetched %d-%d of %d records", i + 1, i + len(chunk), len(ordered))
        return out


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def parse_efetch(xml: str, known_ncts: set[str]) -> list[Publication]:
    """Parse an efetch PubmedArticleSet into Publications.

    Only NCT numbers present in ``known_ncts`` are attributed — papers routinely
    cite trials that have nothing to do with our core, and those must not inflate
    the report.
    """
    root = ET.fromstring(xml)
    pubs: list[Publication] = []

    for art in root.iter("PubmedArticle"):
        citation = art.find("MedlineCitation")
        if citation is None:
            continue
        pmid = _text(citation.find("PMID"))
        if not pmid:
            continue
        article = citation.find("Article")
        pub = Publication(pmid=pmid)

        if article is not None:
            pub.title = _text(article.find("ArticleTitle"))
            journal = article.find("Journal")
            if journal is not None:
                pub.journal = _text(journal.find("ISOAbbreviation")) or _text(
                    journal.find("Title")
                )
                pub.year = (
                    _text(journal.find("./JournalIssue/PubDate/Year"))
                    or _text(journal.find("./JournalIssue/PubDate/MedlineDate"))[:4]
                )
            for eloc in article.findall("ELocationID"):
                if eloc.get("EIdType") == "doi" and not pub.doi:
                    pub.doi = _text(eloc)

            names, affils = [], []
            for author in article.findall("./AuthorList/Author"):
                last, initials = _text(author.find("LastName")), _text(
                    author.find("Initials")
                )
                collective = _text(author.find("CollectiveName"))
                if last:
                    names.append(f"{last} {initials}".strip())
                elif collective:
                    names.append(collective)
                for aff in author.findall("./AffiliationInfo/Affiliation"):
                    value = _text(aff)
                    if value and value not in affils:
                        affils.append(value)
            pub.affiliations = " | ".join(affils)
            pub.authors = "; ".join(names)
            pub.first_author = names[0] if names else ""
            pub.pub_types = "; ".join(
                _text(pt) for pt in article.findall("./PublicationTypeList/PublicationType")
            )

            # Declared registry accessions — the strongest signal.
            for acc in article.findall("./DataBankList/DataBank/AccessionNumberList/AccessionNumber"):
                value = _text(acc).upper()
                if value in known_ncts:
                    pub.ncts.add(value)
                    pub.evidence.add("databank")

        for id_node in art.findall("./PubmedData/ArticleIdList/ArticleId"):
            kind = id_node.get("IdType")
            if kind == "doi" and not pub.doi:
                pub.doi = _text(id_node)
            elif kind == "pmc":
                pub.pmcid = _text(id_node)

        # Prose mentions, chiefly in the abstract.
        haystack = " ".join(
            _text(n)
            for n in (
                citation.findall("./Article/Abstract/AbstractText")
                + citation.findall("./OtherAbstract/AbstractText")
            )
        )
        for found in set(NCT_RE.findall(haystack.upper())):
            if found in known_ncts:
                pub.ncts.add(found)
                pub.evidence.add("abstract")

        pubs.append(pub)

    return pubs
