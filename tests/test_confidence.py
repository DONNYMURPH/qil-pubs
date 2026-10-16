"""Confidence triage, pinned against the October 2026 hand review.

Titles below are real rows from the first full run's abstract-only bucket.
"""
import pytest

from qil_pubs.pubmed import Publication
from qil_pubs.report import confidence


def pub(**kw) -> Publication:
    kw.setdefault("pmid", "1")
    return Publication(**kw)


def test_registry_evidence_outranks_everything():
    # Even a review is trustworthy if the registry declared the link.
    p = pub(evidence={"databank"}, pub_types="Review", title="A review of things")
    assert confidence(p) == "high"
    assert confidence(pub(evidence={"ctgov"})) == "high"
    assert confidence(pub(evidence={"abstract", "databank"})) == "high"


@pytest.mark.parametrize("title,types", [
    ("FDA Approval Summary: Durvalumab for the Treatment of Adult Patients with "
     "Muscle-Invasive Bladder Cancer.", "Journal Article"),
    ("Current evidence and ongoing trials for surgery versus stereotactic body "
     "radiation therapy (SBRT) for early-stage non-small cell lung cancer: a "
     "narrative review.", "Journal Article"),
    ("Preclinical Activity of the DLL3-Targeted T-cell Engager MK-6070 in "
     "Neuroendocrine Prostate Cancer.", "Journal Article"),
    ("Feasibility Study of a Network Meta-Analysis and Unanchored "
     "Population-Adjusted Indirect Treatment Comparison of Niraparib.", "Journal Article"),
    ("First Real-World Data of Olaparib Combination Treatment with Abiraterone.",
     "Journal Article"),
    ("Fruquintinib: Mechanism of Action, Clinical, and Translational Science.",
     "Journal Article"),
    ("Cytoreductive nephrectomy in the era of immune-checkpoint inhibitors: "
     "back to the future?", "Editorial"),
    ("Recent Advancements in Cell-Based Therapies in Melanoma.", "Review"),
])
def test_commentary_is_flagged_for_review(title, types):
    assert confidence(pub(evidence={"abstract"}, title=title, pub_types=types)) == "review?"


@pytest.mark.parametrize("title", [
    # Primary trial results that only ever matched on the abstract. These must
    # not be swept into the review bucket.
    "A phase 2 study of the PI3Kδ inhibitor parsaclisib in relapsed and "
    "refractory marginal zone lymphoma (CITADEL-204).",
    "Targeted therapy for advanced salivary gland carcinoma based on molecular "
    "profiling: results from MyPathway, a phase IIa multiple basket study.",
    "EV-103 dose escalation/cohort A: 5-year follow-up of enfortumab vedotin "
    "plus pembrolizumab in previously untreated locally advanced/metastatic "
    "urothelial carcinoma.",
    "Prostate-specific Antigen and Objective Response Analyses in PROpel.",
    "Population pharmacokinetics of orvacabtagene autoleucel in patients with "
    "relapsed/refractory multiple myeloma.",
])
def test_primary_trial_output_stays_in_the_check_bucket(title):
    p = pub(evidence={"abstract"}, title=title, pub_types="Clinical Trial, Phase II")
    assert confidence(p) == "check"


def test_triage_matches_the_hand_review_on_the_real_bucket():
    """Sanity check on the mix, not on any single row.

    Of the 57 abstract-only rows reviewed by hand, 26 were judged 'drop'. The
    heuristic should catch a solid share of those without touching the 23 real
    ones — if a change to the hint list breaks that balance, this fails.
    """
    drops = [
        ("Preclinical Activity of the DLL3-Targeted T-cell Engager MK-6070.", "Journal Article"),
        ("Neoadjuvant Chemo-Immunotherapy for Surgically Resectable Non-Small Cell "
         "Lung Cancer: Balancing Promise and Pitfalls.", "Review"),
        ("FDA Approval Summary: Enfortumab Vedotin plus Pembrolizumab.", "Journal Article"),
        ("Next-Generation Immune Checkpoints and Tumor Microenvironment Modulation.", "Review"),
        ("Chemotherapy in dedifferentiated chondrosarcoma.", "Review"),
        ("Surgical and focal treatment for metastatic renal cell carcinoma: A "
         "literature review.", "Review"),
        ("An Indirect Comparison of the Efficacy and Safety of Dostarlimab.", "Journal Article"),
        ("Niraparib in the maintenance treatment of advanced epithelial ovarian "
         "cancer: safety and efficacy.", "Review"),
    ]
    keeps = [
        ("Primary CITADEL-204 phase 2 results in marginal zone lymphoma.", "Clinical Trial"),
        ("PROpel non-BRCA mutation subgroup analysis.", "Clinical Trial, Phase III"),
        ("NAPOLI 3 health-related quality of life results.", "Clinical Trial, Phase III"),
        ("TRANSCEND CLL 004 meaningful change thresholds.", "Clinical Trial"),
    ]
    flagged = sum(
        1 for t, ty in drops
        if confidence(pub(evidence={"abstract"}, title=t, pub_types=ty)) == "review?"
    )
    assert flagged == len(drops), f"only flagged {flagged}/{len(drops)} known drops"
    for t, ty in keeps:
        assert confidence(pub(evidence={"abstract"}, title=t, pub_types=ty)) == "check"
