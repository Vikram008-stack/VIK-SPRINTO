"""Step 7: Gmail send. The ONLY recipient is DEMO_RECIPIENT_EMAIL.

- build_message() is the single place a message is constructed. It calls assert_safe_recipient() and the
  real prospect's address appears only in the demo header line inside the body ("Intended recipient: ...").
- send() re-asserts the To header immediately before the API call.
- Dry run (default) writes an .eml file and records status DRY_RUN; nothing leaves the machine.
- OAuth installed-app flow, gmail.send scope only, token cached under data/secrets/.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from src.common.logging import get_logger
from src.common.safety import UnsafeRecipientError, assert_safe_recipient, demo_recipient
from src.store.models import Contact, EmailDraft, ReasonCode, SendResult

log = get_logger(__name__)
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def intended_line(contact: Contact) -> str:
    return f"Intended recipient: {contact.name} <{contact.email or 'no email'}>, {contact.position or 'unknown title'}"


DRY_RUN_PLACEHOLDER = "demo-inbox-not-configured@example.invalid"   # reserved TLD: undeliverable by design


def build_message(draft: EmailDraft, contact: Contact, domain: str, sender_label: str = "",
                  dry_run: bool = False) -> EmailMessage:
    try:
        to = assert_safe_recipient(demo_recipient())
    except UnsafeRecipientError:
        if not dry_run:
            raise
        to = DRY_RUN_PLACEHOLDER      # dry run never sends; the .eml shows where it WOULD go
    if contact.email and to.lower() == contact.email.lower():
        raise UnsafeRecipientError("demo recipient equals the prospect's address; refusing")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = f"[DEMO] {draft.subject}"
    if sender_label:
        msg["X-Sprinto-Demo"] = sender_label
    header = (f"{intended_line(contact)}\nCompany: {domain}\n"
              f"(Demo build: this email was sent to the demo inbox only. Human review required before any real send.)\n"
              + "-" * 60 + "\n\n")
    msg.set_content(header + draft.email_body)
    evidence = "\n".join(f"- {c.claim} :: {c.source_url}" for c in draft.personalisation_evidence)
    msg.add_alternative(
        "<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:14px;line-height:1.55;color:#1f2933'>"
        f"<div style='background:#f6f3ee;border-left:3px solid #c08457;padding:10px 12px;margin-bottom:16px;"
        f"font-size:12px;color:#52606d'>{_esc(intended_line(contact))}<br>Company: {_esc(domain)}<br>"
        "Demo build: delivered to the demo inbox only.</div>"
        f"<div style='white-space:pre-wrap'>{_esc(draft.email_body)}</div>"
        f"<hr style='border:none;border-top:1px solid #e4e7eb;margin:18px 0'>"
        f"<div style='font-size:12px;color:#7b8794'><b>Evidence behind this email</b><br>"
        f"{_esc(evidence).replace(chr(10), '<br>')}</div></div>", subtype="html")
    return msg


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class GmailSender:
    def __init__(self, client_secret_file: Optional[str], token_file: Optional[str], dry_run: bool = True,
                 outbox_dir: Optional[Path] = None):
        self.client_secret_file = client_secret_file
        self.token_file = token_file
        self.dry_run = dry_run
        self.outbox = outbox_dir
        self._service = None

    def _svc(self):
        if self._service is not None:
            return self._service
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        tok = Path(self.token_file) if self.token_file else None
        if tok and tok.exists():
            creds = Credentials.from_authorized_user_file(str(tok), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.client_secret_file or not Path(self.client_secret_file).exists():
                    raise FileNotFoundError("Gmail client secret file not found (GMAIL_CLIENT_SECRET_FILE)")
                flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_file, SCOPES)
                creds = flow.run_local_server(port=0)
            if tok:
                tok.parent.mkdir(parents=True, exist_ok=True)
                tok.write_text(creds.to_json(), encoding="utf-8")
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def send(self, draft: EmailDraft, contact: Contact, domain: str) -> SendResult:
        intended = intended_line(contact)
        try:
            msg = build_message(draft, contact, domain, dry_run=self.dry_run)
            if not self.dry_run:
                assert_safe_recipient(msg["To"])    # re-assert right before any real send
        except UnsafeRecipientError as e:
            return SendResult(domain=domain, to="(blocked)", intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.UNSAFE_RECIPIENT, detail=str(e))
        except Exception as e:
            return SendResult(domain=domain, to="(none)", intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.GMAIL_MIME_ERROR, detail=f"{type(e).__name__}: {e}"[:300])
        to = msg["To"]
        if self.dry_run:
            if self.outbox:
                self.outbox.mkdir(parents=True, exist_ok=True)
                (self.outbox / f"{domain}.eml").write_bytes(bytes(msg))
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="DRY_RUN")
        try:
            raw = base64.urlsafe_b64encode(bytes(msg)).decode()
            resp = self._svc().users().messages().send(userId="me", body={"raw": raw}).execute()
            log.info("gmail sent", extra={"domain": domain, "message_id": resp.get("id")})
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="SENT",
                              message_id=resp.get("id"))
        except FileNotFoundError as e:
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="FAILED",
                              reason=ReasonCode.GMAIL_AUTH_ERROR, detail=str(e))
        except Exception as e:
            text = str(e)
            name = type(e).__name__
            if "RefreshError" in name or "invalid_grant" in text or "401" in text:
                reason = ReasonCode.GMAIL_AUTH_ERROR
            elif "429" in text or "rateLimitExceeded" in text or "quota" in text.lower():
                reason = ReasonCode.GMAIL_QUOTA_ERROR
            else:
                reason = ReasonCode.GMAIL_SEND_ERROR
            return SendResult(domain=domain, to=to, intended_recipient=intended, status="FAILED", reason=reason,
                              detail=f"{name}: {text}"[:300])
