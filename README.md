# qil-pubs

Finds publications that use QIL-supported clinical trials, by looking up each
trial's ClinicalTrials.gov NCT number. Built to run as a monthly Kubernetes
CronJob and write a spreadsheet the cancer center can read directly.

## How a trial gets matched to a paper

Three signals, because no single one is complete:

| Signal | What it is | Why it's needed |
| --- | --- | --- |
| `databank` | The registry accession the publisher declared, indexed by PubMed's `[si]` field | Most reliable, but publishers often skip it |
| `abstract` | The NCT number written in the abstract text | Catches secondary analyses and imaging sub-studies that never get a bank entry |
| `ctgov` | A publication the sponsor registered against the trial on ClinicalTrials.gov | Catches results papers PubMed can't tie to the trial at all |

Every row in the report says which signal(s) matched, so a borderline hit can be
checked by hand.

### What this list is, and what it is not

It is **publications from clinical trials the core supported**. That is the
claim the data supports, and it is the wording to use when handing the list
over. It is *not* a list of publications that acknowledge the core — see
`NOTES-local.md` for what the first full run actually found on that front.

### Did the paper actually use the core? (`QIL evidence`)

An NCT match says a paper belongs to a QIL-supported *trial*. It does not say
the work went through the core — a trial can publish plenty of papers that never
touched QIL imaging. Three positive signals narrow it:

- **`acknowledged (…)`** — the full text carries MCW Cancer Center
  shared-resource wording. MCW asks for *"Supported in part by the Medical
  College of Wisconsin Cancer Center ___ Shared Resource"*, and QIL sits under
  the Translational Metabolomics Shared Resource (TraMSR). Strongest signal, and
  invisible to PubMed, which indexes only abstracts — this needs PMC full text.
- **`qil-author (…)`** — a QIL staff surname is on the author list. Works for
  paywalled papers too. Defaults to LaViolette and Bobholz; change with
  `--qil-authors "LaViolette,Bobholz,Someone"`.
- **`mcw-author`** — some author lists the institution's affiliation, i.e. it
  was a participating site. In practice this is the only signal that fires.

**An empty cell means "unknown", not "didn't use the core."** PMC holds only
deposited papers, acknowledgment compliance is uneven, and MCW's policy folds
QIL into TraMSR without a QIL-specific string, so a compliant acknowledgment may
never say "Quantitative Imaging". Never report a blank as a negative.

Full-text fetching is **off by default** — it cost 222 requests a run and found
nothing. `--full-text` re-enables it, worth a rerun once a year in case
acknowledgment practice changes. `--skip-qil-evidence` turns the pass off
entirely.

The authoritative answer is still the lab's own service records. This is the best
available proxy, not a substitute.

### Confidence column

Every row is triaged, because a prose mention of an NCT number is weaker
evidence than a declared one:

- **`high`** — the registry or the sponsor declared the link (`databank` or
  `ctgov`). Trust it.
- **`review?`** — matched only by a prose mention, and the record looks like
  commentary: PubMed type of Review/Editorial/Meta-Analysis, or a title shaped
  like `FDA Approval Summary`, `narrative review`, `preclinical`, `real-world`,
  `indirect comparison`. Usually a paper citing the trial, not reporting it.
- **`check`** — matched only by a prose mention, not obviously commentary.

This is triage, not truth. **Nothing is dropped on the strength of it** — a
`review?` row can still be a real secondary analysis, so the column sorts the
queue rather than filtering it. In the October 2026 baseline, a hand review of
all 57 prose-only rows came out 23 keep / 26 drop / 8 ambiguous, and the
heuristic separates most of the drops.

Two deliberate exclusions:

- A paper found by free-text search that names **no** trial from our list is
  dropped. PubMed's bare-term match is loose and would otherwise pad the report.
  (`--databank-only` exists but **don't use it**: the October 2026 baseline had
  two primary trial results — CITADEL-204 and the MyPathway salivary cohort —
  that matched on the abstract alone and nowhere else.)
- ClinicalTrials.gov `BACKGROUND` references are prior literature the sponsor
  cited, not output of the trial. Add `--include-background` if the cancer
  center wants them anyway.

## Output

Written to `--out-dir`:

- `qil_publications.xlsx` — the running list. Four tabs:
  - **Publications** — one row per paper, new ones highlighted and sorted first
  - **By trial** — one row per trial including trials with zero publications, so
    coverage is visible
  - **Needs attention** — rows from the source list that couldn't be searched
  - **Run log** — counts and dates for the run
- `new_publications_YYYY-MM.csv` — just that month's additions
- `state.json` — PMIDs already reported, so "new this month" stays meaningful.
  **This file is the memory of the tool. Keep it on the PVC.** Delete it and the
  next run marks every publication as new again.

## Run it locally

```bash
pip install -e .
qil-pubs run --trials data/trials.xlsx --out-dir out --email you@example.org
```

Add `--dry-run` to write the reports without updating `state.json` — useful for
the first look, so you can re-run freely while checking results.

An [NCBI API key](https://account.ncbi.nlm.nih.gov/settings/) is optional and
raises the rate limit from 3 to 10 requests/sec. With 346 trials the run is
roughly 10 PubMed requests plus one ClinicalTrials.gov request per trial, so it
finishes in a few minutes either way.

## Rows without an NCT number

48 rows in the June 2026 list have a blank NCT cell — mostly cooperative-group
protocols (Alliance, ALCHEMIST, ECOG-ACRIN). They can't be searched as-is. To
get candidates:

```bash
qil-pubs resolve-ncts --trials data/trials.xlsx --out out/missing_ncts.csv
```

This searches ClinicalTrials.gov by protocol identifier and writes candidates
with a blank **Confirmed?** column. It guesses — confirm each one before pasting
it into the trial list. Sending the gaps back to the cancer center is the better
fix, since they hold the authoritative mapping.

## Deploy to the cluster

```bash
kubectl create namespace qil-pubs

# The trial list travels as a ConfigMap, so updating it is a one-liner.
kubectl -n qil-pubs create configmap qil-pubs-trials \
  --from-file=trials.xlsx=data/trials.xlsx

# Build for the cluster's architecture, not the Mac's.
# Images are built by .github/workflows/docker.yml; see Deploy below.
docker build --platform linux/amd64 -t ghcr.io/OWNER/qil-pubs:0.1.0 .
docker push ghcr.io/OWNER/qil-pubs:0.1.0

# Edit the image path, and the email fields if you want the summary mailed.
kubectl apply -f deploy/cronjob.yaml
```

Test it without waiting for the 1st:

```bash
kubectl -n qil-pubs create job qil-pubs-manual --from=cronjob/qil-pubs
kubectl -n qil-pubs logs -f job/qil-pubs-manual
```

Pull the spreadsheet off the PVC:

```bash
kubectl -n qil-pubs cp <pod>:/data/qil_publications.xlsx ./qil_publications.xlsx
```

When the cancer center sends an updated trial list:

```bash
kubectl -n qil-pubs create configmap qil-pubs-trials \
  --from-file=trials.xlsx=data/trials.xlsx \
  --dry-run=client -o yaml | kubectl apply -f -
```

`state.json` lives on the PVC and survives this, so the history carries over.

## Monthly email

The CronJob emails the addresses in `QIL_EMAIL_TO` on **every** run, pass or fail:

- **success** — subject `QIL trial publications — OK, N new (YYYY-MM)`, with the
  new papers listed inline (rows needing review first) and
  `qil_publications.xlsx` attached. It sends even when nothing is new, because
  silence is indistinguishable from a dead CronJob.
- **failure** — subject `… RUN FAILED (YYYY-MM)`, with the exception and the
  last 40 log lines in the body, so it can be diagnosed without digging for pod
  logs that may already have rotated. The state file is left untouched, so the
  next run finds everything the failed one would have.

Delivery needs `SMTP_HOST` in the Secret. If the relay does not advertise
STARTTLS on the port you use, set `SMTP_STARTTLS: "0"` — the mail and its xlsx
attachment then cross your network unencrypted, which is worth a thought for
publication data. A submission port (587) with `SMTP_STARTTLS: "1"` avoids it.

A relay that is down or refusing never fails the run — the files are already on
the PVC and the summary is in the pod log, so a missed email costs nothing but
the notification.

Test delivery without waiting for a Monday:

```bash
kubectl -n qil-pubs create job qil-pubs-mailtest --from=cronjob/qil-pubs
kubectl -n qil-pubs logs -f job/qil-pubs-mailtest | grep -i -e smtp -e email
```

A manually created job skips the first-week gate only if you drop
`--only-first-week`; otherwise run it during the first week of a month, or
override the args:

```bash
kubectl -n qil-pubs create job qil-pubs-mailtest --from=cronjob/qil-pubs --dry-run=client -o yaml \
  | sed '/--only-first-week/d' | kubectl apply -f -
```

### Notes on the manifest

- The schedule is **every Monday**, gated to the first 7 days by
  `--only-first-week`, which together means "first Monday". Cron ORs
  day-of-month with day-of-week when both are set, so `15 6 1-7 * 1` would fire
  far more often than intended. Non-first Mondays exit in under a second and
  make no network calls.
- `timeZone` needs Kubernetes 1.27+. On older clusters remove the line; the
  schedule is then UTC — which would put the 06:15 run at 00:15 or 01:15 local.
- `concurrencyPolicy: Forbid` prevents two runs sharing the PVC and corrupting
  state. Writes are atomic anyway, but one writer is simpler to reason about.
- The container runs non-root with a read-only root filesystem; `/data` is the
  only writable path.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

The tests mock PubMed and ClinicalTrials.gov, so they run offline and cover the
parsing edge cases that actually bite: lowercase NCT mentions, registry
accessions belonging to other people's trials, `MedlineDate` instead of a plain
year, duplicate trial rows, corrupt state files, and HTTP 429 retries.
