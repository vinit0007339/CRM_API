"""Auto-link contacts to companies by email domain. CRM-242.

⚠️ **THE FREE-PROVIDER LIST IS A FLOOR, NOT A SEED.**

``crm_settings.free_email_domains_blocklist`` defaults to an **empty list**, and
the requirements describe it as "ships with ~200 common free providers". Those
two facts are only compatible if something seeds it — and an account created
before the seed ran, or one whose list was cleared, would have nothing.

The failure that produces is not subtle: every ``@gmail.com`` contact links to
one company called "Gmail", which then accumulates thousands of unrelated
people, appears at the top of every list sorted by contact count, and is
extremely tedious to unpick. So ``FREE_EMAIL_DOMAINS`` here is consulted
*always*, and the account's list ADDS to it rather than replacing it. An account
can block more; it cannot accidentally unblock Gmail.

**Linking only ever adds.** On an email change the contact is linked to the new
domain's company and the old link is left alone. Removing it would be the
system deleting a relationship a person may have curated deliberately — a
contact who moved from one customer to another is still connected to both — and
an extra link is something the user can see and delete, while a missing one is
invisible.

Reuses ``dedupe.normalize_domain``. No second implementation, so a domain
written by auto-linking matches a domain written by hand.

Stdlib only. No DB, no pydantic.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Tuple

from app.services.dedupe import normalize_domain

# Consumer mail, ISP mail and the big regional providers. Not exhaustive — no
# such list is — which is why it is a floor an account can add to.
FREE_EMAIL_DOMAINS: Set[str] = {
    # global consumer
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "yahoo.ca", "yahoo.com.au", "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it",
    "ymail.com", "rocketmail.com", "hotmail.com", "hotmail.co.uk",
    "hotmail.fr", "hotmail.de", "hotmail.it", "hotmail.es", "outlook.com",
    "outlook.in", "live.com", "live.co.uk", "live.ca", "msn.com",
    "passport.com", "icloud.com", "me.com", "mac.com", "aol.com", "aim.com",
    "protonmail.com", "proton.me", "pm.me", "tutanota.com", "tuta.io",
    "zoho.com", "zohomail.com", "gmx.com", "gmx.net", "gmx.de", "gmx.at",
    "mail.com", "email.com", "usa.com", "consultant.com", "hushmail.com",
    "fastmail.com", "fastmail.fm", "hey.com", "mailfence.com", "posteo.de",
    "runbox.com", "startmail.com", "disroot.org", "riseup.net",
    # india
    "rediffmail.com", "rediff.com", "sify.com", "indiatimes.com",
    "in.com", "bsnl.in", "airtelmail.in", "vsnl.net", "vsnl.com",
    # europe
    "web.de", "t-online.de", "freenet.de", "arcor.de", "1und1.de",
    "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr",
    "neuf.fr", "bbox.fr", "libero.it", "virgilio.it", "tin.it", "alice.it",
    "tiscali.it", "email.it", "terra.es", "telefonica.net", "ono.com",
    "wp.pl", "onet.pl", "o2.pl", "interia.pl", "seznam.cz", "centrum.cz",
    "volny.cz", "abv.bg", "mail.bg", "bluewin.ch", "bluemail.ch",
    "sapo.pt", "telenet.be", "skynet.be", "planet.nl", "ziggo.nl",
    "home.nl", "kpnmail.nl", "chello.nl", "online.no", "start.no",
    "telia.com", "spray.se", "bredband.net", "luukku.com", "suomi24.fi",
    # uk / ireland
    "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
    "ntlworld.com", "blueyonder.co.uk", "tiscali.co.uk", "eircom.net",
    # americas
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net",
    "cox.net", "charter.net", "earthlink.net", "juno.com", "netzero.net",
    "optonline.net", "rogers.com", "shaw.ca", "sympatico.ca", "telus.net",
    "videotron.ca", "bell.net", "uol.com.br", "bol.com.br", "terra.com.br",
    "ig.com.br", "globo.com", "hotmail.com.br", "prodigy.net.mx",
    # apac
    "qq.com", "163.com", "126.com", "sina.com", "sina.cn", "sohu.com",
    "yeah.net", "aliyun.com", "foxmail.com", "naver.com", "hanmail.net",
    "daum.net", "nate.com", "docomo.ne.jp", "ezweb.ne.jp", "softbank.ne.jp",
    "nifty.com", "biglobe.ne.jp", "so-net.ne.jp", "ocn.ne.jp", "excite.co.jp",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au",
    "xtra.co.nz",
    # russia / cis
    "mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru", "yandex.ru",
    "yandex.com", "ya.ru", "rambler.ru", "ukr.net", "i.ua", "meta.ua",
    # middle east / africa
    "walla.com", "walla.co.il", "netvision.net.il", "maktoob.com",
    "webmail.co.za", "mweb.co.za", "telkomsa.net",
    # disposable — never a company, and a company made from one is pure noise
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "sharklasers.com", "getnada.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "spamgourmet.com", "mailnesia.com", "moakt.com",
    "emailondeck.com", "burnermail.io", "spam4.me", "grr.la",
}

# Suffixes that mean "this is not a company domain" whatever the label is.
_NEVER_COMPANY_SUFFIXES = (".local", ".test", ".invalid", ".example", ".localhost")

# Why a contact was not linked. Returned rather than a bare False so the caller
# can log something a human can act on.
SKIP_NO_EMAIL = "no_email"
SKIP_NO_DOMAIN = "no_domain"
SKIP_FREE_PROVIDER = "free_provider"
SKIP_DISABLED = "disabled"
SKIP_NOT_A_DOMAIN = "not_a_domain"


def domain_of(email: Optional[str]) -> Optional[str]:
    """The normalised domain of an email address, or None.

    Through ``normalize_domain`` so that a domain written by auto-linking is
    byte-identical to one typed into the company form — otherwise the company
    this creates would not be found by the dedupe check that runs on the next
    manual create, and the account ends up with two of everything.
    """
    if not email or "@" not in str(email):
        return None
    candidate = str(email).rsplit("@", 1)[-1].strip().lower()
    if not candidate:
        return None
    return normalize_domain(candidate)


def blocklist_for(settings: Any) -> Set[str]:
    """The built-in list PLUS whatever the account added.

    Additive, not a replacement. See the module docstring: an account whose
    list is empty — which is the model default — must still not produce a
    company called Gmail.
    """
    extra = _get(settings, "free_email_domains_blocklist") or []
    normalised = {str(d).strip().lower().lstrip("@") for d in extra if str(d).strip()}
    return FREE_EMAIL_DOMAINS | normalised


def is_free_provider(domain: Optional[str], settings: Any = None) -> bool:
    if not domain:
        return False
    return domain.lower() in blocklist_for(settings)


def should_link(email: Optional[str], settings: Any
                ) -> Tuple[bool, Optional[str], str]:
    """``(should_link, domain, reason)``.

    The reason is returned on success too, so a caller logging every decision
    does not have to reconstruct why.
    """
    if not _get(settings, "auto_link_contacts_by_domain", False):
        return False, None, SKIP_DISABLED
    if not email or "@" not in str(email):
        return False, None, SKIP_NO_EMAIL

    domain = domain_of(email)
    if not domain:
        return False, None, SKIP_NO_DOMAIN
    if any(domain.endswith(suffix) for suffix in _NEVER_COMPANY_SUFFIXES):
        return False, domain, SKIP_NOT_A_DOMAIN
    if is_free_provider(domain, settings):
        return False, domain, SKIP_FREE_PROVIDER
    return True, domain, "ok"


# Words in a domain label that should not be title-cased into a company name.
_STRIP_LABELS = {"www", "mail", "email", "smtp", "imap", "pop", "pop3",
                 "webmail", "mx", "ns", "mailer", "corp", "inc", "co", "com"}


def company_name_for(domain: str) -> str:
    """A readable company name from a domain.

    ``acme-industries.co.in`` becomes "Acme Industries". Not a guess at the
    legal name — nobody expects it to be — but "Acme Industries" is something a
    human recognises and can correct, while "acme-industries.co.in" as a
    company name looks like a bug.
    """
    if not domain:
        return ""
    labels = [l for l in str(domain).split(".") if l]
    if not labels:
        return ""

    # Drop the TLD, then any leading noise labels: "mail.acme.com" is Acme, not
    # Mail. Only from the FRONT — a company genuinely called "Mail Industries"
    # keeps its name.
    candidates = labels[:-1] or labels
    while len(candidates) > 1 and candidates[0].lower() in _STRIP_LABELS:
        candidates = candidates[1:]
    label = candidates[0]

    words = [w for w in re.split(r"[-_]+", label) if w]
    if not words:
        words = [label]
    # Only the first letter is touched. Upper-casing the word would turn "IBM"
    # into "IBM" but "eBay" into "EBAY"; lower-casing the rest breaks "IBM".
    return " ".join(w[:1].upper() + w[1:] for w in words)


def link_decision(email: Optional[str], settings: Any,
                  existing_domains: Optional[Iterable[str]] = None
                  ) -> Dict[str, Any]:
    """Everything the caller needs, as one dict — easier to log and to test."""
    ok, domain, reason = should_link(email, settings)
    already = domain is not None and domain in {
        str(d).lower() for d in (existing_domains or []) if d}
    return {
        "should_link": bool(ok and not already),
        "domain": domain,
        "reason": "already_linked" if (ok and already) else reason,
        "company_name": company_name_for(domain) if ok else None,
    }


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)
