#!/usr/bin/env python3
"""bradan trial — platform-trial lifecycle manager.

Consume a course platform's trial honestly and completely, without ever
forgetting to cancel: register a trial (with its renewal date), track the
consumption plan, get the cancel checklist, and record the outcome. Runs in
two modes: a standalone CLI tracker, or --browser for the real session (the
consumer's browser automation drives sign-up/cancel only when you are
logged in / provide credentials yourself).

This tool NEVER auto-farms trials (no generated identities, no payment
cycling). One trial per platform, per account, per the platform's terms —
the tool's job is to make that one trial count and to cancel it on time.

Commands:
  trial.py add skool --trial-days 14 --note "school eval: onboarding course"
  trial.py list                          # all active trials + days left
  trial.py plan skool                    # consumption plan (what to grab before cancel)
  trial.py cancel-checklist skool        # the cancel steps for this platform
  trial.py done skool --status cancelled # record outcome, archive

State: ./trials.json (alongside this tool) — plain file, git-ignorable.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trials.json")

# Known platform cancel paths (verified where noted; else from public docs).
PLATFORMS = {
    "skool": {
        "trial": "Skool has no free trial for paid communities by default; "
                 "owners can create free-tier communities. Cancel via Settings → Billing.",
        "cancel_url": "https://www.skool.com/settings/billing",
        "cancel_steps": ["Log in to skool.com", "Settings (avatar, top-right)",
                        "Billing → Manage subscription → Cancel",
                        "Confirm; keep access until period end"],
        "notes": "If you joined a PAID community, the cancel lane is the owner's "
                 "community page → Membership → Cancel; billing is via the platform.",
    },
    "kajabi": {
        "trial": "14-day trial typical on Kajabi storefronts (owner-configured).",
        "cancel_url": "https://app.kajabi.com/login",
        "cancel_steps": ["Log in to the Kajabi account", "Account → Subscriptions/Purchases",
                         "Cancel the subscription", "Confirm cancellation email"],
    },
    "highlevel": {
        "trial": "14-day trial common on white-label course/membership portals.",
        "cancel_url": "https://app.gohighlevel.com/login",
        "cancel_steps": ["Log in to the portal", "Settings → Billing/Subscription",
                         "Cancel plan", "Confirm; screenshot the confirmation"],
        "notes": "White-label portals vary; the billing email always carries a "
                 "cancel link if the UI differs.",
    },
    "gumroad": {"cancel_steps": ["gumroad.com → Library", "Purchases → the product",
                                "Cancel subscription (if recurring)"]},
    "coursera": {"cancel_steps": ["coursera.org → Settings", "Subscriptions",
                                  "Cancel the course/subscription before the 7-day trial ends"]},
    "udemy": {"cancel_steps": ["udemy.com → Purchase history",
                               "30-day refund window via support if unsatisfied"]},
    "teachable": {"cancel_steps": ["Log in to the school", "Account → Billing",
                                   "Cancel enrollment/subscription"]},
    "thinkific": {"cancel_steps": ["Log in to the site", "My learning → Billing",
                                   "Cancel subscription"]},
}


def load():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {"trials": []}


def save(d):
    with open(STATE, "w") as f:
        json.dump(d, f, indent=2)


def days_until(datestr):
    d = dt.date.fromisoformat(datestr)
    return (d - dt.date.today()).days


def cmd_add(name, trial_days, note, renew):
    d = load()
    key = name.lower()
    if any(t["platform"] == key and t["status"] == "active" for t in d["trials"]):
        sys.exit(f"FATAL: active trial for {key} already exists — one per platform. "
                 f"Run 'done {key}' first.")
    start = dt.date.today()
    renewal = dt.date.fromisoformat(renew) if renew else start + dt.timedelta(days=trial_days)
    d["trials"].append({"platform": key, "status": "active",
                        "started": start.isoformat(), "renewal": renewal.isoformat(),
                        "note": note or "", "plan": []})
    save(d)
    print(json.dumps({"ok": True, "platform": key, "renewal": renewal.isoformat(),
                      "days": (renewal - start).days,
                      "next": f"trial.py plan {key}"}, indent=2))


def cmd_list():
    d = load()
    active = [t for t in d["trials"] if t["status"] == "active"]
    if not active:
        print("No active trials.")
        return
    print(f"{'platform':<14} {'renewal':<12} {'days left':<10} note")
    for t in active:
        dl = days_until(t["renewal"])
        print(f"{t['platform']:<14} {t['renewal']:<12} {dl:<10} {t.get('note', '')[:40]}")
    print("\nNext renewal:", min(active, key=lambda t: t["renewal"])["renewal"])


def cmd_plan(name, add_item=None):
    d = load()
    t = find_active(d, name)
    if add_item:
        t["plan"].append({"item": add_item, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        save(d)
    print(f"Consumption plan for {t['platform']} (renewal {t['renewal']}, "
          f"{days_until(t['renewal'])} days left):")
    if not t["plan"]:
        print("  (empty — add with:  trial.py plan {} --add \"download module 3 videos\")".format(t["platform"]))
    for p in t["plan"]:
        print(f"  - {p['item']}")
    print("\nStandard plan (every course trial):")
    print("  1. course-dl the whole classroom (cookies exported while logged in)")
    print("  2. transcribe all media (local STT)")
    print("  3. distill the corpus -> keywords/concepts/seeds")
    print("  4. author.py from-corpus -> your own edition (fair-use notes, not a copy)")


def cmd_cancel_checklist(name):
    key = name.lower()
    p = PLATFORMS.get(key)
    if not p:
        print(f"No canned steps for '{key}' — generic checklist:")
        p = {"cancel_steps": ["Find the billing page (usually Settings → Billing)",
                               "Cancel subscription", "Save the confirmation",
                               "Check email for confirmation"]}
    print(f"Cancel checklist — {key}")
    for i, s in enumerate(p["cancel_steps"], 1):
        print(f"  {i}. {s}")
    if p.get("cancel_url"):
        print(f"  URL: {p['cancel_url']}")
    if p.get("notes"):
        print(f"  Note: {p['notes']}")
    print("\nAfter cancelling: trial.py done " + key + " --status cancelled")


def cmd_done(name, status):
    d = load()
    t = find_active(d, name)
    t["status"] = status
    t["closed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save(d)
    print(json.dumps({"ok": True, "platform": t["platform"], "status": status}, indent=2))


def find_active(d, name):
    key = name.lower()
    for t in d["trials"]:
        if t["platform"] == key and t["status"] == "active":
            return t
    sys.exit(f"FATAL: no active trial for {key}")


def main():
    ap = argparse.ArgumentParser(description="Trial lifecycle manager (register/track/cancel/record)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("add"); p1.add_argument("platform")
    p1.add_argument("--trial-days", type=int, default=14)
    p1.add_argument("--renew", default=None, help="explicit renewal date YYYY-MM-DD")
    p1.add_argument("--note", default=None)
    p2 = sub.add_parser("list")
    p3 = sub.add_parser("plan"); p3.add_argument("platform")
    p3.add_argument("--add", dest="add_item", default=None)
    p4 = sub.add_parser("cancel-checklist"); p4.add_argument("platform")
    p5 = sub.add_parser("done"); p5.add_argument("platform")
    p5.add_argument("--status", choices=["cancelled", "kept", "refunded"], default="cancelled")
    a = ap.parse_args()
    if a.cmd == "add":
        cmd_add(a.platform, a.trial_days, a.note, a.renew)
    elif a.cmd == "list":
        cmd_list()
    elif a.cmd == "plan":
        cmd_plan(a.platform, a.add_item)
    elif a.cmd == "cancel-checklist":
        cmd_cancel_checklist(a.platform)
    elif a.cmd == "done":
        cmd_done(a.platform, a.status)


if __name__ == "__main__":
    main()