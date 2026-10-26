"""HTML sanitisation for CRM notes. CRM-215.

SANITISE ON THE WAY IN, NOT ON THE WAY OUT.

The UI runs dompurify when it renders, and that is a good second layer, but it
is not the layer that matters. A client that skips our UI — the API directly, an
import, a future integration — must not be able to *store* a script tag. Once
hostile HTML is in the database it is one careless consumer away from being
executed: an export, an email digest, a PDF render, an admin tool. Output
encoding protects one reader; input sanitisation protects all of them.

APPROACH: parse and rebuild, never regex.

Regex "sanitisers" fail on the cases that matter — nested quotes, malformed
tags, entity-encoded schemes, attributes split across newlines. This walks the
document with ``html.parser`` and emits a new one containing only what is
explicitly allowed. Anything unrecognised is dropped rather than passed
through, so the failure mode of an unanticipated input is *lost formatting*,
not *stored script*.

Allow-list per requirements §6.7: ``p br b i u strong em ul ol li a[href]``.

Stdlib only. No DB, no pydantic — so the hostile-input table is a unit test.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

MAX_LENGTH = 10000

ALLOWED_TAGS: Set[str] = {
    "p", "br", "b", "i", "u", "strong", "em", "ul", "ol", "li", "a",
}

# Only `a` keeps an attribute, and only this one.
ALLOWED_ATTRIBUTES: Dict[str, Set[str]] = {"a": {"href"}}

# Tags that never close themselves in HTML.
VOID_TAGS: Set[str] = {"br"}

# Content inside these is DROPPED, not escaped. Escaping `<script>alert(1)`
# leaves the payload visible as text, which is merely ugly; but for `style` and
# `script` the *content* is the attack, so it must not survive in any form.
DROP_CONTENT_TAGS: Set[str] = {"script", "style", "iframe", "object", "embed",
                               "template", "noscript", "svg", "math"}

SAFE_URL_SCHEMES: Set[str] = {"http", "https", "mailto", "tel"}

# Control characters and whitespace are stripped from a URL before its scheme is
# read: `java\tscript:` and `java\nscript:` are both treated as `javascript:` by
# browsers, and a naive startswith() check misses them.
_URL_NOISE = re.compile(r"[\s\x00-\x1f\x7f]+")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)


def safe_href(value: Optional[str]) -> Optional[str]:
    """The href to emit, or None to drop the attribute.

    Entities are decoded first, and repeatedly: `&amp;#106;avascript:` decodes
    to `&#106;avascript:` and then to `javascript:`. One pass is not enough.
    """
    if not value:
        return None

    decoded = str(value)
    for _ in range(3):
        nxt = html.unescape(decoded)
        if nxt == decoded:
            break
        decoded = nxt

    cleaned = _URL_NOISE.sub("", decoded)
    if not cleaned:
        return None

    match = _SCHEME.match(cleaned)
    if match:
        if match.group(1).lower() not in SAFE_URL_SCHEMES:
            return None
        return cleaned

    # No scheme: a relative or protocol-relative URL.
    if cleaned.startswith("//"):
        # `//evil.com` inherits the page's scheme. Harmless for navigation, but
        # it is not a relative link and the note author almost certainly meant
        # a domain — make it explicit rather than ambiguous.
        return f"https:{cleaned}"
    return cleaned


class _Sanitiser(HTMLParser):
    """Rebuilds a document from allowed pieces only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.open_tags: List[str] = []
        self.drop_depth = 0

    # ── tags ────────────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()

        if tag in DROP_CONTENT_TAGS:
            self.drop_depth += 1
            return
        if self.drop_depth:
            return
        if tag not in ALLOWED_TAGS:
            # Drop the TAG, keep its text — a note wrapped in <div> should not
            # lose its words.
            return

        if tag in VOID_TAGS:
            self.out.append(f"<{tag}/>")
            return

        rendered = self._attributes(tag, attrs)
        if tag == "a" and not rendered:
            # Its href was rejected, so this is not a link — emitting a bare
            # <a> would render as one and suggest the target survived. Drop the
            # tag, keep the words.
            return
        self.out.append(f"<{tag}{rendered}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.drop_depth or tag in DROP_CONTENT_TAGS:
            return
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag not in self.open_tags:
            return          # a stray close tag: ignore rather than emit
        # Close anything opened inside it that was never closed, so the output
        # is always balanced regardless of how malformed the input was.
        while self.open_tags:
            current = self.open_tags.pop()
            self.out.append(f"</{current}>")
            if current == tag:
                break

    def _attributes(self, tag: str, attrs) -> str:
        allowed = ALLOWED_ATTRIBUTES.get(tag, set())
        parts: List[str] = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in allowed:
                # Everything unlisted goes, which is what kills every on*
                # handler, style, and the data-* surface in one rule rather
                # than a blacklist that has to be kept up to date.
                continue
            if name == "href":
                href = safe_href(value)
                if not href:
                    continue
                parts.append(f' href="{html.escape(href, quote=True)}"')
        if tag == "a" and parts:
            # A sanitised note can still contain a link to anywhere. Opening it
            # in place would let a hostile href navigate the app away.
            parts.append(' target="_blank" rel="noopener noreferrer nofollow"')
        return "".join(parts)

    # ── content ─────────────────────────────────────────────────────────────
    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        self.out.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        # Comments are dropped entirely: conditional comments are an execution
        # vector in older engines, and a note has no use for them.
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def result(self) -> str:
        while self.open_tags:
            self.out.append(f"</{self.open_tags.pop()}>")
        return "".join(self.out)


def sanitise_html(value: Optional[str], *, max_length: int = MAX_LENGTH) -> str:
    """Return `value` containing only allowed markup.

    Never raises on hostile input — an unparseable document yields an empty
    string rather than an exception, because a note that fails to save is a
    worse outcome than a note that loses its formatting.
    """
    if not value:
        return ""
    text = str(value)[: max_length * 4]      # bound the parser's work first
    parser = _Sanitiser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # pylint: disable=broad-except
        return html.escape(str(value)[:max_length], quote=False)
    return parser.result()[:max_length]


def plain_text(value: Optional[str], *, limit: int = 200) -> str:
    """Tags stripped, entities resolved — for previews and activity titles.

    Used where HTML would be wrong or unsafe: an activity title, a search
    index, a notification. Never render this as HTML.
    """
    if not value:
        return ""
    text = re.sub(r"<[^>]*>", " ", str(value))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_empty(value: Optional[str]) -> bool:
    """True when a note has no actual content.

    `<p><br></p>` is what an empty rich-text editor submits, and it is not a
    note. Checking the raw string for truthiness would save it.
    """
    return not plain_text(value, limit=1)
