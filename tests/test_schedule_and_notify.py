"""First-Monday gating, success email, and failure notification."""
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from qil_pubs import cli
from qil_pubs.cli import is_first_week, main

FIXTURES = Path(__file__).parent / "fixtures"
TRIALS = FIXTURES / "trials.xlsx"

EMPTY_SEARCH = {"esearchresult": {"count": "0", "idlist": []}}


@pytest.mark.parametrize("day,expected", [
    (1, True), (2, True), (7, True),     # a first Monday always lands here
    (8, False), (14, False), (30, False),  # later Mondays must not run
])
def test_first_week_gate(day, expected):
    assert is_first_week(datetime(2026, 11, day)) is expected


def test_gate_is_true_for_every_real_first_monday_of_2027():
    """The gate must never skip an actual first Monday."""
    import calendar
    for month in range(1, 13):
        first_monday = next(
            day for day, weekday in (
                (d, calendar.weekday(2027, month, d)) for d in range(1, 8)
            ) if weekday == calendar.MONDAY
        )
        assert is_first_week(datetime(2027, month, first_monday)), (
            f"2027-{month:02d}-{first_monday:02d} would have been skipped"
        )


def test_gate_skips_the_run_entirely(tmp_path, monkeypatch):
    """On a non-first Monday the job exits 0 without touching the network."""
    monkeypatch.setattr(cli, "is_first_week", lambda *a, **k: False)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200))
        rc = main(["run", "--trials", str(TRIALS), "--out-dir", str(tmp_path),
                   "--only-first-week"])
    assert rc == 0
    assert not route.called
    assert not (tmp_path / "qil_publications.xlsx").exists()


def test_gate_allows_the_run_in_the_first_week(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "is_first_week", lambda *a, **k: True)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*esearch\.fcgi.*").mock(
            return_value=httpx.Response(200, json=EMPTY_SEARCH))
        mock.get(url__regex=r".*efetch\.fcgi.*").mock(
            return_value=httpx.Response(200, text="<PubmedArticleSet/>"))
        mock.get(url__regex=r".*clinicaltrials\.gov.*").mock(
            return_value=httpx.Response(200, json={}))
        rc = main(["run", "--trials", str(TRIALS), "--out-dir", str(tmp_path),
                   "--state", str(tmp_path / "state.json"), "--only-first-week"])
    assert rc == 0
    assert (tmp_path / "qil_publications.xlsx").exists()


def test_failure_emails_the_owner_and_exits_nonzero(tmp_path, monkeypatch):
    sent = {}

    def fake_send(to, subject, body, attachments=None):
        sent.update(to=to, subject=subject, body=body)
        return True

    monkeypatch.setattr(cli, "send_email", fake_send)
    # A trial list that cannot be parsed is a realistic failure: someone
    # replaces the ConfigMap with a reordered export.
    bad = tmp_path / "bad.xlsx"
    bad.write_text("not a spreadsheet")

    rc = main(["run", "--trials", str(bad), "--out-dir", str(tmp_path),
               "--email-to", "owner@example.org"])

    assert rc == 1
    assert sent["to"] == "owner@example.org"
    assert "RUN FAILED" in sent["subject"]
    # The email must carry enough to diagnose without pod logs.
    assert "state file is unchanged" in sent["body"]
    assert "Last log lines:" in sent["body"]


def test_success_emails_even_when_nothing_is_new(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        cli, "send_email",
        lambda to, subject, body, attachments=None: sent.update(
            to=to, subject=subject, body=body, attachments=attachments) or True,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*esearch\.fcgi.*").mock(
            return_value=httpx.Response(200, json=EMPTY_SEARCH))
        mock.get(url__regex=r".*efetch\.fcgi.*").mock(
            return_value=httpx.Response(200, text="<PubmedArticleSet/>"))
        mock.get(url__regex=r".*clinicaltrials\.gov.*").mock(
            return_value=httpx.Response(200, json={}))
        rc = main(["run", "--trials", str(TRIALS), "--out-dir", str(tmp_path),
                   "--state", str(tmp_path / "state.json"),
                   "--email-to", "owner@example.org"])

    assert rc == 0
    assert "OK, 0 new" in sent["subject"]
    assert "finished successfully" in sent["body"]
    assert "No new publications this month." in sent["body"]
    # Silence would be indistinguishable from a dead CronJob, so the xlsx
    # still goes out on a quiet month.
    assert len(sent["attachments"]) == 1


def test_dry_run_is_called_out_in_the_email(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        cli, "send_email",
        lambda to, subject, body, attachments=None: sent.update(body=body) or True,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*esearch\.fcgi.*").mock(
            return_value=httpx.Response(200, json=EMPTY_SEARCH))
        mock.get(url__regex=r".*efetch\.fcgi.*").mock(
            return_value=httpx.Response(200, text="<PubmedArticleSet/>"))
        mock.get(url__regex=r".*clinicaltrials\.gov.*").mock(
            return_value=httpx.Response(200, json={}))
        main(["run", "--trials", str(TRIALS), "--out-dir", str(tmp_path),
              "--state", str(tmp_path / "state.json"), "--dry-run",
              "--email-to", "owner@example.org"])
    assert "dry run" in sent["body"]


# --- SMTP delivery against a plain relay's quirks ---------------------

class FakeSMTP:
    """Stands in for smtplib.SMTP. Records what the sender did."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.starttls_called = False
        self.logged_in = False
        self.sent = []
        self.starttls_error = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True
        if self.starttls_error:
            raise self.starttls_error

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    import smtplib
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_STARTTLS", "SMTP_FROM",
                "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    return FakeSMTP


def test_campus_relay_settings_send_without_tls_or_auth(smtp, monkeypatch, tmp_path):
    """relay.example.org on 25: no auth, STARTTLS explicitly off."""
    from qil_pubs.notify import send_email
    monkeypatch.setenv("SMTP_HOST", "relay.example.org")
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.setenv("SMTP_STARTTLS", "0")
    monkeypatch.setenv("SMTP_FROM", "qil-pubs@example.org")

    attachment = tmp_path / "qil_publications.xlsx"
    attachment.write_bytes(b"PK\x03\x04fake")

    assert send_email("owner@example.org", "subj", "body", [attachment]) is True
    sender = smtp.instances[0]
    assert (sender.host, sender.port) == ("relay.example.org", 25)
    assert sender.starttls_called is False   # disabled by config
    assert sender.logged_in is False         # relay takes no auth
    msg = sender.sent[0]
    assert msg["To"] == "owner@example.org"
    assert msg["From"] == "qil-pubs@example.org"
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert "qil_publications.xlsx" in names


def test_relay_refusing_starttls_still_delivers(smtp, monkeypatch):
    """If STARTTLS is left on by mistake, delivery must not fail."""
    import smtplib
    from qil_pubs.notify import send_email
    monkeypatch.setenv("SMTP_HOST", "relay.example.org")
    monkeypatch.setenv("SMTP_STARTTLS", "1")

    original = FakeSMTP.__init__

    def init_with_error(self, *a, **kw):
        original(self, *a, **kw)
        self.starttls_error = smtplib.SMTPNotSupportedError("no STARTTLS")

    monkeypatch.setattr(FakeSMTP, "__init__", init_with_error)
    assert send_email("owner@example.org", "s", "b") is True
    assert smtp.instances[0].sent, "message was not sent after STARTTLS refusal"


def test_multiple_recipients_are_split(smtp, monkeypatch):
    from qil_pubs.notify import send_email
    monkeypatch.setenv("SMTP_HOST", "relay.example.org")
    send_email("owner@example.org, second@example.org", "s", "b")
    assert smtp.instances[0].sent[0]["To"] == "owner@example.org, second@example.org"


def test_no_host_configured_logs_instead_of_failing(smtp, caplog):
    from qil_pubs.notify import send_email
    with caplog.at_level("INFO"):
        assert send_email("owner@example.org", "s", "the summary body") is False
    assert not smtp.instances
    assert "the summary body" in caplog.text  # still recoverable from the log


def test_unreachable_relay_does_not_fail_the_run(smtp, monkeypatch):
    """Reports are already written; a dead relay must not make the job fail."""
    import smtplib
    from qil_pubs.notify import send_email
    monkeypatch.setenv("SMTP_HOST", "relay.example.org")
    monkeypatch.setattr(
        smtplib, "SMTP",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("connection refused")),
    )
    assert send_email("owner@example.org", "s", "b") is False
