"""File upload rules: type sniffing, size, and key safety. CRM-218.

THE CORE RULE: **an extension is a claim, not a fact.**

Validating uploads by filename extension is not validation. `invoice.pdf` can
be an HTML document with a script in it, and if anything ever serves it with a
guessed content type, that script runs in our origin. So the type is decided by
reading the first bytes of the file and the extension is only checked for
*consistency* with what was actually found.

WHAT IS DELIBERATELY NOT ALLOWED, and why:

* **SVG.** It is XML, it has no magic bytes to sniff, and it can carry
  `<script>`. An image format that executes is not an image format for our
  purposes. PNG/JPEG/GIF/WebP cover every real use.
* **HTML.** Same reason, more obviously.
* Anything executable, and anything we cannot identify.

WHAT SNIFFING CAN AND CANNOT DO. A `.docx` and a `.zip` are both PKZIP
archives — the bytes cannot tell them apart without unpacking. So sniffing
yields a *family*, and the rule is: the family must be allowed, and the
extension must be consistent with the family. That is honest about the limit
rather than pretending to more precision than the format allows.

Stdlib-only, no DB, no FastAPI — so all of it is unit-tested.
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from typing import Dict, List, Optional, Set, Tuple

MAX_BYTES = 10 * 1024 * 1024          # 10 MB, per requirements §11.4
MAX_FILENAME = 200

# Enough bytes to identify every signature below, including WebP's at offset 8.
SNIFF_BYTES = 64


class FileRuleError(ValueError):
    """A user-facing upload problem. Carries a sentence, not a code."""


# family -> (extensions, human label)
FAMILIES: Dict[str, Tuple[Set[str], str]] = {
    "pdf":   ({".pdf"}, "PDF"),
    "image": ({".png", ".jpg", ".jpeg", ".gif", ".webp"}, "image"),
    "zip":   ({".zip", ".docx", ".xlsx", ".pptx"}, "document or archive"),
    "ole":   ({".doc", ".xls", ".ppt", ".msg"}, "Office document"),
    "text":  ({".txt", ".csv", ".tsv", ".md", ".log"}, "text file"),
}

# The content type we will serve each family as. Never the client's claim —
# a client-supplied Content-Type is as untrustworthy as the extension.
CONTENT_TYPES: Dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".msg": "application/vnd.ms-outlook",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/plain",
    ".log": "text/plain",
}

ALLOWED_EXTENSIONS: Set[str] = {ext for exts, _ in FAMILIES.values() for ext in exts}

# extension -> family. Derived, never written out: FAMILIES is the one table,
# and a second hand-maintained copy is how an extension comes to be allowed by
# one rule and unknown to the other.
FAMILY_BY_EXTENSION: Dict[str, str] = {
    ext: family for family, (exts, _) in FAMILIES.items() for ext in exts
}

# Families a browser can render inline, usefully and safely. CRM-220 preview.
#
# ⚠️ `zip` AND `ole` ARE ABSENT BECAUSE A BROWSER CANNOT SHOW THEM. Serving a
# .docx inline means the browser downloads it anyway or shows binary soup, so
# "preview" would be a button that does nothing recognisable. The API refuses
# `disposition=inline` for them rather than minting a URL whose behaviour
# depends on the viewer's plugins.
#
# ⚠️ THE XSS QUESTION IS ALREADY ANSWERED UPSTREAM, which is the only reason
# `inline` is safe at all: SVG and HTML are refused at upload, by extension
# AND by magic bytes (see `_REFUSED_SIGNATURES` and the module docstring), so
# no stored object can execute script when rendered. If either is ever
# allowed, `inline` must be withdrawn for it in the same change — an inline
# SVG served from the API's own origin is a session-stealing XSS, and from a
# bucket on an app subdomain it is a convincing phishing page.
#
# `text` is included and served as `text/plain` with `nosniff`, so a .txt
# containing markup is shown as the markup it is rather than rendered.
PREVIEWABLE_FAMILIES: Set[str] = {"pdf", "image", "text"}


def family_for(filename: str) -> Optional[str]:
    """The family this filename's extension belongs to, or None."""
    _, ext = os.path.splitext(str(filename or "").lower())
    return FAMILY_BY_EXTENSION.get(ext)


def previewable(filename: str) -> bool:
    """May a browser be asked to render this inline?"""
    return family_for(filename) in PREVIEWABLE_FAMILIES


def preview_refusal(filename: str) -> str:
    """Why this file cannot be previewed, as a sentence for the user."""
    family = family_for(filename)
    label = FAMILIES.get(family, ((), "file"))[1] if family else "file"
    # "A Office document" — the labels are data, so the article has to be
    # chosen rather than hard-coded.
    article = "An" if label[:1].lower() in "aeiou" else "A"
    return (f"{article} {label} can't be previewed in the browser. "
            f"Download it to open it.")

# Extensions that are refused with a specific reason rather than a generic one,
# because the user will otherwise assume we simply forgot them.
EXPLAINED_REFUSALS: Dict[str, str] = {
    ".svg": "SVG files can contain scripts, so they can't be attached. "
            "Export it as a PNG instead.",
    ".html": "HTML files can contain scripts, so they can't be attached.",
    ".htm": "HTML files can contain scripts, so they can't be attached.",
    ".exe": "Executable files can't be attached.",
    ".sh": "Executable files can't be attached.",
    ".bat": "Executable files can't be attached.",
    ".js": "Script files can't be attached.",
}

# (offset, magic bytes, family). Order matters: WebP is a RIFF container, so it
# must be checked before any other RIFF use.
_SIGNATURES: List[Tuple[int, bytes, str]] = [
    (0, b"%PDF-", "pdf"),
    (0, b"\x89PNG\r\n\x1a\n", "image"),
    (0, b"\xff\xd8\xff", "image"),
    (0, b"GIF87a", "image"),
    (0, b"GIF89a", "image"),
    (8, b"WEBP", "image"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),          # empty archive
    (0, b"PK\x07\x08", "zip"),          # spanned archive
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
]

# Signatures that are never acceptable, whatever the extension claims.
_HOSTILE: List[Tuple[bytes, str]] = [
    (b"<?xml", "XML and SVG files can't be attached — they can carry scripts."),
    (b"<svg", "SVG files can contain scripts, so they can't be attached."),
    (b"<!DOCTYPE html", "HTML files can contain scripts, so they can't be attached."),
    (b"<html", "HTML files can contain scripts, so they can't be attached."),
    (b"\x7fELF", "Executable files can't be attached."),
    (b"MZ", "Executable files can't be attached."),
    (b"#!", "Script files can't be attached."),
]


def extension_of(filename: Optional[str]) -> str:
    return os.path.splitext(str(filename or ""))[1].lower()


def sniff_family(head: bytes) -> Optional[str]:
    """The file family the BYTES say this is, or None if unrecognised.

    Text files have no signature, so they are never identified here — the
    caller falls back to `looks_like_text` only for extensions in the text
    family, which keeps "unrecognised" meaning "rejected" everywhere else.
    """
    if not head:
        return None
    for offset, magic, family in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return family
    return None


def hostile_signature(head: bytes) -> Optional[str]:
    """A refusal message when the bytes are something we never accept."""
    if not head:
        return None
    stripped = head.lstrip()[:64].lower()
    for magic, message in _HOSTILE:
        if stripped.startswith(magic.lower()):
            return message
    return None


def looks_like_text(head: bytes) -> bool:
    """True when the bytes are plausibly plain text.

    A NUL byte means binary — no text format contains one.

    Otherwise the test is **"does it decode as UTF-8"**, not "is it mostly
    ASCII". An ASCII-ratio check rejects a perfectly ordinary CSV containing
    `₹` or `José`: those are multi-byte sequences, every byte is >= 0x80, and a
    short file of them scores near zero. In this product that is the common
    case, not the edge case.

    `head` is a prefix of the file, so it can end mid-character; a decode error
    in the last few bytes is expected and is not evidence of binary.
    """
    if not head:
        return True                      # an empty .txt is still a .txt
    if b"\x00" in head:
        return False

    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as ex:
        if ex.start >= len(head) - 4:
            # A truncated multi-byte character at the very end: `head` is a
            # prefix, so this is expected and says nothing about the file.
            text = head[: ex.start].decode("utf-8", errors="ignore")
        else:
            # Not UTF-8. Could still be a legacy single-byte encoding, so fall
            # back to counting bytes printable in one.
            printable = sum(1 for b in head
                            if 32 <= b < 127 or b in (9, 10, 13) or b >= 160)
            return printable / max(1, len(head)) > 0.85
    except Exception:  # pylint: disable=broad-except
        return False

    # Decoding cleanly is necessary but NOT sufficient: bytes 0x01-0x1f are
    # perfectly valid UTF-8, so a binary blob can decode without error. Text
    # is also mostly *printable* once decoded.
    if not text:
        return True
    control = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
    return control / len(text) < 0.15


def validate_upload(
    filename: Optional[str], head: bytes, size: Optional[int] = None
) -> Tuple[str, str]:
    """Validate one upload. Returns ``(extension, content_type)`` or raises.

    ``head`` is the first bytes of the file; ``size`` is its length when known.
    """
    ext = extension_of(filename)

    if not ext:
        raise FileRuleError("That file has no extension, so we can't tell what it is.")

    explained = EXPLAINED_REFUSALS.get(ext)
    if explained:
        raise FileRuleError(explained)

    if ext not in ALLOWED_EXTENSIONS:
        raise FileRuleError(
            f"{ext} files can't be attached. Allowed: "
            f"{', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )

    if size is not None and size > MAX_BYTES:
        raise FileRuleError(
            f"That file is {human_size(size)}. The limit is {human_size(MAX_BYTES)}."
        )
    if size == 0:
        raise FileRuleError("That file is empty.")

    # Bytes beat the extension, always.
    hostile = hostile_signature(head)
    if hostile:
        raise FileRuleError(hostile)

    family = sniff_family(head)
    expected = _family_for_extension(ext)

    if family is None:
        if expected == "text" and looks_like_text(head):
            return ext, CONTENT_TYPES[ext]
        raise FileRuleError(
            f"That file doesn't look like a {FAMILIES[expected][1]} inside, "
            f"whatever it's named. It hasn't been saved."
        )

    if family != expected:
        raise FileRuleError(
            f"That file is named {ext} but its contents are a "
            f"{FAMILIES[family][1]}. It hasn't been saved."
        )

    return ext, CONTENT_TYPES[ext]


def _family_for_extension(ext: str) -> str:
    for family, (exts, _) in FAMILIES.items():
        if ext in exts:
            return family
    raise FileRuleError(f"{ext} files can't be attached.")


# ── naming ───────────────────────────────────────────────────────────────────
_UNSAFE = re.compile(r"[^A-Za-z0-9._ \-()]+")


def safe_filename(filename: Optional[str]) -> str:
    """A display name with no path in it.

    ``../../etc/passwd`` and ``C:\\windows\\x.pdf`` both become a bare name.
    This is the name shown and used for download; it never becomes part of the
    storage key, so a traversal cannot reach the object store either way — but
    a name containing a separator would still be wrong in a Content-Disposition
    header, and confusing in a list.
    """
    raw = str(filename or "").strip()
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKD", raw)
    raw = raw.replace("\x00", "")
    cleaned = _UNSAFE.sub("_", raw).strip(". ")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        cleaned = "file"
    if len(cleaned) > MAX_FILENAME:
        stem, ext = os.path.splitext(cleaned)
        cleaned = stem[: MAX_FILENAME - len(ext)] + ext
    return cleaned


def storage_key(account_id, entity_type: str, ext: str) -> str:
    """The object key. Generated, never derived from the user's filename.

    A random key means a user-supplied name can never influence where the
    object lands, and means one attachment's key cannot be guessed from
    another's. The account prefix keeps a bucket listing legible and makes an
    account-scoped lifecycle rule possible later.
    """
    safe_ext = ext if ext in ALLOWED_EXTENSIONS else ""
    return f"crm/{account_id}/{entity_type}/{uuid.uuid4().hex}{safe_ext}"


def human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "0 KB"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, num_bytes // 1024)} KB"
