#!/usr/bin/env python3
"""Drive the user's REAL Chrome (not Chromium) via AppleScript to draft
WhatsApp Web messages for a /admin/leads segment.

Why AppleScript instead of Playwright: Alvaro already has Chrome open
with WA Web logged into his personal profile. Playwright launches a
fresh Chromium with no session. AppleScript drives the existing browser.
One-time setup: System Settings → Privacy & Security → Automation →
grant Terminal access to "Google Chrome".

Data source: hits the production admin JSON endpoint
/admin/leads/segment.json on Render. ADMIN_PASSWORD comes from local
.env (loaded via python-dotenv or the env). Set METAKIZZ_BASE to point
elsewhere (defaults to the prod Render URL).

Usage:
  python tools/wa_draft_segment.py --seg deposit_paid --dry-run
  python tools/wa_draft_segment.py --seg deposit_paid --limit 3
  python tools/wa_draft_segment.py --seg hot_no_reserve --limit 50

Mechanism: for each lead, navigate the active tab to
`https://web.whatsapp.com/send?phone=X&text=Y`. WA Web opens that chat
and prefills the input box. Switching to the next URL leaves the
previous chat's input populated — drafts persist per-chat in WA Web's
local storage, so the chat list shows "Draft: Hey ..." for each contact
when the run finishes.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request


SEGMENTS = ("deposit_paid", "hot_no_reserve", "watched_no_reserve", "no_engagement")
DEFAULT_BASE = os.environ.get("METAKIZZ_BASE", "https://metakizz-ambassador.onrender.com").rstrip("/")


def _load_dotenv():
    """Light-touch .env loader so ADMIN_PASSWORD is picked up without
    polluting the user's shell. Only sets keys that aren't already set."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def chrome_navigate(url: str) -> bool:
    safe = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        '  activate\n'
        '  if (count of windows) = 0 then make new window\n'
        f'  set URL of active tab of front window to "{safe}"\n'
        'end tell'
    )
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"   ⚠️  AppleScript error: {r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def normalize_phone(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def build_wa_url(phone: str, message: str) -> str:
    return (
        "https://web.whatsapp.com/send?"
        f"phone={normalize_phone(phone)}&text={urllib.parse.quote(message)}"
    )


def admin_login(base: str, password: str) -> str:
    """POST to /admin/login, capture session cookie. Returns Cookie header value."""
    url = f"{base}/admin/login"
    data = urllib.parse.urlencode({"password": password}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    # Don't auto-follow — login redirects, we need the Set-Cookie from THAT response.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw): return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        resp = e
    cookies = resp.headers.get_all("Set-Cookie") or []
    # Extract just the name=value parts joined with ;
    pieces = []
    for c in cookies:
        pieces.append(c.split(";", 1)[0])
    if not pieces:
        raise SystemExit("❌ Login failed — no Set-Cookie returned. Check ADMIN_PASSWORD.")
    return "; ".join(pieces)


def fetch_segment_json(base: str, cookie: str, seg: str, limit=None):
    qs = urllib.parse.urlencode({k: v for k, v in [("seg", seg), ("limit", limit)] if v is not None})
    url = f"{base}/admin/leads/segment.json?{qs}"
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--seg', required=True, choices=SEGMENTS,
                   help='Segment key. See app/services/temperature.py:SEGMENT_LABELS')
    p.add_argument('--limit', type=int, default=None, help='Cap number of contacts this run')
    p.add_argument('--start-from', type=int, default=1, help='1-based index to resume from')
    p.add_argument('--delay', type=float, default=8.0,
                   help='Seconds between chats. 8s gives WA Web time to load + persist the draft')
    p.add_argument('--base', default=DEFAULT_BASE, help='Admin base URL (default: prod Render)')
    p.add_argument('--dry-run', action='store_true', help='List who/what, no Chrome control')
    args = p.parse_args()

    _load_dotenv()
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not password:
        raise SystemExit("❌ ADMIN_PASSWORD not set (check .env or env vars).")

    print(f"\n➜ Admin base: {args.base}")
    print(f"➜ Logging in...")
    cookie = admin_login(args.base, password)
    print(f"   ✓ Session established\n")

    print(f"➜ Fetching segment '{args.seg}'...")
    data = fetch_segment_json(args.base, cookie, args.seg, args.limit)
    leads = data.get("rows", [])
    print(f"   ✓ {len(leads)} leads with phone in segment\n")

    if args.start_from > 1:
        leads = leads[args.start_from - 1:]

    if not leads:
        print("Nothing to do.")
        return

    if args.dry_run:
        for i, r in enumerate(leads, args.start_from):
            print(f"[{i:>3}] {(r['name'] or '(no name)'):<28}  {r['phone']}")
            msg = r["message"]
            print(f"       → {msg[:120]}{'...' if len(msg) > 120 else ''}\n")
        return

    print(f"➜ Drafting {len(leads)} chats in your Chrome.")
    print(f"   (Chrome must be open + WA Web logged in. {args.delay:.0f}s between each.)\n")
    print("   Tip: bring Chrome to focus and don't type while this runs.\n")
    time.sleep(2)

    end_idx = args.start_from + len(leads) - 1
    for i, r in enumerate(leads, args.start_from):
        name, phone, msg = r["name"] or "(no name)", r["phone"], r["message"]
        url = build_wa_url(phone, msg)
        print(f"[{i}/{end_idx}] {name}  ·  {phone}")
        if not chrome_navigate(url):
            print("   ⚠️  Navigation failed, skipping.")
            continue
        print(f"   ✓ Drafted: {msg[:80]}{'...' if len(msg) > 80 else ''}")
        if i < end_idx:
            time.sleep(args.delay)

    print("\n✅ Done. Check WA Web — each chat shows 'Draft: Hey ...' in the chat list.")
    print("   Click each, review, send when ready.\n")


if __name__ == "__main__":
    main()
