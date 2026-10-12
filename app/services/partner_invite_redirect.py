"""Partner invite moved off Render (13-oct-2026).

The Couple invite now lives in the member app
(https://dashboard.metakizzproject.com/invite-partner) and the admin panel
(Supabase): it validates the buyer against Stripe payments, adds the partner
in Circle and links the couple in Supabase automatically.

Purchase emails already sent still point to
metakizz-ambassador.onrender.com/invite-partner, so this app keeps the URL
alive and redirects it there, query string included (?buyer_name&buyer_email).

Rollback without a code change: set PARTNER_INVITE_REDIRECT_URL to an empty
string in Render and the old form is served again.
"""

import os
from urllib.parse import urlencode

DEFAULT_NEW_INVITE_URL = "https://dashboard.metakizzproject.com/invite-partner"


def new_invite_url(args, env=None):
    """Where GET /invite-partner should redirect, or None to keep the old form.

    `args` is a mapping of query params (Flask's request.args works).
    """
    env = os.environ if env is None else env
    base = env.get("PARTNER_INVITE_REDIRECT_URL", DEFAULT_NEW_INVITE_URL).strip()
    if not base:
        return None
    params = {
        k: (args.get(k) or "").strip()
        for k in ("buyer_name", "buyer_email")
        if (args.get(k) or "").strip()
    }
    return f"{base}?{urlencode(params)}" if params else base
