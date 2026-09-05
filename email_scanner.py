"""
Email inbox scanner — solves opportunities that only arrive by
newsletter email, never RSS (many aggregators only publish that way).

Uses Python's built-in imaplib and email modules — deliberately no
extra package to install, so this can never hit the same "needs a
compiler" problem the libsql package did.

Setup (Gmail):
  1. Turn on 2-Step Verification on your Google account, if it isn't
     already (Google Account -> Security).
  2. Google Account -> Security -> 2-Step Verification -> App
     passwords -> generate one for "Mail". This gives a 16-character
     password DIFFERENT from your real Gmail password — use that one
     here, never your real password.
  3. export EMAIL_ADDRESS="you@gmail.com"
     export EMAIL_APP_PASSWORD="the 16-character app password"
     (EMAIL_IMAP_HOST defaults to imap.gmail.com — only set it if
     you're using a different provider, e.g. Outlook)

First run checks your most recent mail (both read and unread — capped
by max_emails so it doesn't try to backfill years of history). Every
run after that picks up exactly where the last one left off, tracked
in the shared database.
"""

import os
import imaplib
import email
from email.header import decode_header

from bs4 import BeautifulSoup

import db
import core
import extractor
import scraper

IMAP_HOST = os.environ.get("EMAIL_IMAP_HOST", "imap.gmail.com")
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")

STATE_KEY_LAST_UID = "email_last_seen_uid"

# Higher than RSS's default min_score of 2 — see the comment where
# this is used, below.
EMAIL_MIN_SCORE = 3


def _decode_header_value(raw) -> str:
    if not raw:
        return ""
    out = ""
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
        else:
            out += text
    return out


def _extract_body(msg) -> str:
    """Pull plain text out of a possibly multipart/HTML email."""
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        for part in msg.walk():  # fall back to HTML if no plain-text part
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="ignore")
                    return BeautifulSoup(html, "html.parser").get_text(
                        separator="\n", strip=True)
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""
    if msg.get_content_type() == "text/html":
        return BeautifulSoup(payload, "html.parser").get_text(separator="\n", strip=True)
    return payload


def _file_from_email(subject: str, body: str, category: str, guessed_deadline):
    """Same idea as core.handle_link / core.handle_pasted_text, but for
    an automated background scan with no single chat to reply to —
    files the opportunity and returns its id, or None if skipped."""
    url_match = core.URL_RE.search(body)

    if url_match:
        url = url_match.group(0).rstrip(").,")
        if db.url_exists(url):
            return None
        try:
            result = extractor.extract(url)
        except Exception:
            result = None
        if result:
            title = result.get("title") or subject
            deadline = core.normalize_deadline(result.get("deadline")) or guessed_deadline
            oid = db.add_opportunity(title=title, url=url, category=category,
                                     deadline=deadline, source="email",
                                     extraction=result)
            db.add_checklist_items(oid, result["fields"])
        else:
            oid = db.add_opportunity(title=subject, url=url, category=category,
                                     deadline=guessed_deadline, source="email")
    else:
        try:
            result = extractor.extract_llm_text(body)
            if result:
                result = extractor.tag_effort(result)
        except Exception:
            result = None
        if result:
            title = result.get("title") or subject
            deadline = core.normalize_deadline(result.get("deadline")) or guessed_deadline
            oid = db.add_opportunity(title=title, url=None, category=category,
                                     deadline=deadline, source="email",
                                     extraction=result)
            db.add_checklist_items(oid, result["fields"])
        else:
            oid = db.add_opportunity(title=subject, url=None, category=category,
                                     deadline=guessed_deadline, source="email")

    db.set_status(oid, "seen")
    return oid


def scan_inbox(max_emails: int = 100) -> list:
    """Check for new emails since the last scan and file any relevant
    opportunities found. Returns the list of filed items — does NOT
    notify anyone itself (matching scraper.scan_all's pattern), so
    whoever calls this is responsible for telling the user about the
    results, however is appropriate for that context."""
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD):
        return []

    filed = []
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        imap.select("INBOX")

        last_uid = db.get_state(STATE_KEY_LAST_UID)
        if last_uid:
            status, data = imap.uid("search", None, f"UID {int(last_uid) + 1}:*")
        else:
            # First run: check ALL mail (read and unread), not just
            # unread — capped by max_emails below to the most recent
            # ones, so this doesn't try to backfill years of history.
            status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []

        uids = [int(u) for u in data[0].split()]
        # IMAP's "N:*" range can hand back an old UID <= N when nothing
        # new exists (a documented protocol quirk) — filter defensively.
        if last_uid:
            uids = [u for u in uids if u > int(last_uid)]
        uids = uids[-max_emails:]  # cap how many a single run processes

        max_uid_seen = int(last_uid) if last_uid else 0

        for uid in uids:
            status, msg_data = imap.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header_value(msg.get("Subject"))
            body = _extract_body(msg)
            max_uid_seen = max(max_uid_seen, uid)

            item = {"title": subject, "summary": body[:500]}
            # Higher bar than RSS's default (2): an inbox mixes real
            # postings with newsletters, notifications, and articles
            # that happen to mention job-market keywords without being
            # an actual opportunity — confirmed this was letting a
            # scam-awareness article through during real testing.
            if scraper.score(item) < EMAIL_MIN_SCORE:
                continue

            category = core.classify(f"{subject} {body}")
            deadline = scraper.guess_deadline(item)
            oid = _file_from_email(subject, body, category, deadline)
            if oid:
                filed.append({"id": oid, "title": subject, "category": category,
                             "deadline": deadline})

        if max_uid_seen:
            db.set_state(STATE_KEY_LAST_UID, str(max_uid_seen))
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return filed


if __name__ == "__main__":
    found = scan_inbox()
    print(f"{len(found)} new opportunities filed from email.")