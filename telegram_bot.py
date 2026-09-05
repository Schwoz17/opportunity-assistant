"""
Telegram adapter — two modes, same underlying logic in core.py.

  poll_forever() — continuous long-polling. Use this when the process
    stays running (your laptop via run.py, or a real always-on server).
    Keeps the offset in memory since the process never restarts.

  poll_once()    — checks for new messages ONE time and returns. Use this
    from GitHub Actions, where a fresh process starts every ~15 minutes
    and remembers nothing on its own — so the offset (which messages
    have already been handled) is stored in the shared database instead
    of a local variable.
"""

import os
import time

import requests

import core
import db

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"

STATE_KEY_OFFSET = "telegram_update_offset"


def poll_once() -> int:
    """Fetch and handle whatever Telegram messages have arrived since the
    last run. Returns how many were processed. Safe to call repeatedly —
    Telegram won't redeliver a message once its update_id has been
    acknowledged via the offset."""
    if not TOKEN:
        print("[telegram] TELEGRAM_BOT_TOKEN not set — skipping.")
        return 0

    stored = db.get_state(STATE_KEY_OFFSET)
    offset = int(stored) if stored else 0

    resp = requests.get(f"{API}/getUpdates",
                        params={"timeout": 0, "offset": offset},
                        timeout=30)
    updates = resp.json().get("result", [])
    count = 0
    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id and text:
            core.handle_message("telegram", chat_id, text)
            count += 1

    if updates:
        db.set_state(STATE_KEY_OFFSET, str(offset))
    return count


def poll_forever():
    """Continuous mode for a long-running process (laptop / real server)."""
    if not TOKEN:
        print("[telegram] TELEGRAM_BOT_TOKEN not set — Telegram disabled.")
        return
    offset = 0
    print("[telegram] polling...")
    while True:
        try:
            resp = requests.get(f"{API}/getUpdates",
                                params={"timeout": 30, "offset": offset},
                                timeout=45)
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if chat_id and text:
                    core.handle_message("telegram", chat_id, text)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[telegram] poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    poll_forever()
