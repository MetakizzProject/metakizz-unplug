"""GET /invite-partner redirects to the new invite page (13-oct-2026).

Run from the repo root:
    python -m unittest discover -s tests -t .
"""

import unittest

from app.services.partner_invite_redirect import DEFAULT_NEW_INVITE_URL, new_invite_url


class NewInviteUrlTests(unittest.TestCase):
    def test_default_redirects_to_member_app(self):
        self.assertEqual(new_invite_url({}, env={}), DEFAULT_NEW_INVITE_URL)

    def test_keeps_buyer_params(self):
        url = new_invite_url({"buyer_name": " Ana María ", "buyer_email": "ana@example.com"}, env={})
        self.assertEqual(
            url,
            DEFAULT_NEW_INVITE_URL + "?buyer_name=Ana+Mar%C3%ADa&buyer_email=ana%40example.com",
        )

    def test_ignores_other_and_empty_params(self):
        url = new_invite_url({"buyer_name": "", "utm_source": "x", "buyer_email": "a@b.co"}, env={})
        self.assertEqual(url, DEFAULT_NEW_INVITE_URL + "?buyer_email=a%40b.co")

    def test_custom_url(self):
        self.assertEqual(
            new_invite_url({}, env={"PARTNER_INVITE_REDIRECT_URL": "https://example.com/x"}),
            "https://example.com/x",
        )

    def test_empty_env_keeps_old_form(self):
        self.assertIsNone(new_invite_url({"buyer_email": "a@b.co"}, env={"PARTNER_INVITE_REDIRECT_URL": ""}))


if __name__ == "__main__":
    unittest.main()
