"""
Core logic — completely channel-agnostic.

Both bots call handle_message(channel, chat_id, text). Everything else
(capture, extraction, commands, formatting) lives here once.

Commands (work identically on WhatsApp and Telegram; the leading '/' is
optional so WhatsApp typing is easier — 'list jobs' == '/list jobs'):

  list [category]           pipeline (optionally: scholarship/internship/
                            fellowship/job/grant/hackathon)
  show <id>                 detail + checklist
  status <id> <status>      seen|interested|drafting|submitted|rejected|won
  category <id> <category>  reclassify
  deadline <id> YYYY-MM-DD
  done <item_id>            tick checklist item
  scan                      run discovery now
  help
"""

import re
from datetime import datetime

import db
import channels
import extractor
import scraper

URL_RE = re.compile(r"https?://\S+")

STATUS_ICON = {"seen": "👁", "interested": "⭐", "drafting": "✍️",
               "submitted": "✅", "rejected": "✖️", "won": "🏆"}

CATEGORY_ICON = {"scholarship": "🎓", "internship": "🧑‍💻", "fellowship": "🏛",
                 "job": "💼", "grant": "💰", "hackathon": "⚡", "other": "📌"}

# Keywords for auto-categorizing pasted links / extracted titles
CATEGORY_KEYWORDS = {
    "scholarship": ["scholarship", "bursary", "tuition"],
    "fellowship":  ["fellowship", "fellow "],
    "internship":  ["internship", "intern ", "siwes", "placement"],
    "job":         ["job", "vacancy", "hiring", "role", "position", "career",
                    "engineer wanted", "developer wanted", "full-time", "full time"],
    "grant":       ["grant", "funding", "seed fund", "prize money"],
    "hackathon":   ["hackathon", "challenge", "competition", "datathon", "buildathon"],
}


def classify(text: str) -> str:
    t = (text or "").lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in t for k in kws):
            return cat
    return "other"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_line(o: dict) -> str:
    icon = STATUS_ICON.get(o["status"], "•")
    cat = CATEGORY_ICON.get(o["category"], "📌")
    dl = o["deadline"] or "no deadline!"
    days = db.days_until(o["deadline"]) if o["deadline"] else None
    days_txt = f" ({days}d)" if days is not None and days >= 0 else ""
    done, total = db.checklist_progress(o["id"])
    prog = f" [{done}/{total}]" if total else ""
    return f"{icon}{cat} [{o['id']}] {o['title'][:55]} — {dl}{days_txt}{prog}"


def fmt_checklist(oid: int) -> str:
    items = db.get_checklist(oid)
    if not items:
        return "No checklist extracted yet."
    lines = []
    heavy = [i for i in items if i["effort"] == "heavy"]
    quick = [i for i in items if i["effort"] == "quick"]
    if heavy:
        lines.append("HEAVY ITEMS (start now):")
        for i in heavy:
            box = "☑" if i["done"] else "☐"
            notes = f" — {i['notes']}" if i["notes"] else ""
            lines.append(f" {box} ({i['id']}) {i['item']}{notes}")
    if quick:
        lines.append("QUICK FILLS:")
        for i in quick:
            box = "☑" if i["done"] else "☐"
            lines.append(f" {box} ({i['id']}) {i['item']}")
    lines.append("\nTick items with: done <item_id>")
    return "\n".join(lines)


def normalize_deadline(raw) -> str | None:
    if not raw:
        return None
    raw = str(raw).replace(",", "").strip()
    # Strip ordinal suffixes: "31st August 2026" -> "31 August 2026"
    raw = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    for fmt in ("%Y-%m-%d", "%B %d %Y", "%d %B %Y", "%b %d %Y", "%d %b %Y",
                "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Capture flow: link in -> checklist out
# ---------------------------------------------------------------------------

def handle_link(channel: str, chat_id: str, url: str):
    if db.url_exists(url):
        channels.send(channel, chat_id,
                      "Already tracking that one. 'list' to find it.")
        return

    channels.send(channel, chat_id, "Got it. Extracting requirements... ⏳")
    try:
        result = extractor.extract(url)
    except Exception as e:
        category = classify(url)
        oid = db.add_opportunity(title=url[:80], url=url, category=category,
                                 source="manual",
                                 notes=f"extraction failed: {e}")
        channels.send(channel, chat_id,
            f"Filed as [{oid}] but extraction failed ({e}).\n"
            f"If it's login-walled, screenshot the form and run the extractor "
            f"on the image locally.\n"
            f"Set the deadline: deadline {oid} YYYY-MM-DD")
        return

    title = result.get("title") or url[:80]
    category = classify(f"{title} {url}")
    deadline = normalize_deadline(result.get("deadline"))
    oid = db.add_opportunity(title=title, url=url, category=category,
                             deadline=deadline, source="manual",
                             extraction=result)
    db.add_checklist_items(oid, result["fields"])
    db.set_status(oid, "interested")

    msg = [f"⭐{CATEGORY_ICON.get(category)} Filed [{oid}] {title} ({category})"]
    if deadline:
        msg.append(f"Deadline: {deadline} ({db.days_until(deadline)} days)")
    else:
        msg.append(f"⚠ No deadline found — set it: deadline {oid} YYYY-MM-DD")
    if result.get("warnings"):
        msg.append("\n".join("⚠ " + w for w in result["warnings"]))
    msg.append("\n" + fmt_checklist(oid))
    channels.send(channel, chat_id, "\n".join(msg))


# Minimum length before a linkless paste is treated as an announcement to
# extract from, rather than a short chat message the command parser should
# handle (and reply "didn't understand").
PASTE_MIN_CHARS = 120


def handle_pasted_text(channel: str, chat_id: str, text: str):
    """Handles announcements with NO application link — e.g. a scholarship
    post forwarded from a Telegram/WhatsApp channel where you apply by
    emailing documents rather than filling a web form. Runs the same LLM
    extraction tier the link flow uses, just on the pasted text directly."""
    channels.send(channel, chat_id, "Got it. Reading through that... ⏳")
    try:
        result = extractor.extract_llm_text(text)
        if result:
            result = extractor.tag_effort(result)
    except Exception as e:
        oid = db.add_opportunity(title=text.split("\n")[0][:80], url=None,
                                 category=classify(text), source="pasted-text",
                                 notes=f"extraction failed: {e}\n\n{text[:500]}")
        channels.send(channel, chat_id,
            f"Filed as [{oid}] but couldn't extract requirements ({e}).\n"
            f"Set the deadline: deadline {oid} YYYY-MM-DD")
        return

    if not result:
        oid = db.add_opportunity(title=text.split("\n")[0][:80], url=None,
                                 category=classify(text), source="pasted-text",
                                 notes=text[:500])
        channels.send(channel, chat_id,
            f"Filed as [{oid}] but couldn't identify structured requirements "
            f"in that text. Notes saved — add details manually if needed.")
        return

    title = result.get("title") or text.split("\n")[0][:80]
    category = classify(f"{title} {text}")
    deadline = normalize_deadline(result.get("deadline"))
    # Surface an email-to-apply address if the text mentions one, since
    # there's no form link to fall back on.
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    notes = f"Apply via: {email_match.group(0)}" if email_match else None

    oid = db.add_opportunity(title=title, url=None, category=category,
                             deadline=deadline, source="pasted-text",
                             notes=notes, extraction=result)
    db.add_checklist_items(oid, result["fields"])
    db.set_status(oid, "interested")

    msg = [f"⭐{CATEGORY_ICON.get(category)} Filed [{oid}] {title} ({category})"]
    if notes:
        msg.append(notes)
    if deadline:
        msg.append(f"Deadline: {deadline} ({db.days_until(deadline)} days)")
    else:
        msg.append(f"⚠ No deadline found — set it: deadline {oid} YYYY-MM-DD")
    if result.get("warnings"):
        msg.append("\n".join("⚠ " + w for w in result["warnings"]))
    msg.append("\n" + fmt_checklist(oid))
    channels.send(channel, chat_id, "\n".join(msg))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP = (
    "Paste ANY application link (scholarship, internship, fellowship, job, "
    "grant, hackathon) and I'll extract its requirements.\n\n"
    "list [category] — pipeline, e.g. 'list job'\n"
    "show <id> — detail + checklist\n"
    "status <id> <seen|interested|drafting|submitted|rejected|won>\n"
    "category <id> <scholarship|internship|fellowship|job|grant|hackathon|other>\n"
    "deadline <id> YYYY-MM-DD\n"
    "done <item_id> — tick checklist item\n"
    "scan — run discovery now\n"
    "scanemail — check your inbox now (if email scanning is set up)\n\n"
    "(the leading / is optional)"
)


def handle_command(channel: str, chat_id: str, text: str):
    send = lambda t: channels.send(channel, chat_id, t)
    parts = text.lstrip("/").split()
    if not parts:
        send(HELP)
        return
    cmd = parts[0].lower().split("@")[0]

    try:
        if cmd in ("start", "help", "hi", "hello", "menu"):
            send(HELP)

        elif cmd == "list":
            category = parts[1].lower().rstrip("s") if len(parts) > 1 else None
            if category and category not in db.CATEGORIES:
                send(f"Unknown category. Use: {', '.join(db.CATEGORIES)}")
                return
            active = db.list_active(category)
            if not active:
                send("Pipeline empty for that filter. Paste a link or 'scan'.")
            else:
                head = f"PIPELINE ({category or 'all'}):"
                send(head + "\n" + "\n".join(fmt_line(o) for o in active))

        elif cmd == "show" and len(parts) >= 2:
            o = db.get(int(parts[1]))
            if not o:
                send("No opportunity with that id.")
                return
            lines = [fmt_line(o)]
            if o["url"]:
                lines.append(o["url"])
            if o["notes"]:
                lines.append(f"Notes: {o['notes']}")
            lines.append("\n" + fmt_checklist(o["id"]))
            send("\n".join(lines))

        elif cmd == "status" and len(parts) >= 3:
            oid, status = int(parts[1]), parts[2].lower()
            if db.set_status(oid, status):
                send(f"[{oid}] → {status} {STATUS_ICON.get(status, '')}")
            else:
                send(f"Invalid status. Use: {', '.join(db.STATUSES)}")

        elif cmd == "category" and len(parts) >= 3:
            oid, cat = int(parts[1]), parts[2].lower().rstrip("s")
            if db.set_category(oid, cat):
                send(f"[{oid}] → {cat} {CATEGORY_ICON.get(cat, '')}")
            else:
                send(f"Invalid category. Use: {', '.join(db.CATEGORIES)}")

        elif cmd == "deadline" and len(parts) >= 3:
            oid, dl = int(parts[1]), parts[2]
            norm = normalize_deadline(dl)
            if norm:
                db.set_deadline(oid, norm)
                send(f"[{oid}] deadline set: {norm} "
                     f"({db.days_until(norm)} days). Reminders armed.")
            else:
                send("Couldn't parse that date. Use YYYY-MM-DD.")

        elif cmd == "done" and len(parts) >= 2:
            db.mark_item_done(int(parts[1]))
            send("Ticked ☑")

        elif cmd == "scan":
            send("Scanning sources... ⏳")
            found, failed = scraper.scan_all()
            if not found and failed and len(failed) == len(scraper.FEEDS):
                send(f"⚠ Couldn't reach any of the {len(failed)} sources this "
                     f"time — looks like a connection issue on this end, not "
                     f"that there's genuinely nothing new. Try 'scan' again "
                     f"in a minute.")
            elif not found:
                msg = "Nothing new matching your profile."
                if failed:
                    msg += f"\n⚠ ({len(failed)} source(s) unreachable this time: {', '.join(failed)})"
                send(msg)
            else:
                lines = [f"🔎 {len(found)} new opportunities:"]
                for it in found[:10]:
                    cat = CATEGORY_ICON.get(it.get("category", "other"), "📌")
                    dl = f" | ~{it['deadline']}" if it.get("deadline") else ""
                    lines.append(f"{cat} [{it['id']}] {it['title'][:65]}{dl}\n{it['url']}")
                lines.append("\n'show <id>' for detail, or paste its link "
                             "to extract requirements.")
                if failed:
                    lines.append(f"⚠ Couldn't reach: {', '.join(failed)}")
                send("\n".join(lines))

        elif cmd == "scanemail":
            import email_scanner
            if not (email_scanner.EMAIL_ADDRESS and email_scanner.EMAIL_APP_PASSWORD):
                send("Email scanning isn't set up yet — needs EMAIL_ADDRESS "
                     "and EMAIL_APP_PASSWORD set. See README for the "
                     "Gmail app-password steps.")
                return
            send("Scanning your inbox... ⏳")
            found = email_scanner.scan_inbox()
            if not found:
                send("Nothing new/relevant in your inbox.")
            else:
                lines = [f"📧 {len(found)} new opportunities from email:"]
                for it in found[:10]:
                    cat = CATEGORY_ICON.get(it.get("category", "other"), "📌")
                    dl = f" | ~{it['deadline']}" if it.get("deadline") else ""
                    lines.append(f"{cat} [{it['id']}] {it['title'][:65]}{dl}")
                send("\n".join(lines))

        else:
            send("Didn't understand that. 'help' for commands.")
    except (ValueError, IndexError):
        send("Bad arguments — check 'help' for the format.")


# ---------------------------------------------------------------------------
# Single entry point for both channels
# ---------------------------------------------------------------------------

COMMAND_WORDS = {"start", "help", "hi", "hello", "menu", "list", "show",
                 "status", "category", "deadline", "done", "scan", "scanemail"}


def handle_message(channel: str, chat_id, text: str):
    chat_id = str(chat_id)
    text = (text or "").strip()
    if not text:
        return

    # First message on each channel locks ownership; strangers are ignored
    db.lock_owner(channel, chat_id)
    if not db.is_owner(channel, chat_id):
        return

    m = URL_RE.search(text)
    first_word = text.lstrip("/").split()[0].lower() if text.split() else ""

    if m and first_word not in COMMAND_WORDS:
        handle_link(channel, chat_id, m.group(0).rstrip(").,"))
    elif (not m and first_word not in COMMAND_WORDS
          and len(text) >= PASTE_MIN_CHARS):
        # No link, not a command, and long enough to be a pasted
        # announcement (e.g. forwarded from a channel) rather than
        # ordinary chat — extract requirements straight from the text.
        handle_pasted_text(channel, chat_id, text)
    else:
        handle_command(channel, chat_id, text)