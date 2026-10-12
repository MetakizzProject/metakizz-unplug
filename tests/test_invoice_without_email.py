"""Invoices are stored but NOT emailed while INVOICE_EMAIL_ENABLED is off
(the default since 13-oct-2026).

Run from the repo root (needs requirements installed):
    python -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


class InvoiceWithoutEmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import flask_sqlalchemy  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("Flask-SQLAlchemy / reportlab not installed")
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(cls.tmp.name, "t.db")
        os.environ.setdefault("FLASK_SECRET_KEY", "test")
        from app.app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _payment(self, charge_id):
        from app.models import db, CirclePayment
        cp = CirclePayment(
            stripe_charge_id=charge_id,
            email="buyer@example.com",
            customer_name="Buyer Test",
            amount_cents=1500,
            currency="eur",
            paid_at=datetime.now(timezone.utc),
            description="The MetaKizz Community",
        )
        db.session.add(cp)
        db.session.commit()
        return cp

    def test_default_stores_number_and_pdf_without_email(self):
        with self.app.app_context(), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOICE_EMAIL_ENABLED", None)
            from app.routes import admin
            cp = self._payment("ch_test_no_email")
            with mock.patch.object(admin, "send_invoice_email") as send:
                ok = admin._generate_and_send_invoice(cp)
            self.assertTrue(ok)
            send.assert_not_called()
            self.assertRegex(cp.invoice_id or "", r"^INV-\d{4}-\d{4,}$")
            self.assertTrue(cp.invoice_pdf_bytes and cp.invoice_pdf_bytes[:4] == b"%PDF")
            self.assertIsNone(cp.invoice_sent_at)

    def test_switch_on_sends_email(self):
        with self.app.app_context(), mock.patch.dict(os.environ, {"INVOICE_EMAIL_ENABLED": "1"}):
            from app.routes import admin
            cp = self._payment("ch_test_with_email")
            with mock.patch.object(admin, "send_invoice_email", return_value=True) as send:
                ok = admin._generate_and_send_invoice(cp)
            self.assertTrue(ok)
            send.assert_called_once()
            self.assertIsNotNone(cp.invoice_sent_at)


if __name__ == "__main__":
    unittest.main()
