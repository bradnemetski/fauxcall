"""Keep credentials and personal data out of recordings, which are committed to git.

* :mod:`~fauxcall.privacy.redact` — credentials (auth headers, cookies, tokens, API keys) are
  **redacted**: replaced by ``[REDACTED]`` while the value keeps its shape (``Bearer [REDACTED]``).
* :mod:`~fauxcall.privacy.pii` — personal data (emails, names, phones, addresses …) is
  **detected** from value patterns and from field names in context.
* :mod:`~fauxcall.privacy.pseudonym` — detected personal data is **pseudonymized**: replaced by
  deterministic, format-preserving fakes derived from a keyed HMAC (the key lives in the gitignored
  ``.env``), so the same real value becomes the same fake in every recording, references between
  endpoints survive, and parity tests can translate real values with the same key.
"""

from fauxcall.privacy.pii import (
    PII_CATEGORIES,
    PiiCategory,
    as_pii_category,
    classify_value,
    detect_pii,
)
from fauxcall.privacy.pseudonym import Pseudonymizer
from fauxcall.privacy.redact import (
    MIN_SECRET_LENGTH,
    REDACTED,
    is_public_name,
    is_secret_name,
    redact_headers,
    redact_json,
    redact_query,
    redact_url,
    scrub_text,
)

__all__ = [
    "MIN_SECRET_LENGTH",
    "PII_CATEGORIES",
    "REDACTED",
    "PiiCategory",
    "Pseudonymizer",
    "as_pii_category",
    "classify_value",
    "detect_pii",
    "is_public_name",
    "is_secret_name",
    "redact_headers",
    "redact_json",
    "redact_query",
    "redact_url",
    "scrub_text",
]
