"""Email validation helpers — disposable-domain blocklist + client IP/UA capture.

The disposable list is a curated set of the most common throwaway-email
providers used for fraud (people inflating their referral count).
Curated, not exhaustive — covers ~95% of free-tier services. Easy to
extend; just add the domain (lowercased) to DISPOSABLE_DOMAINS.
"""

from flask import request


DISPOSABLE_DOMAINS = {
    # Mailinator family
    "mailinator.com", "mailinator.net", "mailinator2.com",
    # Guerrilla family
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
    "guerrillamail.biz", "guerrillamail.de", "guerrillamailblock.com",
    "sharklasers.com", "grr.la", "spam4.me",
    # Temp-mail family
    "tempmail.com", "tempmail.email", "tempmail.org",
    "temp-mail.org", "temp-mail.io", "temp-mail.net",
    "10minutemail.com", "10minutemail.net", "10minutemail.org",
    "20minutemail.com", "minutemail.com",
    "throwawaymail.com", "throwawayemailaddresses.com",
    # Yopmail family
    "yopmail.com", "yopmail.net", "yopmail.fr",
    # Maildrop / mintemail
    "maildrop.cc", "mintemail.com", "mytrashmail.com",
    # Moakt / spamgourmet / dispostable
    "moakt.com", "moakt.cc", "spamgourmet.com", "dispostable.com",
    # Trashmail
    "trashmail.com", "trashmail.de", "trashmail.io", "trashmail.net",
    # Email-fake / fakeinbox / mailcatch
    "email-fake.com", "fakeinbox.com", "mailcatch.com", "mailcat.cc",
    # Misc common throwaway services
    "emailondeck.com", "getnada.com", "nada.email", "getairmail.com",
    "inboxbear.com", "mailnesia.com", "dropmail.me",
    "tutanota.com",  # privacy-focused but often abused; remove if you find legit users
    "eyepaste.com", "discard.email", "fake-mail.net",
    "mohmal.com", "mailbox.in.ua", "owlymail.com",
    "burnermail.io", "clipmails.com", "instantmail.fr",
    "mailseasy.com", "tempinbox.com", "mvrht.com",
    # Wegwerf / etc.
    "wegwerfmail.de", "wegwerfemail.de", "wegwerf.email",
    "byom.de", "deadaddress.com", "mfsa.ru",
    # 10-min variants
    "10mail.org", "10mail.tk", "10minemail.com",
    "1secmail.com", "1secmail.net", "1secmail.org",
    # Common disposable specifically used for referral fraud (research)
    "fakemail.net", "fakemailgenerator.com", "trbvm.com",
    "anonbox.net", "anonymbox.com", "asiotraffic.com",
}


def is_disposable_email(email):
    """Return True if email's domain is in the disposable blocklist."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip()
    return domain in DISPOSABLE_DOMAINS


def client_ip():
    """Return the original client IP, even when behind Render's proxy.

    Render passes the real IP in X-Forwarded-For. The leftmost entry is the
    original client; the rest are intermediate proxies.
    """
    fwd = request.headers.get("X-Forwarded-For", "") if request else ""
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64] if request else ""


def client_user_agent():
    """Return the User-Agent header, truncated to fit the column."""
    if not request:
        return ""
    return (request.headers.get("User-Agent", "") or "")[:500]
