"""
Tiny web server for Render's free tier — the free alternative to both
GitHub Actions and Render's own (paid) Cron Job feature.

Exposes one endpoint, /tick, that a free external scheduler
(cron-job.org) visits on a timer. Each visit runs exactly the same
check scheduled_task.py always ran: poll Telegram once, deliver any
due reminders, and run the discovery scan during your scheduled
windows (12am/9am/4pm Africa/Lagos time).

Why this works: Render's free web services spin down after 15 minutes
with no incoming request, then wake within about a minute the next
time a request arrives. Visiting /tick every few minutes both triggers
the check AND keeps the service from spinning down in between.

This is also where WhatsApp's webhook will live later (as another
route in this same file) — one small free service ends up covering
Telegram's scheduled checks and, eventually, WhatsApp's incoming
messages together.

Setup:
  1. Deploy this file on Render as a free Web Service
       Build command: pip install requests beautifulsoup4 pypdf flask libsql
       Start command: python web_server.py
       Env vars: TELEGRAM_BOT_TOKEN, GROQ_API_KEY, TURSO_DATABASE_URL,
                 TURSO_AUTH_TOKEN, TICK_SECRET (any password you make up)
  2. Sign up at cron-job.org (free) and add a job that visits:
         https://your-app.onrender.com/tick?secret=<your TICK_SECRET>
     every 5-15 minutes.
"""

import os

from flask import Flask, request

import scheduled_task

TICK_SECRET = os.environ.get("TICK_SECRET", "")

app = Flask(__name__)


@app.get("/")
def health():
    return "opportunity-assistant is running", 200


@app.get("/tick")
@app.post("/tick")
def tick():
    # Fail CLOSED: if you forget to set TICK_SECRET, the endpoint is
    # locked rather than left open to anyone who finds the URL.
    if not TICK_SECRET or request.args.get("secret") != TICK_SECRET:
        return "forbidden", 403
    scheduled_task.main()
    return "ok", 200


def run_server(port: int = None):
    port = port or int(os.environ.get("PORT", 5000))
    print(f"[web_server] listening on :{port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_server()
