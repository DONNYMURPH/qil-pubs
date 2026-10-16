"""Core-usage grading."""
import httpx
import pytest
import respx

from qil_pubs.pubmed import Publication, PubMedClient
from qil_pubs.qil_evidence import (
    DEFAULT_QIL_AUTHORS, acknowledgment_hit, annotate, author_hit, extract_text,
)

# A PMC article shaped like the real thing: the core mention lives in <ack>,
# which is exactly where PubMed's abstract-only index cannot see it.
PMC_ACK = """<article>
 <front><article-meta><title-group><article-title>A trial</article-title></title-group></article-meta></front>
 <body><sec><title>Methods</title><p>Scans were read centrally.</p></sec></body>
 <back>
  <ack><p>Supported in part by the Medical College of Wisconsin Cancer Center
  Translational Metabolomics Shared Resource.</p></ack>
 </back>
</article>"""

PMC_QIL_NAMED = """<article><back><ack><p>We thank the Quantitative Imaging
Laboratory for segmentation support.</p></ack></back></article>"""

PMC_NO_ACK = """<article>
 <body><sec><title>Methods</title><p>Imaging performed at an outside vendor.</p></sec></body>
 <back><ack><p>We thank the patients and their families.</p></ack></back>
</article>"""


def test_acknowledgment_found_across_line_breaks():
    # The real XML wraps mid-phrase; collapsing whitespace is what makes this
    # work. TraMSR is named, which outranks the generic cancer-center wording.
    text = extract_text(PMC_ACK)
    assert "Medical College of Wisconsin Cancer Center Translational" in text
    assert acknowledgment_hit(text).lower() == "translational metabolomics shared resource"


def test_generic_cancer_center_wording_matches_when_no_resource_is_named():
    xml = ("<article><back><ack><p>Supported in part by the Medical College of "
           "Wisconsin Cancer Center Shared Resource program.</p></ack></back></article>")
    assert "medical college of wisconsin cancer center" in \
        acknowledgment_hit(extract_text(xml)).lower()


def test_qil_named_directly_is_the_most_specific_match():
    hit = acknowledgment_hit(extract_text(PMC_QIL_NAMED))
    assert "quantitative imaging" in hit.lower()


def test_unrelated_acknowledgment_is_not_a_hit():
    assert acknowledgment_hit(extract_text(PMC_NO_ACK)) == ""


def test_malformed_xml_is_survivable():
    assert extract_text("<article><unclosed>") == ""
    assert acknowledgment_hit("") == ""


@pytest.mark.parametrize("authors,expected", [
    ("Smith J; LaViolette PS; Jones A", "LaViolette"),
    ("Bobholz SA; Smith J", "Bobholz"),
    ("laviolette p", "LaViolette"),          # case-insensitive
    ("Smith J; Jones A", ""),
    ("Laviolettehausen Q", ""),              # word-boundary, not substring
])
def test_author_matching(authors, expected):
    assert author_hit(Publication(pmid="1", authors=authors), DEFAULT_QIL_AUTHORS) == expected


@respx.mock
def test_annotate_grades_each_signal_and_tallies():
    respx.get(url__regex=r".*efetch\.fcgi.*").mock(
        side_effect=lambda request: httpx.Response(
            200,
            text=PMC_ACK if request.url.params.get("id") == "PMC1" else PMC_NO_ACK,
        )
    )
    pubs = [
        # acknowledged in full text, no QIL author
        Publication(pmid="1", pmcid="PMC1", authors="Smith J"),
        # QIL co-author, no acknowledgment, MCW affiliation too
        Publication(pmid="2", pmcid="PMC2", authors="Bobholz SA; Smith J",
                    affiliations="Department of Radiology, Medical College of Wisconsin"),
        # nothing at all — unknown, not negative
        Publication(pmid="3", pmcid="PMC2", authors="Nobody X"),
        # not in PMC, so full text is unavailable; author signal still works
        Publication(pmid="4", authors="LaViolette PS"),
    ]
    with httpx.Client() as http:
        tally = annotate(pubs, PubMedClient(http, requests_per_second=1000),
                         fetch_full_text=True)

    assert "acknowledged" in pubs[0].qil_evidence
    assert "qil-author (Bobholz)" in pubs[1].qil_evidence
    assert "mcw-author" in pubs[1].qil_evidence
    assert pubs[2].qil_evidence == ""
    assert "qil-author (LaViolette)" in pubs[3].qil_evidence

    assert tally == {
        "acknowledged": 1, "qil-author": 2, "mcw-author": 1, "no signal": 1
    }


@respx.mock
def test_full_text_is_off_by_default_but_authors_still_grade():
    route = respx.get(url__regex=r".*efetch\.fcgi.*")
    pubs = [Publication(pmid="1", pmcid="PMC1", authors="LaViolette PS")]
    with httpx.Client() as http:
        tally = annotate(pubs, PubMedClient(http, requests_per_second=1000))
    assert not route.called  # no PMC requests at all
    assert "qil-author" in pubs[0].qil_evidence
    assert tally["acknowledged"] == 0


@respx.mock
def test_one_unavailable_paper_does_not_sink_the_batch(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if request.url.params.get("id") == "PMC_BAD":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=PMC_ACK)

    respx.get(url__regex=r".*efetch\.fcgi.*").mock(side_effect=flaky)
    pubs = [
        Publication(pmid="1", pmcid="PMC_BAD", authors="Smith J"),
        Publication(pmid="2", pmcid="PMC_OK", authors="Smith J"),
    ]
    with httpx.Client() as http:
        tally = annotate(pubs, PubMedClient(http, requests_per_second=1000),
                         fetch_full_text=True)
    assert pubs[0].qil_evidence == ""      # failed fetch, graded as unknown
    assert "acknowledged" in pubs[1].qil_evidence
    assert tally["acknowledged"] == 1
