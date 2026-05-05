"""Render the Class 1 (and Class 2) ready email to local HTML files
so you can open them in a browser before doing a bulk send.

Usage:
    python tools/preview_class_ready.py

Outputs:
    .tmp/class1_ready_public.html
    .tmp/class1_ready_community.html
    .tmp/class2_ready_public.html
    .tmp/class2_ready_community.html

The rendered HTML uses the production-style absolute URL for images
(https://metakizz-ambassador.onrender.com/static/email/...) so what
you see locally is exactly what the recipient will see in Gmail.
"""
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.app import create_app  # noqa: E402
from flask import render_template  # noqa: E402

OUT_DIR = ROOT / ".tmp"
OUT_DIR.mkdir(exist_ok=True)

PROD_URL = "https://metakizz-ambassador.onrender.com"
LANDING_URL = "https://hackingtheurbankizcode.com"


def render_one(class_number: int, community: bool) -> Path:
    variant = "community" if community else "public"
    out = OUT_DIR / f"class{class_number}_ready_{variant}.html"

    app = create_app()
    with app.app_context(), app.test_request_context():
        html = render_template(
            "emails/class_ready.html",
            first_name="Alvaro",
            community=community,
            class_number=class_number,
            class_url=f"{LANDING_URL}/class{class_number}",
            dashboard_url=f"{PROD_URL}/dashboard/SAMPLE_CODE",
            unsubscribe_url=f"{PROD_URL}/unsubscribe?token=SAMPLE_TOKEN",
            app_url=PROD_URL,
        )

    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    files = []
    for class_number in (1, 2):
        for community in (False, True):
            p = render_one(class_number, community)
            files.append(p)
            print(f"  ✓ {p.relative_to(ROOT)}")

    # Open Class 1 public + community side-by-side in default browser
    primary = OUT_DIR / "class1_ready_public.html"
    print(f"\nOpening {primary.relative_to(ROOT)} in browser...")
    webbrowser.open(f"file://{primary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
