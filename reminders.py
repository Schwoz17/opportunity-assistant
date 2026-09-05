"""
Reminder engine — solves "I starred it and forgot."

Escalation ladder per opportunity (each fires exactly once):
  7d      : gentle heads-up with checklist progress
  3d      : firm — unfinished heavy items called out by name
  1d      : final call
  overdue : deadline passed while still active
  stale   : sat in 'seen'/'interested' for 5+ days untouched

deliver() broadcasts each nudge to your account on EVERY connected
channel (Telegram + WhatsApp).
"""

from datetime import datetime, timedelta

import db
import channels

STALE_AFTER_DAYS = 5


def _progress_line(oid: int) -> str:
    done, total = db.checklist_progress(oid)
    if total == 0:
        return "No checklist yet — paste its link to me to extract requirements."
    return f"Checklist: {done}/{total} done."


def _unfinished_heavy(oid: int) -> list:
    return [i for i in db.get_checklist(oid)
            if not i["done"] and i["effort"] == "heavy"]


def build_nudges() -> list:
    """Return [(oid, kind, message), ...] for every nudge due now."""
    out = []
    for o in db.list_active():
        oid, title, cat = o["id"], o["title"], o["category"]

        if o["deadline"]:
            days = db.days_until(o["deadline"])
            if days is None:
                continue

            ladder = []
            if days < 0:
                ladder.append(("overdue",
                    f"⚫ [{oid}] '{title}' ({cat}) deadline ({o['deadline']}) has PASSED.\n"
                    f"Did you submit? Reply: status {oid} submitted — "
                    f"or close it: status {oid} rejected"))
            elif days <= 1:
                heavy = _unfinished_heavy(oid)
                heavy_txt = ("\nSTILL OPEN: " + "; ".join(i["item"] for i in heavy)) if heavy else ""
                ladder.append(("1d",
                    f"🔴 [{oid}] FINAL CALL: '{title}' ({cat}) closes "
                    f"{'TODAY' if days == 0 else 'TOMORROW'} ({o['deadline']}).\n"
                    f"{_progress_line(oid)}{heavy_txt}"))
            elif days <= 3:
                heavy = _unfinished_heavy(oid)
                heavy_txt = ("\nHeavy items not done: " + "; ".join(i["item"] for i in heavy)) if heavy else ""
                ladder.append(("3d",
                    f"🟠 [{oid}] '{title}' ({cat}) closes in {days} days ({o['deadline']}).\n"
                    f"{_progress_line(oid)}{heavy_txt}"))
            elif days <= 7:
                ladder.append(("7d",
                    f"🟡 [{oid}] '{title}' ({cat}) closes in {days} days ({o['deadline']}).\n"
                    f"{_progress_line(oid)}"))

            for kind, msg in ladder:
                if not db.nudge_already_sent(oid, kind):
                    out.append((oid, kind, msg))

        # Staleness — the starred-and-forgot killer
        if o["status"] in ("seen", "interested"):
            updated = datetime.fromisoformat(o["updated_at"].replace(" ", "T"))
            if datetime.now() - updated > timedelta(days=STALE_AFTER_DAYS):
                if not db.nudge_already_sent(oid, "stale"):
                    dl = (f"(deadline {o['deadline']})" if o["deadline"]
                          else "(no deadline set — find it!)")
                    out.append((oid, "stale",
                        f"👻 [{oid}] '{title}' ({cat}) has been sitting in "
                        f"'{o['status']}' for {STALE_AFTER_DAYS}+ days {dl}.\n"
                        f"Apply, or kill it: status {oid} rejected"))
    return out


def deliver():
    """Build all due nudges and broadcast to every connected channel."""
    owners = db.all_owners()
    if not owners:
        return
    for oid, kind, msg in build_nudges():
        channels.broadcast(owners, msg)
        db.record_nudge(oid, kind)


if __name__ == "__main__":
    nudges = build_nudges()
    if not nudges:
        print("Nothing due. Pipeline is healthy.")
    for oid, kind, msg in nudges:
        print(f"--- [{kind}] ---\n{msg}\n")
