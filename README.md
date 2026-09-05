# Opportunity Assistant

One pipeline for **scholarships, internships, fellowships, jobs, grants, and hackathons** — reachable from **both WhatsApp and Telegram**.

Solves both failure modes:
- **"I didn't see it"** → discovery scraper checks opportunity + job sources **three times a day (12am, 9am, 4pm)**, filters by your profile, auto-categorizes, pings you on every connected channel.
- **"I starred it and forgot"** → paste any link into either app; requirements get extracted into a checklist; escalating reminders (7d → 3d → 1d → overdue) plus a staleness nudge for anything untouched 5+ days.

## Architecture

```
run.py                entry point (all three run concurrently)
├── core.py           ALL logic, channel-agnostic
├── channels.py       unified send() + broadcast to both channels
├── telegram_bot.py   long-polling adapter (no public URL needed)
├── whatsapp_bot.py   Flask webhook adapter (Meta Cloud API)
├── extractor.py      universal requirements extractor
│                     (Google Forms → HTML → LLM → vision → PDF cascade)
├── scraper.py        RSS discovery, profile filter, auto-categorization
├── reminders.py      escalating nudges, broadcast to all channels
└── db.py             SQLite (opportunities, checklists, nudges, owners)
```

Run with either channel alone or both — each enables itself based on which env vars exist.

## Setup

```bash
pip install requests beautifulsoup4 pypdf flask
export GROQ_API_KEY="gsk_..."          # console.groq.com (free)
```

### Telegram (5 minutes, no server needed)
1. Message @BotFather → `/newbot` → copy token
2. `export TELEGRAM_BOT_TOKEN="123:ABC..."`
3. `python run.py`, then message your bot once (locks it to you)

### WhatsApp (~15 minutes, needs a public URL)
1. developers.facebook.com → Create App (type **Business**) → add the **WhatsApp** product. You get a free test number + token.
2. Copy the **Phone Number ID** from WhatsApp → API Setup.
3. Expose the webhook: locally `ngrok http 5000`, or deploy to Railway/Render.
4. WhatsApp → Configuration → Webhook: callback `https://<url>/webhook`, verify token = whatever you set below, subscribe to **messages**.
5. Add your personal number as a test recipient, then message the bot.

```bash
export WHATSAPP_TOKEN="EAAG..."
export WHATSAPP_PHONE_ID="1234567890"
export WHATSAPP_VERIFY_TOKEN="any-string-you-like"
```

**WhatsApp 24h window caveat:** with a test number, the bot can only message you within 24h of your last message to it. Daily use keeps the window open; for guaranteed anytime-nudges, Telegram has no such limit — a good reason to run both.

## Usage (identical on both apps; leading `/` optional)

| You send | Bot does |
|---|---|
| any link | Files it, auto-categorizes (🎓🧑‍💻🏛💼💰⚡), extracts full checklist, flags rec letters, asks for deadline if unknown |
| `list` / `list job` / `list internship` | Pipeline, optionally filtered, soonest deadline first |
| `show 3` | Detail + checklist |
| `done 12` | Tick checklist item 12 |
| `status 3 submitted` | Move through pipeline |
| `category 3 fellowship` | Reclassify |
| `deadline 3 2026-08-15` | Set/fix deadline, arms reminders |
| `scan` | Run discovery now |

Statuses: `seen → interested → drafting → submitted` (terminal: `rejected`, `won`)

## Discovery sources (currently 7 feeds)

- Opportunity Desk
- After School Africa
- Opportunities For Africans
- Scholarship Positions
- **Scholars World** (scholarsworld.ng) — Nigeria-focused scholarships, internships, fellowships, grants
- WeWorkRemotely (remote programming jobs)
- RemoteOK (remote dev jobs)

Scans run at **12:00am, 9:00am, and 4:00pm** daily (server local time), plus on demand with `scan`.

### Adding "Google" as a general source

Google itself has no public RSS feed, but **Google Alerts** does — this is the free, no-API-key way to fold general Google search into the same pipeline:

1. Go to `google.com/alerts`
2. Enter a search term, e.g. `scholarship Nigeria undergraduate engineering`
3. Click **Show options** → set **Deliver to: RSS feed** (this is the key setting)
4. Create the alert, then click the RSS icon next to it on your alerts dashboard and copy the link
5. Paste it into `GOOGLE_ALERTS_FEEDS` in `scraper.py`:
   ```python
   GOOGLE_ALERTS_FEEDS = [
       {"name": "Google Alert: scholarship Nigeria", "url": "https://www.google.com/alerts/feeds/.../..."},
   ]
   ```
   Repeat for as many search terms as you want tracked (e.g. one per opportunity type). Each one runs through the same relevance filter as every other feed.

## Customizing discovery

Edit `scraper.py`: `FEEDS` (sources), `PROFILE_BOOST` (your eligibility keywords), `EXCLUDE` (currently skips PhD-only and senior roles).

## Changing the scan schedule

Edit `SCRAPE_HOURS` in `run.py` (24-hour format, server local time). Currently `(0, 9, 16)` = 12am, 9am, 4pm.

## Deployment

### Option A — laptop (what you've been testing with)
`python run.py`. Works, but stops the moment your laptop is off.

### Option B — free 24/7 (Render + cron-job.org + Turso)
This is the free path: one small always-on-ish web service, woken up
on a schedule by a free external pinger, sharing a small free cloud
database. WhatsApp is intentionally left out of this for now — the
same service can take on WhatsApp's webhook later with no changes to
this setup.

**Why a shared database is needed:** Render's free tier doesn't
reliably keep local files across restarts, so `web_server.py` needs
somewhere durable to remember your pipeline, checklists, and which
reminders it's already sent. Turso is a small free cloud database that
gives it that memory, standing in for the local `opportunities.db`
file you've been using on your laptop.

**1. Create the shared database (2 min)**
- Sign up at turso.tech, create a database
- Get its URL and an auth token from the dashboard (or `turso db show` /
  `turso db tokens create` if using their CLI)

**2. Deploy the web service on Render**
- New Web Service → connect a GitHub repo with this project (push it
  there first if you haven't)
- Build command: `pip install requests beautifulsoup4 pypdf flask libsql`
- Start command: `python web_server.py`
- Environment variables: `TELEGRAM_BOT_TOKEN`, `GROQ_API_KEY`,
  `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and `TICK_SECRET` (make up
  any password-like string — this stops strangers from finding your
  URL and triggering it)
- Once deployed, note the URL Render gives you, e.g.
  `https://opportunity-assistant.onrender.com`

**3. Set up the free scheduler**
- Sign up at cron-job.org (free)
- Create a new cron job that visits:
  `https://your-app.onrender.com/tick?secret=<your TICK_SECRET>`
- Set it to run every 5-15 minutes

Each visit checks Telegram once, delivers any due reminders, and runs
the discovery scan during your 12am/9am/4pm windows (Africa/Lagos time
specifically, regardless of what timezone Render's servers run in) —
and keeps the free service awake in between visits.

**What this costs:** $0. Turso's free tier, Render's free web service
tier, and cron-job.org's free scheduling all comfortably cover a
single-user hobby bot like this one.

**Trade-off to know:** Telegram replies land within your ping interval
(5-15 minutes) rather than being instant, since it's checked on a
timer rather than held open continuously.

**Adding WhatsApp later:** add a `/webhook` route to this same
`web_server.py` (the code from `whatsapp_bot.py` can move in directly),
give it `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, and
`WHATSAPP_VERIFY_TOKEN` as additional Render env vars, then point
Meta's webhook at `https://your-app.onrender.com/webhook`. It already
shares the same Turso database, so nothing about the Telegram/tick
side needs to change.

### Option C — a small VPS (~$4-6/month)
Simpler and instant instead of on a timer, if you'd rather pay a little
than manage the free-tier scheduling trade-off above. `python run.py`
on any always-on Linux box works exactly like your laptop does now,
and covers WhatsApp too without the split-services complexity.

## Edge cases

- Login-walled forms: bot files the link anyway; screenshot the form → `python extractor.py screenshot.png` locally.
- Scanned PDFs: screenshot pages → image mode.
- Groq vision model name rotates — update `GROQ_VISION_MODEL` in `extractor.py` if image extraction errors.
- Scholars World's RSS feed URL (`/feed/`) follows standard WordPress convention but wasn't directly verified live — if it 404s, run `python scraper.py` once and it'll print a `[!]` warning without breaking the other feeds.
