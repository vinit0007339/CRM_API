"""Private object storage for CRM attachments. CRM-218 / CRM-219.

WHY THIS IS NOT ``api/services/asset_storage.py``.

That module exists and does S3 uploads, and reusing it here would have been the
obvious move. It is also **wrong for this**: it writes objects intended to be
world-readable and returns a permanent public URL. That is right for a
marketing image in an email template and completely wrong for a customer's
signed contract, which would then sit at a URL that needs no authentication and
never expires.

So CRM attachments get their own path with the opposite defaults:

* objects are **private** — no ACL, no public URL, ever;
* reads go through a **short-lived signed URL**, minted per request, only after
  the caller's access to the *parent record* has been checked;
* the key is random and account-prefixed, so one attachment's location cannot
  be guessed from another's.

Same credentials and bucket as the rest of the app (``config/app.conf``), with
the same local-disk fallback for dev, because a developer without AWS keys
should still be able to work on the feature.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from app.config import logger as _logger, settings as _conf
# Long enough to click and download, short enough that a leaked URL in a
# browser history or a chat paste is not a lasting exposure.
SIGNED_URL_TTL_SECONDS = 300


def _settings():
    """This service's settings.

    ⚠️ THIS READ `core_app.settings` AND `core_app` DOES NOT EXIST HERE. The
    extraction replaced the monolith's `core_app` with `app/config.py`, on the
    grounds that all 85 references to it were `core_app.logger` — this file was
    the exception nobody counted, and it uses `.settings`. The result was a
    `NameError` on the FIRST file upload and on every CSV export, from a
    module that imports cleanly and passes a contract check.
    """
    return _conf


def s3_available() -> bool:
    s = _settings()
    return bool(s and getattr(s, "aws_access_key", None)
                and getattr(s, "aws_secret_key", None)
                and getattr(s, "s3_bucket", None))


def _client():
    import boto3           # imported lazily: dev without AWS never needs it

    s = _settings()
    return boto3.client(
        "s3",
        aws_access_key_id=s.aws_access_key,
        aws_secret_access_key=s.aws_secret_key,
        region_name=getattr(s, "s3_region", None) or None,
    )


def _local_path(key: str) -> str:
    s = _settings()
    # `local_file_dir` here, not the monolith's `uploads_dir`: this service
    # keeps CRM attachments in their own tree (`data/crm_files`) so a dev box
    # without AWS keys never mixes them with the platform's public assets.
    base = (getattr(s, "local_file_dir", None)
            or os.path.join(os.getcwd(), "data", "crm_files"))
    return os.path.join(base, "crm-private", key.replace("/", os.sep))


def put(key: str, body: bytes, content_type: str) -> Tuple[bool, Optional[str]]:
    """Store bytes privately. Returns ``(ok, error)``.

    **No ACL is set, deliberately.** On a modern bucket ACLs are disabled and
    objects are private by default; on an older one, omitting the ACL leaves
    the bucket default. Either way we never ask for public-read — the whole
    point of this module.
    """
    if s3_available():
        try:
            _client().put_object(
                Bucket=_settings().s3_bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                # Stops a browser ever rendering an attachment inline in our
                # origin, whatever the content type turns out to be.
                ContentDisposition="attachment",
            )
            return True, None
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False, "Could not store that file. Try again."

    try:
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return True, None
    except OSError as ex:
        _logger.exception(ex)
        return False, "Could not store that file. Try again."


def signed_url(key: str, filename: str, content_type: str, *,
               disposition: str = "attachment") -> Optional[str]:
    """A short-lived URL for the object, or None when one can't be minted.

    The caller MUST have already checked that this account owns the file — a
    signed URL is a bearer token for the object, and this function has no way
    to know who asked for it.

    ``disposition`` is ``attachment`` (save it) or ``inline`` (render it in
    the browser). It is NOT a free-text passthrough: anything else falls back
    to ``attachment``, because this value lands in a response header and the
    safe default is the one that cannot execute anything.

    ⚠️ ONLY ASK FOR `inline` ON A PREVIEWABLE FAMILY — see
    `file_rules.previewable`. The route enforces that; this function cannot,
    because it is handed a key and a name rather than a record.
    """
    safe_disposition = "inline" if disposition == "inline" else "attachment"
    if s3_available():
        try:
            return _client().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": _settings().s3_bucket,
                    "Key": key,
                    "ResponseContentType": content_type,
                    # The stored key is random, so the download needs the real
                    # name put back for the user. Quoted, because a filename
                    # with a comma or a quote would otherwise break the header.
                    #
                    # The filename is kept on `inline` too: it is what the
                    # browser offers if the user then saves what they are
                    # previewing, and dropping it would save the random key.
                    "ResponseContentDisposition":
                        f'{safe_disposition}; filename="{filename}"',
                },
                ExpiresIn=SIGNED_URL_TTL_SECONDS,
            )
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return None

    # Local dev: the API serves the bytes itself. There is no signing, which is
    # fine because there is also no bucket — but it must never look like the
    # production path, hence a distinct route rather than a fake signed URL.
    suffix = "?disposition=inline" if safe_disposition == "inline" else ""
    return f"/api/v1/crm/files/local/{key}{suffix}"


def delete(key: str) -> bool:
    """Remove one object. Idempotent — deleting a missing key is success."""
    if s3_available():
        try:
            _client().delete_object(Bucket=_settings().s3_bucket, Key=key)
            return True
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False

    try:
        path = _local_path(key)
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError as ex:
        _logger.exception(ex)
        return False


def read_local(key: str) -> Optional[bytes]:
    """Bytes for the dev fallback only. Never used when S3 is configured."""
    if s3_available():
        return None
    try:
        with open(_local_path(key), "rb") as handle:
            return handle.read()
    except OSError:
        return None
