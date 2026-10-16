from pathlib import Path

import openpyxl
import pytest

from qil_pubs.ctgov import _protocol_token
from qil_pubs.pubmed import parse_efetch
from qil_pubs.report import State
from qil_pubs.trials import load_trials, searchable

EFETCH = """<?xml version="1.0"?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">35000001</PMID>
   <Article>
    <Journal>
      <ISOAbbreviation>J Clin Oncol</ISOAbbreviation>
      <JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue>
    </Journal>
    <ArticleTitle>Camidanlumab tesirine in relapsed Hodgkin
      lymphoma</ArticleTitle>
    <ELocationID EIdType="doi" ValidYN="Y">10.1200/JCO.23.00001</ELocationID>
    <Abstract><AbstractText>A phase 1 study of ADCT-301.</AbstractText></Abstract>
    <AuthorList>
      <Author><LastName>Hamadani</LastName><Initials>M</Initials></Author>
      <Author><LastName>Collins</LastName><Initials>GP</Initials></Author>
    </AuthorList>
    <PublicationTypeList><PublicationType>Clinical Trial, Phase I</PublicationType></PublicationTypeList>
    <DataBankList>
      <DataBank>
        <DataBankName>ClinicalTrials.gov</DataBankName>
        <AccessionNumberList>
          <AccessionNumber>NCT02432235</AccessionNumber>
          <AccessionNumber>NCT09999999</AccessionNumber>
        </AccessionNumberList>
      </DataBank>
    </DataBankList>
   </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
    <ArticleId IdType="pubmed">35000001</ArticleId>
    <ArticleId IdType="pmc">PMC1234567</ArticleId>
  </ArticleIdList></PubmedData>
 </PubmedArticle>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">35000002</PMID>
   <Article>
    <Journal>
      <ISOAbbreviation>Blood</ISOAbbreviation>
      <JournalIssue><PubDate><MedlineDate>2023 Spring</MedlineDate></PubDate></JournalIssue>
    </Journal>
    <ArticleTitle>Secondary imaging analysis</ArticleTitle>
    <Abstract><AbstractText>Patients enrolled on nct03589469 were re-read.</AbstractText></Abstract>
    <AuthorList>
      <Author><CollectiveName>Alliance Investigators</CollectiveName></Author>
    </AuthorList>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">35000003</PMID>
   <Article>
    <Journal><ISOAbbreviation>Nature</ISOAbbreviation></Journal>
    <ArticleTitle>Unrelated paper citing someone else's trial</ArticleTitle>
    <Abstract><AbstractText>See NCT07777777 for details.</AbstractText></Abstract>
   </Article>
  </MedlineCitation>
 </PubmedArticle>
</PubmedArticleSet>
"""

KNOWN = {"NCT02432235", "NCT03589469"}


def test_databank_attribution_only_counts_our_trials():
    pubs = {p.pmid: p for p in parse_efetch(EFETCH, KNOWN)}
    one = pubs["35000001"]
    assert one.ncts == {"NCT02432235"}  # NCT09999999 is not ours
    assert one.evidence == {"databank"}
    assert one.journal == "J Clin Oncol"
    assert one.year == "2024"
    assert one.doi == "10.1200/JCO.23.00001"
    assert one.pmcid == "PMC1234567"
    assert one.first_author == "Hamadani M"
    # multi-line titles in the XML must collapse to one line
    assert "\n" not in one.title and "lymphoma" in one.title


def test_lowercase_abstract_mention_is_matched():
    pubs = {p.pmid: p for p in parse_efetch(EFETCH, KNOWN)}
    two = pubs["35000002"]
    assert two.ncts == {"NCT03589469"}
    assert two.evidence == {"abstract"}
    assert two.year == "2023"  # pulled from MedlineDate
    assert two.first_author == "Alliance Investigators"


def test_paper_naming_only_a_foreign_trial_is_not_attributed():
    pubs = {p.pmid: p for p in parse_efetch(EFETCH, KNOWN)}
    assert pubs["35000003"].ncts == set()


def _sheet(tmp_path: Path, rows: list[list[str]]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Trial Protocolnumber", "Trial Nctnumber", "Trial Phase",
               "Trial Name", "Trial Trialstatus"])
    for r in rows:
        ws.append(r)
    path = tmp_path / "trials.xlsx"
    wb.save(path)
    return path


def test_trial_loading_flags_unsearchable_rows(tmp_path):
    path = _sheet(tmp_path, [
        ["mcwqil: A-1", "NCT02432235", "II", "Study A", "Active"],
        ["mcwqil: A-2", "", "III", "Study B", "Active"],
        ["mcwqil: A-3", " nct03589469 ", "I", "Study C", "Closed"],
        ["mcwqil: A-4", "pending", "I", "Study D", "Draft"],
        ["mcwqil: A-5", "NCT02432235", "II", "Study A duplicate", "Active"],
        [None, None, None, None, None],
    ])
    trials = load_trials(path)
    assert len(trials) == 5  # blank row skipped
    assert trials[0].nct == "NCT02432235"
    assert trials[1].notes == ["no NCT number in source list"]
    assert trials[2].nct == "NCT03589469"  # lowercase + whitespace normalized
    assert "unrecognized" in trials[3].notes[0]

    by_nct = searchable(trials)
    assert set(by_nct) == {"NCT02432235", "NCT03589469"}
    assert by_nct["NCT02432235"].name == "Study A"  # first listing wins


def test_missing_nct_column_is_an_error(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["Protocol", "Phase"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    with pytest.raises(ValueError, match="Nctnumber"):
        load_trials(path)


def test_state_marks_only_unseen_pmids_as_new(tmp_path):
    state = State.load(tmp_path / "state.json")
    assert state.record(["1", "2"], "2026-10-15") == ["1", "2"]
    state.save()

    again = State.load(tmp_path / "state.json")
    assert again.record(["1", "2", "3"], "2026-11-15") == ["3"]
    assert again.first_seen["1"] == "2026-10-15"  # original date preserved
    assert again.last_run


def test_corrupt_state_does_not_crash_the_run(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    state = State.load(p)
    assert state.record(["1"], "2026-10-15") == ["1"]


@pytest.mark.parametrize("protocol,expected", [
    ("mcwqil: ALLIANCE-A071102", "ALLIANCE-A071102"),
    ("mcwqil: Alliance A071801 PRO#036673  OCRICC20-0116", "A071801"),
    ("mcwqil: ALCHEMIST-E4512 IRB24887", "ALCHEMIST-E4512"),
    ("mcwqil: ADCT-301-001 IRB# 24871 OCRICC#15-0268", "ADCT-301-001"),
])
def test_protocol_token_strips_local_tracking_numbers(protocol, expected):
    assert _protocol_token(protocol) == expected
