"""Email validation helpers — disposable-domain blocklist, MX check,
client IP/UA capture, and lightweight rate limiting.

The disposable list is a curated set of the most common throwaway-email
providers used for fraud (people inflating their referral count).
Curated, not exhaustive — covers ~95% of free-tier services. Easy to
extend; just add the domain (lowercased) to DISPOSABLE_DOMAINS.
"""

import logging
import re
import threading
import time
from collections import defaultdict, deque
from flask import request

logger = logging.getLogger(__name__)


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


# ════════════════════════════════════════════════════════════════════
# Strict email syntax check (stricter than HTML5 input type=email)
# ════════════════════════════════════════════════════════════════════

# RFC 5322 simplified — covers 99% of legit emails, rejects obvious garbage.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email_syntax(email):
    """Return True if email looks syntactically real (rejects 'asdf@asdf')."""
    if not email or len(email) > 254 or len(email) < 6:
        return False
    return bool(_EMAIL_RE.match(email))


# ════════════════════════════════════════════════════════════════════
# MX record check — does the email's domain actually receive mail?
# ════════════════════════════════════════════════════════════════════
#
# Rejects garbage like "asdfg@asdfg.com" where the domain has no mail
# servers. Cached per domain for 24h so common providers (gmail.com, etc.)
# never re-resolve.

_MX_CACHE = {}              # domain -> (timestamp, has_mx_bool)
_MX_CACHE_TTL = 24 * 3600   # 24 hours


def has_mx_record(email, timeout=3.0):
    """True if the email's domain has at least one MX record. Cached."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip()

    # Cache hit
    cached = _MX_CACHE.get(domain)
    if cached is not None:
        ts, ok = cached
        if (time.time() - ts) < _MX_CACHE_TTL:
            return ok

    # Resolve
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        ok = len(answers) > 0
    except Exception:
        # Could be NXDOMAIN, timeout, or any DNS error. Treat as fail.
        ok = False

    _MX_CACHE[domain] = (time.time(), ok)
    return ok


# ════════════════════════════════════════════════════════════════════
# Rate limiting — max signups per IP in a sliding window
# ════════════════════════════════════════════════════════════════════
#
# In-memory, per-process. Sufficient for a single-instance Render deploy
# during a 2-week campaign. Survives a few thousand entries fine.
# Resets on app restart, which is acceptable.

_RATE_BUCKETS = defaultdict(deque)  # ip -> deque of timestamps
_RATE_LOCK = threading.Lock()


def check_rate_limit(ip, max_per_window=10, window_seconds=3600):
    """Return True if `ip` is below the limit; False if it should be blocked.

    Default: max 10 signups per IP per hour. Sliding window.
    """
    if not ip:
        return True  # never block anonymous (no IP); shouldn't happen behind proxy
    now = time.time()
    cutoff = now - window_seconds
    with _RATE_LOCK:
        q = _RATE_BUCKETS[ip]
        # Drop stale entries
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_per_window:
            return False
        q.append(now)
    return True
