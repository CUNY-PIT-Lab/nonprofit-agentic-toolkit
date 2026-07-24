"""Transactional email adapters.

Verification and reset tokens live only in URL fragments so ordinary HTTP
access logs never receive them.
"""

from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class SentEmail:
    to: str
    subject: str
    text: str
    link: str


class MemoryEmailBackend:
    """Key-free local/test adapter. It never writes messages to disk."""

    def __init__(self):
        self.messages: list[SentEmail] = []

    def send(self, *, to: str, subject: str, text: str, link: str) -> None:
        self.messages.append(SentEmail(to=to, subject=subject, text=text, link=link))


class ResendEmailBackend:
    def __init__(self, api_key: str, email_from: str):
        self.api_key = api_key
        self.email_from = email_from

    def send(self, *, to: str, subject: str, text: str, link: str) -> None:
        idempotency_key = hashlib.sha256(
            f"{to.casefold()}\x1f{subject}\x1f{link}".encode("utf-8")
        ).hexdigest()
        payload = json.dumps(
            {
                "from": self.email_from,
                "to": [to],
                "subject": subject,
                "text": f"{text}\n\n{link}",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "nonprofit-ai-toolkit/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status >= 300:
                    raise RuntimeError("Email delivery failed")
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RuntimeError("Email delivery failed") from exc


def verification_link(public_app_url: str, token: str) -> str:
    return f"{public_app_url}/#verify?token={token}"


def reset_link(public_app_url: str, token: str) -> str:
    return f"{public_app_url}/#reset?token={token}"
