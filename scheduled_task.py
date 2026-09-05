"""
Entry point for the periodic check — called by web_server.py's /tick
route (pinged by cron-job.org) or run directly from GitHub Actions if
you went that route instead.

Each run:
  1. Checks Telegram once for new messages (telegram_bot.poll_once)
  2. Checks and delivers any due reminders (reminders.deliver)
  3. Runs the RSS discovery scraper AND the email inbox scanner, but
     ONLY during the scheduled windows (12am / 9am / 4pm, Africa/Lagos
     time) — guarded by a state-table entry so it fires once per
     window even though this runs every few minutes and would
     otherwise overlap it repeatedly.

WhatsApp is NOT handled here — it needs a real always-on webhook server
(Meta pushes messages to it), which is what web_server.py's /webhook
route (once added) is for. This script only covers Telegram, RSS
discovery, email discovery, and reminders.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import db
import telegram_bot
import reminders
import scraper
import email_scanner

LAGOS = ZoneInfo("Africa/Lagos")
SCAN_HOURS = (0, 9, 16)  # 12am, 9am, 4pm — matches run.py's laptop schedule
STATE_KEY_LAST_SCAN = "last_scan_window"


def maybe_scan():
    now = datetime.now(LAGOS)
    if now.hour not in SCAN_HOURS:
        return
    window_key = f"{now.date()}-{now.hour}"
    if db.get_state(STATE_KEY_LAST_SCAN) == window_key:
        return  # already scanned this window on an earlier run today
    print(f"[scheduled_task] running discovery scan for window {window_key}")

    found, failed = scraper.scan_all()
    db.set_state(STATE_KEY_LAST_SCAN, window_key)
    from core import CATEGORY_ICON
    import channels
    owners = db.all_owners()
    if found and owners:
        lines = [f"🔎 Scheduled scan: {len(found)} new opportunities:"]
        for it in found[:8]:
            cat = CATEGORY_ICON.get(it.get("category", "other"), "📌")
            dl = f" | ~{it['deadline']}" if it.get("deadline") else ""
            lines.append(f"{cat} [{it['id']}] {it['title'][:65]}{dl}\n{it['url']}")
        if failed:
            lines.append(f"⚠ Couldn't reach: {', '.join(failed)}")
        channels.broadcast(owners, "\n".join(lines))
    elif failed and len(failed) == len(scraper.FEEDS) and owners:
        channels.broadcast(owners,
            f"⚠ Scheduled scan couldn't reach any of the {len(failed)} "
            f"sources this time — will try again next window.")

    # Email scanning quietly no-ops if EMAIL_ADDRESS/EMAIL_APP_PASSWORD
    # aren't set, so this is safe to call unconditionally.
    email_found = email_scanner.scan_inbox()
    if email_found and owners:
        lines = [f"📧 {len(email_found)} new opportunit"
                 f"{'y' if len(email_found) == 1 else 'ies'} from email:"]
        for it in email_found[:8]:
            cat = CATEGORY_ICON.get(it.get("category", "other"), "📌")
            dl = f" | ~{it['deadline']}" if it.get("deadline") else ""
            lines.append(f"{cat} [{it['id']}] {it['title'][:65]}{dl}")
        channels.broadcast(owners, "\n".join(lines))


def main():
    db.connect().close()  # ensure schema exists (harmless if it already does)

    n = telegram_bot.poll_once()
    print(f"[scheduled_task] processed {n} Telegram message(s)")

    reminders.deliver()
    print("[scheduled_task] reminder check complete")

    maybe_scan()


if __name__ == "__main__":
    main()