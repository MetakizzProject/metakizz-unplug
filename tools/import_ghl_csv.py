"""Import a GoHighLevel Contacts CSV export into our DB.

Backfills `ghl_contact_id`, `ghl_tags`, `phone_number`, `country_code`
on existing Ambassador rows (matched by lowercase email). Creates
ghost-lead rows for emails that aren't in our DB yet, so the Leads
dashboard can show GHL-only contacts too.

Usage (local):
    python tools/import_ghl_csv.py path/to/export.csv
    python tools/import_ghl_csv.py path/to/export.csv --dry-run
    python tools/import_ghl_csv.py path/to/export.csv --create-missing

Usage (Render shell — when DATABASE_URL is set in the env):
    same as above; will hit Postgres instead of local SQLite.

Idempotent: re-running with the same CSV updates fields but doesn't
duplicate rows.
"""

import csv
import sys
import os
import logging
import argparse
from datetime import datetime, timezone

# Allow running from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app import create_app
from app.models import db, Ambassador

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _parse_phone(raw):
    """Normalize a phone string to (e164, country_iso) or (None, None)."""
    if not raw:
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        from app.services.phone import parse as parse_phone
        parsed = parse_phone(raw)
        if parsed:
            return parsed["e164"], parsed["country_code"]
    except Exception:
        logger.exception("phone parsing failed for %r", raw)
    return None, None


def _normalize_tags(raw):
    """Strip + sort + dedupe tag list. Returns comma-separated string or None."""
    if not raw:
        return None
    parts = [t.strip() for t in raw.split(",")]
    parts = sorted({p for p in parts if p})
    return ",".join(parts) if parts else None


def import_csv(csv_path, dry_run=False, create_missing=False):
    if not os.path.exists(csv_path):
        logger.error("CSV not found: %s", csv_path)
        sys.exit(2)

    app = create_app()
    stats = {
        "rows_seen": 0,
        "matched_updated": 0,
        "matched_unchanged": 0,
        "ghost_created": 0,
        "ghost_skipped_no_email": 0,
        "ghost_skipped_no_create": 0,
        "errors": 0,
    }

    with app.app_context(), open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # GHL exports column names are space-separated, e.g. "Contact Id".
        # Normalize accessors so casing/whitespace differences don't break us.
        for row in reader:
            stats["rows_seen"] += 1
            email = (row.get("Email") or "").strip().lower()
            if not email:
                stats["ghost_skipped_no_email"] += 1
                continue

            ghl_id = (row.get("Contact Id") or "").strip() or None
            first = (row.get("First Name") or "").strip()
            last = (row.get("Last Name") or "").strip()
            full_name = (first + " " + last).strip() or email.split("@")[0]
            phone_raw = (row.get("Phone") or "").strip()
            tags = _normalize_tags(row.get("Tags") or "")
            phone_e164, country_iso = _parse_phone(phone_raw)

            try:
                amb = Ambassador.query.filter(
                    db.func.lower(Ambassador.email) == email
                ).first()

                if amb is not None:
                    # ── Existing row: update only fields that are missing or stale ──
                    changed = False
                    if ghl_id and amb.ghl_contact_id != ghl_id:
                        amb.ghl_contact_id = ghl_id
                        changed = True
                    if tags and amb.ghl_tags != tags:
                        amb.ghl_tags = tags
                        changed = True
                    if phone_e164 and not amb.phone_number:
                        amb.phone_number = phone_e164
                        changed = True
                    if country_iso and not amb.country_code:
                        amb.country_code = country_iso
                        changed = True
                    if changed:
                        stats["matched_updated"] += 1
                    else:
                        stats["matched_unchanged"] += 1
                else:
                    # ── No matching ambassador: create a ghost lead if asked ──
                    if not create_missing:
                        stats["ghost_skipped_no_create"] += 1
                        continue

                    import secrets
                    def _gen_code():
                        return secrets.token_urlsafe(6)[:8]

                    new_amb = Ambassador(
                        name=full_name,
                        email=email,
                        referral_code=_gen_code(),
                        dashboard_code=_gen_code(),
                        source="ghl_import",
                        ghl_contact_id=ghl_id,
                        ghl_tags=tags,
                        phone_number=phone_e164,
                        country_code=country_iso,
                    )
                    db.session.add(new_amb)
                    stats["ghost_created"] += 1
            except Exception:
                stats["errors"] += 1
                logger.exception("failed to import row email=%s", email)

            # Commit every 100 rows to keep the transaction small.
            if stats["rows_seen"] % 100 == 0:
                if dry_run:
                    db.session.rollback()
                else:
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        logger.exception("commit failed at row %d", stats["rows_seen"])
                        stats["errors"] += 1

        # Final commit.
        if dry_run:
            db.session.rollback()
        else:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("final commit failed")
                stats["errors"] += 1

    logger.info("")
    logger.info("─── Import summary ───")
    for k, v in stats.items():
        logger.info("  %-26s %d", k, v)
    if dry_run:
        logger.info("(dry-run — no changes persisted)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", help="Path to the GHL Contacts CSV export")
    parser.add_argument("--dry-run", action="store_true", help="Don't persist changes")
    parser.add_argument(
        "--create-missing", action="store_true",
        help="Create ghost Ambassador rows for emails not already in our DB",
    )
    args = parser.parse_args()
    import_csv(args.csv_path, dry_run=args.dry_run, create_missing=args.create_missing)
