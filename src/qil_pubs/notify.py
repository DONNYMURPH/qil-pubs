"""Optional email summary over plain SMTP.

Configured entirely by environment variables so the CronJob can pick them up
from a Secret. If SMTP_HOST is unset the summary is logged instead of sent,
which is the sane default until the lab's relay details are filled in.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str, attachments: list[Path] | None = None) -> bool:
    host = os.environ.get("SMTP_HOST", "")
    if not host:
        log.info("SMTP_HOST not set — summary not emailed. Summary follows:\n%s", body)
        return False

    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "qil-pubs@localhost")
    msg["To"] = ", ".join(a.strip() for a in to.split(",") if a.strip())
    msg["Subject"] = subject
    msg.set_content(body)

    for path in attachments or []:
        p = Path(path)
        if not p.exists():
            continue
        msg.add_attachment(
            p.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=p.name,
        )

    port = int(os.environ.get("SMTP_PORT", "25"))
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if os.environ.get("SMTP_STARTTLS", "1") == "1":
                try:
                    smtp.starttls()
                except smtplib.SMTPNotSupportedError:
                    # The campus relay does not offer STARTTLS on port 25.
                    log.warning(
                        "%s does not support STARTTLS — sending unencrypted", host
                    )
                except smtplib.SMTPException as exc:
                    log.warning("STARTTLS failed on %s (%s) — sending "
                                "unencrypted", host, exc)
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        log.info("emailed summary to %s", msg["To"])
        return True
    except (smtplib.SMTPException, OSError) as exc:
        # A failed email must not fail the run; the reports are already written.
        log.error("could not send summary email: %s", exc)
        return False
