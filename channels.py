"""
Channel layer — one send() for both platforms.

Telegram: Bot API sendMessage.
WhatsApp: Meta Cloud API (graph.facebook.com) text messages.

Env vars:
  TELEGRAM_BOT_TOKEN
  WHATSAPP_TOKEN         (permanent access token from Meta app)
  WHATSAPP_PHONE_ID      (your WhatsApp Business phone number ID)
"""

import os
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WHATSAPP_API = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"


def send_telegram(chat_id: str, text: str):
    for chunk in _chunks(text, 4000):
        requests.post(f"{TELEGRAM_API}/sendMessage",
                      json={"chat_id": chat_id, "text": chunk},
                      timeout=15)


def send_whatsapp(chat_id: str, text: str):
    for chunk in _chunks(text, 3900):
        requests.post(
            WHATSAPP_API,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": chat_id,
                "type": "text",
                "text": {"body": chunk},
            },
            timeout=15,
        )


def send(channel: str, chat_id: str, text: str):
    if channel == "telegram":
        send_telegram(chat_id, text)
    elif channel == "whatsapp":
        send_whatsapp(chat_id, text)
    else:
        raise ValueError(f"unknown channel: {channel}")


def broadcast(owners: dict, text: str):
    """Send to your account on every connected channel."""
    for channel, chat_id in owners.items():
        try:
            send(channel, chat_id, text)
        except Exception as e:
            print(f"[channels] send to {channel} failed: {e}")


def _chunks(text: str, n: int):
    return [text[i:i + n] for i in range(0, len(text), n)] or [text]
