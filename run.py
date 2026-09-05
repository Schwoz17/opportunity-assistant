"""
Entry point — runs everything concurrently:
  - Telegram bot        (long-polling thread; enabled if TELEGRAM_BOT_TOKEN set)
  - WhatsApp webhook    (Flask thread on :5000; enabled if WHATSAPP_TOKEN set)
  - Scheduler           (nudges every 30 min; discovery scans at 12am, 9am, 4pm)

You can run with either channel alone or both — each enables itself
based on which env vars are present.

Usage:
  export TELEGRAM_BOT_TOKEN=...     # optional
  export WHATSAPP_TOKEN=...         # optional
  export WHATSAPP_PHONE_ID=...
  export WHATSAPP_VERIFY_TOKEN=...
  export GROQ_API_KEY=...
  python run.py
"""

import threading
import time
from datetime import datetime

import db
import channels
import reminders
import scraper
import telegram_bot
import whatsapp_bot
from core import CATEGORY_ICON

# Discovery scan hours (24h, local server time): 12am, 9am, 4pm
SCRAPE_HOURS = (0, 9, 16)
SCRAPE_LABELS = {0: "Midnight", 9: "Morning", 16: "Afternoon"}

NUDGE_INTERVAL_SEC = 30 * 60


def scheduler():
    last_scrape_key = None
    while True:
        # --- nudges (broadcast to all connected channels) ---
        try:
            reminders.deliver()
        except Exception as e:
            print(f"[scheduler] nudge error: {e}")

        # --- thrice-daily discovery: 12am / 9am / 4pm ---
        now = datetime.now()
        key = (now.date(), now.hour)
        if now.hour in SCRAPE_HOURS and key != last_scrape_key:
            last_scrape_key = key
            try:
                found, failed = scraper.scan_all()
                owners = db.all_owners()
                if found and owners:
                    label = SCRAPE_LABELS.get(now.hour, "Scheduled")
                    lines = [f"🔎 {label} scan: {len(found)} new opportunities:"]
                    for it in found[:8]:
                        cat = CATEGORY_ICON.get(it.get("category", "other"), "📌")
                        dl = f" | ~{it['deadline']}" if it.get("deadline") else ""
                        lines.append(f"{cat} [{it['id']}] {it['title'][:65]}{dl}\n{it['url']}")
                    if failed:
                        lines.append(f"⚠ Couldn't reach: {', '.join(failed)}")
                    channels.broadcast(owners, "\n".join(lines))
                elif failed and len(failed) == len(scraper.FEEDS) and owners:
                    channels.broadcast(owners,
                        f"⚠ Scheduled scan couldn't reach any of the "
                        f"{len(failed)} sources this time — will try again "
                        f"next window.")
            except Exception as e:
                print(f"[scheduler] scrape error: {e}")

        time.sleep(NUDGE_INTERVAL_SEC)


if __name__ == "__main__":
    db.connect().close()  # ensure schema exists

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=whatsapp_bot.run_server, daemon=True).start()

    # Telegram polling holds the main thread (or idles if not configured)
    telegram_bot.poll_forever()
    while True:  # keep alive if Telegram is disabled but WhatsApp runs
        time.sleep(3600)