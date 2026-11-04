"""Small SMTP sender using the same SMTP_* settings as the daily email."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def send(subject: str, text: str, html: str | None = None,
         attachments: list[tuple[str, bytes, str]] = ()) -> tuple[bool, str]:
    """attachments: (filename, data, mime 'type/subtype'). Never logs server replies."""
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip()
    recipients = [a.strip() for a in os.environ.get("SMTP_TO", "").split(",") if a.strip()]
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    missing = [k for k, v in {"SMTP_HOST": host, "SMTP_USERNAME": username, "SMTP_PASSWORD": password,
                              "SMTP_FROM": sender, "SMTP_TO": recipients}.items() if not v]
    if missing:
        return False, "SKIPPED: missing email configuration: " + ", ".join(missing)
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        if not 1 <= port <= 65535 or security not in {"starttls", "ssl"}:
            raise ValueError
    except ValueError:
        return False, "FAILED: SMTP_PORT or SMTP_SECURITY is invalid."
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, sender, ", ".join(recipients)
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    for filename, data, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    try:
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context()) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        return False, f"FAILED: SMTP {type(exc).__name__}; check SMTP configuration and connectivity."
    return True, f"SENT to {len(recipients)} recipient(s)."
