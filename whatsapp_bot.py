"""
WhatsApp adapter — Meta WhatsApp Cloud API webhook (Flask).

Setup (one-time, ~15 min):
  1. developers.facebook.com → Create App → type "Business"
  2. Add the "WhatsApp" product. Meta gives you a FREE test number
     and a temporary token (generate a permanent one under
     Business Settings → System Users for production).
  3. Note your Phone Number ID (WhatsApp → API Setup page).
  4. Expose this server publicly:
       local dev:  ngrok http 5000   → copy the https URL
       hosted:     Railway / Render / any VPS
  5. WhatsApp → Configuration → Webhook:
       Callback URL:  https://<your-url>/webhook
       Verify token:  whatever you set in WHATSAPP_VERIFY_TOKEN
       Subscribe to the "messages" field.
  6. On the test number, add your personal WhatsApp as a recipient,
     then message the bot from your phone.

Env vars:
  WHATSAPP_TOKEN         access token
  WHATSAPP_PHONE_ID      phone number id
  WHATSAPP_VERIFY_TOKEN  any string you choose (must match step 5)

Note: with a test number you must send the bot a message first;
it can then reply freely within a 24h session window. Your own
nudges re-open the window each time you interact, which is fine
for a personal assistant you talk to daily.
"""

import os
import threading

from flask import Flask, request

import core

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "change-me")

app = Flask(__name__)


@app.get("/webhook")
def verify():
    """Meta's one-time webhook verification handshake."""
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "verification failed", 403


@app.post("/webhook")
def receive():
    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue
                    sender = msg.get("from")
                    text = msg.get("text", {}).get("body", "")
                    if sender and text:
                        threading.Thread(
                            target=core.handle_message,
                            args=("whatsapp", sender, text),
                            daemon=True,
                        ).start()
    except Exception as e:
        print(f"[whatsapp] webhook error: {e}")
    return "ok", 200


@app.get("/")
def health():
    return "opportunity-assistant whatsapp webhook running", 200


def run_server(port: int = None):
    if not os.environ.get("WHATSAPP_TOKEN"):
        print("[whatsapp] WHATSAPP_TOKEN not set — WhatsApp disabled.")
        return
    # Render (and most hosts) assign the port via $PORT — fall back to
    # 5000 for local testing where nothing sets that variable.
    port = port or int(os.environ.get("PORT", 5000))
    print(f"[whatsapp] webhook listening on :{port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_server()
