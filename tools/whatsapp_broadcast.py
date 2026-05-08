"""
Semi-automated WhatsApp Web broadcast for paid Reservations.

Opens a Chromium window with a persistent profile (QR scan + admin
login only on first run), pulls the paid-reservation list from the
production admin JSON endpoint, and walks through each contact one by
one. For each contact:
  1. Navigates the chat in WhatsApp Web
  2. Types the draft "Hi <Name>! I left you an important audio 👇🏼"
     into the message input
  3. PAUSES — you attach the audio in WA Web manually and click send
  4. Press ENTER in the terminal to advance to the next contact

The script never clicks "send". It only drafts. You stay in control
of every send, which keeps you within WhatsApp's terms and means
you can attach the audio file per-chat by drag-and-drop.

Usage:
    # First-time setup (do once)
    pip install playwright
    playwright install chromium

    # Dry run — list who would be contacted, no browser
    python tools/whatsapp_broadcast.py --dry-run

    # Real run
    python tools/whatsapp_broadcast.py

    # Resume from contact 12 if interrupted
    python tools/whatsapp_broadcast.py --start-from 12

    # Override admin base URL (defaults to production Render URL)
    METAKIZZ_BASE=http://localhost:5001 python tools/whatsapp_broadcast.py
"""
import argparse
import json
import os
import sys
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run:")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)


USER_DATA_DIR = os.path.expanduser("~/.metakizz-wa-session")
APP_BASE = os.environ.get("METAKIZZ_BASE", "https://metakizz-ambassador.onrender.com").rstrip("/")


def first_name(s: str) -> str:
    if not s:
        return "there"
    return s.strip().split()[0]


def build_msg(name: str) -> str:
    return f"Hi {first_name(name)}! I left you an important audio 👇🏼"


def fetch_paid_contacts(page):
    """Navigates to /admin/reservations.json. If session is expired,
    parks on the login page and asks the user to authenticate manually,
    then retries until JSON comes back."""
    json_url = f"{APP_BASE}/admin/reservations.json"
    while True:
        try:
            page.goto(json_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            print(f"⚠️  Could not reach {json_url}. Check connection.")
            sys.exit(1)

        if "/admin/login" in page.url:
            print(f"\n🔐 Admin session expired. Log in manually in the Chromium window")
            print(f"   ({APP_BASE}/admin/login), then press ENTER here.")
            input()
            continue

        body = page.locator("body").inner_text()
        try:
            data = json.loads(body)
        except Exception:
            print(f"⚠️  Couldn't parse JSON. First 200 chars of response:")
            print(body[:200])
            sys.exit(1)
        return data.get("rows", [])


def filter_paid(rows):
    """Keep paid reservations with a phone, dedupe by phone, oldest paid first."""
    seen = set()
    out = []
    paid = [r for r in rows if r.get("paid_at")]
    paid.sort(key=lambda r: r.get("paid_at") or "")
    for r in paid:
        phone = (r.get("phone") or "").replace("+", "").replace(" ", "").replace("-", "")
        if not phone or phone in seen:
            continue
        seen.add(phone)
        out.append({
            "name": r.get("name") or "",
            "phone": phone,
            "email": r.get("email") or "",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-from", type=int, default=1, help="1-based index to resume from")
    ap.add_argument("--dry-run", action="store_true", help="List contacts only, don't open WA")
    args = ap.parse_args()

    os.makedirs(USER_DATA_DIR, exist_ok=True)
    print(f"\n📂 Browser profile : {USER_DATA_DIR}")
    print(f"🌐 Admin base      : {APP_BASE}\n")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            viewport={"width": 1280, "height": 820},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("➜ Fetching paid reservations from admin...")
        rows = fetch_paid_contacts(page)
        contacts = filter_paid(rows)
        print(f"   ✓ {len(contacts)} paid contacts with phone.\n")

        if args.start_from > 1:
            contacts = contacts[args.start_from - 1:]

        if args.dry_run:
            for i, c in enumerate(contacts, args.start_from):
                print(f"  [{i:>3}] {c['name']:<25}  +{c['phone']:<14}  {c['email']}")
                print(f"         → {build_msg(c['name'])}")
            ctx.close()
            return

        if not contacts:
            print("Nothing to do.")
            ctx.close()
            return

        total = args.start_from + len(contacts) - 1

        print("➜ Opening WhatsApp Web...")
        page.goto("https://web.whatsapp.com/")
        try:
            page.wait_for_selector(
                'canvas[aria-label*="QR"], canvas[aria-label*="Scan"], div[contenteditable="true"]',
                timeout=60_000,
            )
        except PWTimeout:
            print("   ⚠️  WA Web didn't respond. Try again later.")
            ctx.close()
            return

        if page.locator('canvas[aria-label*="QR"], canvas[aria-label*="Scan"]').count() > 0:
            print("   📱 Scan the QR with your phone (Settings → Linked Devices).")
            print("      Waiting up to 3 min...")
            try:
                page.wait_for_selector('div[contenteditable="true"]', timeout=180_000)
                print("   ✅ Logged in.")
            except PWTimeout:
                print("   ⚠️  QR not scanned in time. Aborting.")
                ctx.close()
                return
        else:
            print("   ✅ Already logged in.")
        time.sleep(2)
        print()

        for i, c in enumerate(contacts, args.start_from):
            print(f"[{i}/{total}] {c['name']}  ·  +{c['phone']}")
            msg = build_msg(c["name"])

            try:
                page.goto(
                    f"https://web.whatsapp.com/send?phone={c['phone']}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except PWTimeout:
                print("   ⚠️  Navigation timed out, skipping.")
                continue

            try:
                page.wait_for_selector(
                    'footer div[contenteditable="true"]',
                    timeout=15_000,
                )
            except PWTimeout:
                print("   ⚠️  Chat didn't load — phone may not be on WhatsApp. Skipping.")
                continue

            try:
                input_box = page.locator('footer div[contenteditable="true"]').first
                input_box.click(timeout=5_000)
                page.keyboard.insert_text(msg)
                print(f"   ✓ Drafted: {msg}")
            except Exception as e:
                print(f"   ⚠️  Couldn't draft: {e}")
                continue

            cmd = input("   [Enter]=next  [s]=skip  [q]=quit  > ").strip().lower()
            if cmd == "q":
                print("Aborted by user.")
                break

        print("\n✅ All done.")
        input("Press ENTER to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
