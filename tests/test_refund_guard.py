"""Unit tests for the deposit-refund guard (app/services/refund_guard.py).

Run from the repo root:
    python -m unittest discover -s tests -t .
(or `pytest tests/` if pytest is installed).
"""

import unittest

from app.services.refund_guard import (
    REASON_AMOUNT_BELOW_DEPOSIT,
    REASON_AMOUNT_UNKNOWN,
    REASON_EXCLUDED_PRODUCT,
    excluded_product_ids,
    refund_skip_reason,
    resolve_product_ids,
)

COMMUNITY = "prod_community"
PROGRAMME = "prod_metadancers"


def fake_charge(amount, invoice=None):
    """Minimal `charge.succeeded` data.object."""
    return {
        "id": "ch_test_%d" % amount,
        "object": "charge",
        "amount": amount,
        "currency": "eur",
        "invoice": invoice,
        "payment_intent": "pi_test_%d" % amount,
        "billing_details": {"email": "buyer@example.com"},
    }


class RefundSkipReasonTests(unittest.TestCase):

    def test_community_charge_1500_is_skipped(self):
        charge = fake_charge(1500)
        self.assertEqual(
            refund_skip_reason(charge["amount"], 10000),
            REASON_AMOUNT_BELOW_DEPOSIT,
        )

    def test_programme_charge_48000_is_refunded(self):
        charge = fake_charge(48000)
        self.assertIsNone(refund_skip_reason(charge["amount"], 10000))

    def test_programme_charge_with_excluded_product_is_skipped(self):
        charge = fake_charge(48000)
        self.assertEqual(
            refund_skip_reason(charge["amount"], 10000, {COMMUNITY}, {COMMUNITY}),
            REASON_EXCLUDED_PRODUCT,
        )

    def test_programme_charge_with_other_product_is_refunded(self):
        self.assertIsNone(
            refund_skip_reason(48000, 10000, {PROGRAMME}, {COMMUNITY}),
        )

    def test_charge_equal_to_deposit_is_refunded(self):
        self.assertIsNone(refund_skip_reason(10000, 10000))

    def test_missing_deposit_amount_defaults_to_100_eur(self):
        self.assertEqual(refund_skip_reason(9999, None), REASON_AMOUNT_BELOW_DEPOSIT)
        self.assertIsNone(refund_skip_reason(10000, None))

    def test_unknown_amount_is_skipped(self):
        self.assertEqual(refund_skip_reason(None, 10000), REASON_AMOUNT_UNKNOWN)
        self.assertEqual(refund_skip_reason("n/a", 10000), REASON_AMOUNT_UNKNOWN)


class ExcludedProductIdsTests(unittest.TestCase):

    def test_parses_comma_separated_list(self):
        self.assertEqual(
            excluded_product_ids(" prod_a, prod_b ,,prod_c "),
            {"prod_a", "prod_b", "prod_c"},
        )

    def test_empty_by_default(self):
        self.assertEqual(excluded_product_ids(""), set())


class _FakeInvoiceApi:
    def __init__(self, lines):
        self._lines = lines

    def retrieve(self, invoice_id, api_key=None, **kwargs):
        return {"id": invoice_id, "lines": {"data": self._lines}}


class _FakeChargeApi:
    def __init__(self, invoice_id):
        self._invoice_id = invoice_id

    def retrieve(self, charge_id, api_key=None, **kwargs):
        return {"id": charge_id, "invoice": self._invoice_id}


class _FakeStripe:
    def __init__(self, lines, charge_invoice=None):
        self.Invoice = _FakeInvoiceApi(lines)
        self.Charge = _FakeChargeApi(charge_invoice)


class _BrokenStripe:
    class Charge:
        @staticmethod
        def retrieve(*args, **kwargs):
            raise RuntimeError("network down")


class ResolveProductIdsTests(unittest.TestCase):

    def test_invoice_line_classic_shape(self):
        stripe = _FakeStripe([{"price": {"id": "price_x", "product": COMMUNITY}}])
        ids = resolve_product_ids(stripe, "charge.succeeded", fake_charge(1500, "in_1"), "sk_test")
        self.assertEqual(ids, {COMMUNITY})

    def test_invoice_line_new_pricing_shape(self):
        stripe = _FakeStripe([{"pricing": {"price_details": {"product": PROGRAMME}}}])
        ids = resolve_product_ids(stripe, "charge.succeeded", fake_charge(48000, "in_2"), "sk_test")
        self.assertEqual(ids, {PROGRAMME})

    def test_charge_without_invoice_is_re_read(self):
        stripe = _FakeStripe([{"price": {"product": COMMUNITY}}], charge_invoice="in_3")
        ids = resolve_product_ids(stripe, "charge.succeeded", fake_charge(1500), "sk_test")
        self.assertEqual(ids, {COMMUNITY})

    def test_errors_and_missing_key_return_empty_set(self):
        self.assertEqual(
            resolve_product_ids(_BrokenStripe, "charge.succeeded", fake_charge(1500), "sk_test"),
            set(),
        )
        self.assertEqual(
            resolve_product_ids(_FakeStripe([]), "charge.succeeded", fake_charge(1500, "in_4"), ""),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
