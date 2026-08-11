"""Bounded recursive JSON / JSON-string secret scanning and masking.

C-122 supervision 06:58: a credential can be smuggled through multiple layers
of JSON encoding — ``{"outer": "{\\"Authorization\\": \\"Basic a\\"}"}`` — where
each ``json.dumps`` adds another layer of backslash escaping that a raw-byte
regex stops seeing after one level.  This module provides:

* :func:`iter_json_levels` — a BOUNDED recursive ``json.loads`` walker that
  yields the text at every decoded level (hard depth / node / size caps,
  parse failures surfaced so callers fail closed, never unbounded recursion
  or waiting).  Every dict/list/scalar node AND every object member key counts
  toward the node cap and is scanned one-by-one (C-122 supervision 00:06
  要求 A).
* :func:`bounded_json_mask` — a BOUNDED recursive masker that rebuilds a
  whole-JSON document with every nested JSON-string value masked, and applies
  a caller-supplied ``mask_level`` to every free-form level, with an optional
  ``normalize_patterns`` re-check on a NFKC + casefold + Cf/U+200B-stripped
  copy (:func:`mask_normalized_spans`, C-122 supervision 00:06 要求 B).
* :func:`mask_normalized_spans` / :func:`_normalize_for_scan` — the shared
  Unicode normalization helpers the sensitive key/value detection runs on.

All are shared by the canary producer (``benchmarks/live_canary_certified.py``),
the gate consumer and the final secret scan
(``scripts/run_product_done_gate.py``) so the whole chain has ONE consistent
redaction semantic.  This module is the single source of truth — do NOT
re-implement the walker in callers.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Flag, auto
from typing import Any

# Hard budgets for the recursive JSON walk.  ``_MAX_JSON_SCAN_DEPTH`` covers
# the mandated level 0-3 double/triple-encoding counter-examples plus structural
# margin; the node / size caps stop a maliciously huge or fan-out document from
# forcing unbounded work.  C-122 supervision 07:29 (gap 1): the SAME budgets
# also cover the JSON STRUCTURE itself — every dict/list/scalar node counts
# toward the node cap and the container nesting depth is capped, so a deep /
# fan-out / 20000-primitive object inside one decoded level fails closed too,
# never relying on Python's recursion limit.  C-122 supervision 00:06
# (要求 A): an object MEMBER KEY counts as a node as well (a 20000-member object
# fails on its keys alone), so a normal/decoded string value AND every
# object member/key both count toward the budget and are scanned one-by-one.
# Budget overflow raises ``RecursiveJsonBudgetError`` and every caller fails
# closed.
_MAX_JSON_SCAN_DEPTH = 8
_MAX_JSON_SCAN_NODES = 10_000
_MAX_JSON_SCAN_CHARS = 2_000_000


class RecursiveJsonBudgetError(Exception):
    """A nested-JSON scan/mask exceeded a hard budget — callers fail closed."""


def looks_like_json(text: str) -> bool:
    """True when ``text`` starts with a JSON structural character.

    The first CHARACTER must be ``{`` / ``[`` / ``"`` — a membership check, not a
    substring check, so an empty string (which is a substring of everything) is
    never mistaken for a JSON start.
    """
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in "{[\""


class DuplicateJsonKeyError(ValueError):
    """A JSON object repeated a member key — every parser fails closed on it.

    Standard ``json.loads`` keeps the LAST occurrence of a duplicate key and
    silently discards the earlier value, so a published artifact could smuggle a
    FOREIGN 64-hex under a whitelisted field name and have the first (candidate)
    value discarded while the second (normal) value is kept — both currently
    pass the gate (C-122 supervision 09:59 Block 2).  Every object parsed by the
    redaction chain must fail BEFORE any member enters an object the gate
    trusts, so the whole chain loads through :func:`json_loads_no_dupes`.
    """


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicated member key."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object member: {key!r}")
        result[key] = value
    return result


def json_loads_no_dupes(text: str) -> Any:
    """``json.loads`` that fails closed on duplicate object member keys."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)


# ============================================================================
# Typed sensitive-shape pattern registry (C-122 supervision 08:30+08:31 补充 C)
#
# ONE registry of every credential SHAPE the redaction chain knows about.  The
# producer (``benchmarks/live_canary_certified.py``), the gate consumer and the
# final secret scan (``scripts/run_product_done_gate.py``) all derive their
# pattern sets from :data:`SHAPE_PATTERN_REGISTRY` — the single source of
# truth, so a flag change (e.g. AKIA case-insensitivity) lands in all three
# layers at once and a shape is never defined twice with drift.
# ============================================================================


class PatternScope(Flag):
    """Where a shape pattern applies.  Flags compose.

    - ``PRODUCER_MASK`` — the canary producer's ``_desensitize`` level mask.
    - ``CONSUMER_MASK`` — the gate consumer's ``_canary_diag_mask_level``.
    - ``NORMALIZED`` — ALSO re-searched on the NFKC + casefold + Cf/U+200B-
      dropped copy (00:06 要求 B), so a full-width / zero-width-obfuscated
      span is caught even though the ASCII regex stops seeing it on raw text.
    - ``FINAL_TEXT`` — the final secret scan on a WHOLE free-form diagnostic
      text level (``.failure.json``), raw and normalized.
    - ``FINAL_VALUE`` — the final secret scan on an individual DECODED string
      value (every string value in the shared bounded walker, on BOTH the
      committed-evidence and the failure-artifact path — 补充 A).
    """

    PRODUCER_MASK = auto()
    CONSUMER_MASK = auto()
    NORMALIZED = auto()
    FINAL_TEXT = auto()
    FINAL_VALUE = auto()


@dataclass(frozen=True)
class SensitiveShapePattern:
    """One credential shape in the single registry.

    ``pattern`` is the compiled regex (flags baked in — e.g. AKIA is
    case-insensitive so the casefolded normalized copy still matches),
    ``kind`` is the human error label, and ``scopes`` pins exactly where the
    shape is applied (producer / consumer mask, normalized re-check, final
    whole-text scan, final decoded-value scan).
    """

    name: str
    pattern: re.Pattern[str]
    kind: str
    scopes: PatternScope


_SHAPE_PATTERN_WHOLE_HEADER_RE = re.compile(
    r"(?i)\b(?:proxy[-_ ]authorization|set[-_ ]cookie|x-api-key|api[-_ ]key|"
    r"authorization|cookie)\b\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    r"(?:\\*[\"']?|[\"'])?[^\r\n]+"
)
_SHAPE_PATTERN_CANARY_URL_RE = re.compile(
    r"(?i)https?://[^\s\"'<>)\[\]{}]+"
)
_SHAPE_PATTERN_TOKEN_RUN_RE = re.compile(r"[A-Za-z0-9_\-=]{32,}")
# ``(?i)`` (C-122 supervision 08:30+08:31 缺口②): the NORMALIZED copy is
# casefolded, so a full-width ``\uff21\uff4b\uff29\uff21`` composes to lowercase
# ``akia`` — the pattern must be case-insensitive to match it.  This one flag
# change makes the full-width AKIA counter-example fail closed in all three
# layers at once.
_SHAPE_PATTERN_AKIA_RE = re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b")
_SHAPE_PATTERN_PREFIX_TOKEN_RE = re.compile(
    r"(?i)\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xoxb-|xoxp-|xoxa-|"
    r"sk-|rk-)[A-Za-z0-9_\-]{6,}"
)
_SHAPE_PATTERN_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.]{4,}")
_SHAPE_PATTERN_DOTTED_TOKEN_RE = re.compile(
    r"\b[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{3,}\b"
)
# Credential FIELD NAMES — the full set a diagnostic / committed artifact must
# never carry.  Two shapes derive from this single alternation:
#
# * ``credential_field`` — the KEY-VALUE form (``session_token=abc``,
#   ``"Session_token":"abc"``, ``Session_token: abc``), masked WHOLE (name +
#   value together) by the producer/consumer diagnostic sanitizers and rejected
#   by the final scan on BOTH the committed-evidence and failure-artifact paths
#   (C-122 supervision 09:00: an ASCII / full-width ``Session_token=abc`` was
#   passing all three layers because ``session_token`` was never in the shape
#   set and the free-text ``=``-form had no field-position guard).
# * ``CREDENTIAL_FIELD_NAME_PATTERN`` — the KEY-NAME form used by the gate's
#   committed-evidence key rejector and the structured-JSON key mask.
#
# ``bridge_token_present`` / ``candidate_set_sha256`` / ``build_sha256`` are
# deliberately NOT matched: the token flags and digest fields are legitimate
# committed contract fields, so a name is only flagged when it is the FULL key
# or a ``_``/``-``-bounded final component — never a substring of a flag/digest
# name.  Bare ``token`` / ``cookie`` / ``secret`` / ``browser_token`` are
# separate (``BARE_CREDENTIAL_FIELD_NAMES`` owns the exact-name key check; the
# key-VALUE shape includes them as whole keys only, never as a name substring,
# so ``bridge_token_present`` stays a legitimate flag).
_CREDENTIAL_FIELD_NAME_ALT = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"session[_-]?token|api[_-]?key|apikey|client[_-]?secret|"
    r"secret[_-]?key|private[_-]?key|authorization|set[_-]?cookie|"
    r"passw(?:ord|d)|session[_-]?id|account[_-]?secret|credentials?|"
    r"access[_-]?key|session[_-]?key|bearer"
)
BARE_CREDENTIAL_FIELD_NAMES = frozenset(
    {"token", "cookie", "secret", "browser_token"}
)
# Key-NAME form: the credential word must be the full key or a ``_``/``-``-
# bounded component — ``bridge_token_present`` / ``candidate_set_sha256`` /
# ``build_sha256`` (token flags and digest fields) never match.
CREDENTIAL_FIELD_NAME_PATTERN = re.compile(
    r"(?i)(?:^|[_-])(" + _CREDENTIAL_FIELD_NAME_ALT + r")(?:[_-]?|$)"
)
# The credential-field KEY-VALUE shape splits the field names by how strongly
# they signal a credential (C-122 supervision 09:28 gap 2 + 09:59 Block 3/4):
#
# * ``_CREDENTIAL_FIELD_STRONG_NAME_ALT`` — names that are NEVER English prose
#   (``session_token``, ``api_key``, ``client_secret``, bare ``token`` /
#   ``secret``, and — 09:59 Block 3 — ``password`` / ``passwd`` / ``access_key``
#   / ``session_key``, which no longer share a branch with ``authorization``).
#   Their VALUE is QUOTE- AND BRACKET-AWARE and CHARSET-UNRESTRICTED (R18
#   Block 3): the first value character may be ANY character except a newline;
#   a quoted value runs to the matching UNESCAPED closing quote, a bracket
#   value (``token=[1,2]``) to the matching closing bracket, and an unquoted
#   value to a real field boundary (a newline / the end of the diagnostic, or a
#   space that BEGINS another field assignment).  So ``Session_token=a`` (1
#   char), ``Password=a/ab/!``, ``Session_token=abc@def``,
#   ``Session_token="abc;def"``, ``Session_token=abc,def``,
#   ``Session_token=abc\def`` and ``token=[1,2]`` are all masked WHOLE — never
#   relying on a 3-char token-run minimum, never limited to an ASCII charset,
#   and never leaving a ``;def"`` / ``,2]`` residue.
# * ``_CREDENTIAL_FIELD_WEAK_NAME_ALT`` — names that are ALSO ordinary English
#   words (``authorization``, ``cookie``, ``bearer``).  Their value must be an
#   actual token-character payload: a SINGLE token run of ``{3,}`` token
#   characters NOT followed by more text (``(?![ \\t])``), so the canary's own
#   scope-detail PROSE — ``pending user authorization: not all certified…``,
#   ``authorization: full real E2E…``, ``authorization: no connected
#   Companion…`` — is never flagged as a credential assignment, while a real
#   token payload (``authorization: abc``, ``Cookie:a=b``) still is.
#
# Both branches keep the ``(?:^|[^A-Za-z0-9_])`` leading guard (a field
# position — line start / quote / comma / bracket — separate from ordinary
# prose) and the bare names are only whole keys (``session_token`` matches
# ``session[_-]?token`` first; the bare ``token`` never matches the ``token``
# inside ``bridge_token_present`` because a preceding ``_`` is not a
# field-position guard).  The gate's own redaction marker ``secret=[REDACTED]``
# is the ONE EXACT exemption (R18 Block 2): only a field value that — after
# removing surrounding quotes — is precisely ``[REDACTED]`` and is immediately
# followed by a real field boundary / end (``secret=[REDACTED]``,
# ``secret="[REDACTED]"``, ``secret=[REDACTED] next=1``) is left untouched.
# Any trailing character — ``[REDACTED]actual``, ``[REDACTED] actual``, the
# ``[REDACTED];def"`` residue of a quote-split value — fails the exemption and
# re-masks / re-rejects the WHOLE segment.
_CREDENTIAL_FIELD_STRONG_NAME_ALT = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"session[_-]?token|api[_-]?key|apikey|client[_-]?secret|"
    r"secret[_-]?key|private[_-]?key|set[_-]?cookie|session[_-]?id|"
    r"account[_-]?secret|credentials?|browser_token|token|secret|"
    r"access[_-]?key|session[_-]?key|passw(?:ord|d)"
)
_CREDENTIAL_FIELD_WEAK_NAME_ALT = r"authorization|cookie|bearer"
_CREDENTIAL_FIELD_VALUE_END = (
    # A real end of a credential value, matched as a NON-CONSUMING lookahead so
    # the boundary text stays in the diagnostic and the NEXT field assignment
    # can be masked on its own (R19 Block 16: ``token=[1,2];password=…`` must
    # NOT swallow ``;password=`` into the bracket value — the second field's
    # VALUE would otherwise be orphaned as plaintext).  The value ``[REDACTED]``
    # inside a JSON string (``\"detail\":\"secret=[REDACTED]\",...``) is parsed
    # with the value's opening quote as the field-position guard and the value
    # ending at the CLOSING quote, so a string-closing quote followed by JSON
    # structure (``\"`` then ``,`` / ``}`` / ``]`` / end) is a boundary.  The
    # other boundary forms: end-of-text / a newline, a JSON structural
    # punctuation (``}`` / ``]`` / ``,``) or a ``;`` / ``,`` field separator —
    # optionally followed by the next (possibly quoted) field assignment — or a
    # space that BEGINS another field assignment.  A bare quote is NOT a
    # boundary on its own: ``secret=\"[REDACTED]\"actual`` and
    # ``secret=[REDACTED]actual`` must not be treated as the marker followed by
    # a clean boundary (R18 Block 2).
    r"(?=(?:$|[\r\n]"
    r"|[\"'][ \t]*(?:$|[\r\n]|[}\],;][ \t]*(?:$|[\r\n]|[\"']?[A-Za-z0-9_.-]+[\"']*[ \t]*[:=]))"
    r"|[}\],;][ \t]*(?:$|[\r\n]|[\"']?[A-Za-z0-9_.-]+[\"']*[ \t]*[:=])"
    r"|[ \t](?=[A-Za-z0-9_-]+[ \t]*[\"']*[ \t]*[:=]))"
    r")"
)
_CREDENTIAL_FIELD_STRONG_VALUE = (
    # C-122 supervision 09:59 (R18 Block 3): the value is CHARSET-UNRESTRICTED
    # AND quote/bracket-AWARE.  ``@``, ``!``, ``/``, CJK and emoji are real
    # value characters (``Session_token=!/@/秘密/\U0001f511``,
    # ``Password=a/ab/!`` and ``Session_token=abc@def`` must mask WHOLE, never
    # leave ``@def`` residue); a quoted value runs to the matching UNESCAPED
    # closing quote (``Session_token="abc;def"`` — never a ``;def"`` residue),
    # a comma / backslash value (``Session_token=abc,def``,
    # ``Session_token=abc\def``) and a bracket list (``token=[1,2]``) mask
    # whole, and an unquoted value runs to a real field boundary — a newline /
    # the end of the diagnostic, or a space that BEGINS another field
    # assignment (``name`` followed by ``:`` / ``=``), so ``token=a
    # Session_token: b`` still stops the first value at the next field instead
    # of swallowing its name (C-122 supervision 09:28 gap B) while a
    # space-separated value (``Session_token: abc def``) is covered WHOLE.
    #
    # The leading lookahead is the EXACT safe-marker exemption (R18 Block 2,
    # tightened in R20 Block 18/19): only a field value that — after removing
    # surrounding quotes AND the JSON backslash-escaping of those quotes — is
    # precisely ``[REDACTED]`` and is immediately followed by a real field
    # boundary / end (``secret=[REDACTED]``, ``secret="[REDACTED]"``,
    # ``secret=\"[REDACTED]\"`` inside a JSON string, ``secret=[REDACTED]
    # next=1``) is left untouched.  The exemption is CASE-SENSITIVE — the
    # credential-field pattern is compiled without ``(?i)`` (names keep their
    # own ``(?i:...)`` scope), so ``[Redacted]`` / ``[redacted]`` /
    # ``\"[Redacted]\"`` are impersonations that FAIL the exemption and are
    # masked / rejected whole.  Any trailing character
    # (``secret=[REDACTED]actual``, ``secret=[REDACTED] actual``,
    # ``secret=\"[REDACTED]\" actual``) fails the exemption and the WHOLE value
    # is masked again.
    r"(?!"
    r"(?:(?:\\*[\"'])\[REDACTED\](?:\\*[\"'])|\[REDACTED\])"
    + _CREDENTIAL_FIELD_VALUE_END
    + r")"
    + r"(?:"
    + r"[\"'](?:\\[\s\S]|[^\\\"'])*[\"']"
    + _CREDENTIAL_FIELD_VALUE_END
    + r"|\[(?:[^\[\]\"']|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')*\]"
    + _CREDENTIAL_FIELD_VALUE_END
    + r"|[^\r\n](?:[^\r\n]|[ \t](?![A-Za-z0-9_-]+[ \t]*[\"']*[ \t]*[:=]))*"
    + r")"
)
_SHAPE_PATTERN_CREDENTIAL_FIELD_RE = re.compile(
    # R20 Block 18: the field-NAME alternations keep their own ``(?i:...)``
    # scope (``Session_token`` / ``passworD`` still match), but the compile is
    # NO LONGER globally case-insensitive — the marker exemption and the value
    # branches are case-sensitive, so a mixed/lowercase ``[Redacted]`` /
    # ``[redacted]`` value fails the exact ``[REDACTED]`` exemption and is
    # masked / rejected whole (the root cause of the Block-18 leak was the
    # ``(?i)`` flag making the exemption case-blind).
    r"(?:"
    r"(?:^|[^A-Za-z0-9_])((?i:"
    + _CREDENTIAL_FIELD_STRONG_NAME_ALT
    + r"))(?:[_-]?|$)\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    # R18 Block 3: NO value pre-pattern quote-consumption on the STRONG branch —
    # the value regex itself is quote-aware (a quoted value runs to the closing
    # quote, a bracket value to the closing bracket), so an opening quote must
    # reach the value so the closing quote is never left as residue.
    + _CREDENTIAL_FIELD_STRONG_VALUE
    + r"|"
    r"(?:^|[^A-Za-z0-9_])((?i:"
    + _CREDENTIAL_FIELD_WEAK_NAME_ALT
    + r"))(?:[_-]?|$)\s*(?:\\*[\"']?|[\"'])?\s*[:=]\s*"
    r"(?:\\*[\"']?|[\"'])?(?>[A-Za-z0-9+/=_\-.]{3,})(?![ \t])"
    r")"
)
# C-122 supervision 09:59 (R18 Block 1): the ``Authorization`` /
# ``Proxy-Authorization`` Basic-SCHEME field.  The weak credential-field branch
# requires a SINGLE token payload (``authorization: YWJjZA==``), so the
# space-separated Basic scheme (``Authorization: Basic YWJjZA==``,
# ``proxy-authorization: Basic YWJjZA==``) was invisible to the FINAL_VALUE
# decoded-value scan even though the whole-header shape masks it in the
# producer/consumer and failure-diagnostic text.  This shape is the FINAL-scan
# independent backstop: the committed-evidence decoded-value scan recognizes
# the COMPLETE Basic field (scheme + base64 payload) as a leak while the three
# real ``pending user authorization: …`` prose positives (no ``Basic`` scheme)
# stay allowed.  Scoped to the FINAL scans only — the whole-header shape
# already owns producer/consumer masking.
#
# R20 Block 20b/c: the payload is a base64 token validated by
# :func:`_is_valid_basic_payload` — length 4-aligned + correct padding (the
# regex's ``{4,}={0,2}`` shape) AND the decoded bytes must be valid UTF-8, the
# real ``base64(user:pass)`` form.  This separates a genuine payload from prose:
# ``Basic YWJjZA==`` / ``Basic b3BlbiBzZXNhbWU=`` / ``Basic YQ==`` are leaks,
# ``upstream Authorization: Basic YWJjZA== extra`` (mid-text Basic header, R20
# Block 20b) is a leak because the payload token ends at the space and the
# trailing prose is outside the match, while ``authorization: Basic is
# required`` (``is`` is under the 4-char floor) and ``authorization: Basic
# auth/setting`` (4-aligned but decodes to non-UTF-8 bytes) stay positive.
_SHAPE_PATTERN_BASIC_AUTH_RE = re.compile(
    r"(?i)\b(?:proxy[-_ ]authorization|authorization)\b\s*"
    r"(?:\\*[\"']?|[\"'])?\s*[:=]\s*(?:\\*[\"']?|[\"'])?"
    r"Basic[ \t]+(?P<payload>[A-Za-z0-9+/]{4,}={0,2})"
)


def _is_valid_basic_payload(payload: str) -> bool:
    """True when ``payload`` is a REAL Basic-auth base64 body: standard base64
    alphabet, length 4-aligned with correct padding (``b64decode(validate=True)``
    enforces both), and the decoded bytes are valid UTF-8 — a real
    ``base64(user:pass)``.  Prose like ``auth/setting`` (4-aligned but decodes
    to binary) or ``is required`` (not 4-aligned) fails the check."""
    try:
        base64.b64decode(payload, validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    return True


class _BasicAuthScan:
    """The FINAL-scan ``basic_auth`` backstop as a drop-in ``.search``-able
    object: the regex bounds the complete ``Authorization`` /
    ``Proxy-Authorization`` ``Basic`` field and captures the payload token, and
    :func:`_is_valid_basic_payload` decides whether it is a real base64
    credential (R20 Block 20c) — the scan loops stay ``pattern.search(text)``
    unchanged.

    R21 Block 22: ``.search`` now iterates EVERY candidate — a leading prose
    ``Basic auth/setting`` (4-aligned but non-UTF-8, so the regex's ``{4,}``
    floor matches it but the validity check rejects it) must never hide a real
    ``Basic YWJjZA==`` later in the same text.
    """

    def search(self, text: str) -> re.Match[str] | None:
        for match in _SHAPE_PATTERN_BASIC_AUTH_RE.finditer(text):
            if _is_valid_basic_payload(match.group("payload")):
                return match
        return None


_BASIC_AUTH_SCAN = _BasicAuthScan()

# A ``Basic`` scheme token used to decide whether a whole-header value is a REAL
# credential or prose.  The token charset+padding mirrors the ``basic_auth``
# shape so the whole-header backstop and the ``basic_auth`` shape agree
# (C-122 round-20 Block 20c).
_BASIC_VALUE_TOKEN_RE = re.compile(r"(?i)\bBasic[ \t]+(?P<payload>[A-Za-z0-9+/]{1,}={0,2})")

# ``Basic`` scheme payload spans preserved VERBATIM through normalization (R20
# Block 20c): base64 is case-sensitive, so a casefolded copy would turn a real
# ``Basic YWJjZA==`` payload into ``ywjjza==`` and the whole-header /
# Authorization prose-exemption — which separates real payloads from prose by
# base64 validity — would then misclassify the real credential as prose and let
# it through.  The payload TOKEN of every ``Basic`` value is preserved in its
# original case; prose (``Basic auth/setting``) is preserved too and the
# prose-exemption still decides by validity, while an unrelated uppercase 64-hex
# digest (``{"sha256": "AAAA..."}``) is NOT a Basic payload and stays casefolded
# so the 64-hex trust check keeps rejecting it.
# (The span used is :func:`_BASIC_VALUE_TOKEN_RE`'s ``payload`` group.)


# Basic-prose phrase vocabulary used ONLY by the Basic-header prose-exemption
# (``_is_english_prose_phrase``): a non-base64 ``Basic`` payload plus any
# trailing text is non-credential prose ONLY when every word is in this
# controlled vocabulary — ``is required`` / ``authentication is required`` are
# prose, ``ab extra`` / ``abc extra`` are not.  R26 Block 41 removed this
# vocabulary from the BARE-IDENTIFIER determination — business identifiers are
# now bound to the closed, auditable ``_BUSINESS_IDENTIFIER_BASES``
# schema/field-path registry below, never to a word list — so this set exists
# solely to classify Basic-header prose.  ``authentication`` is added (R26
# Block 42) so ``Basic authentication is required`` is recognised as the normal
# English sentence the supervision contract mandates stay accepted.
_CREDENTIAL_VALUE_WORDS = frozenset(
    {
        # function words
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "am", "of", "for", "to", "in", "on", "at", "by", "with", "from",
        "up", "down", "off", "over", "under", "into", "onto", "about",
        "and", "or", "but", "nor", "so", "yet", "as", "than", "if", "then",
        "else", "when", "while", "not", "no", "yes", "also", "only",
        "this", "that", "these", "those", "it", "its", "there", "here",
        "we", "you", "they", "he", "she", "them", "us", "our", "your",
        "their", "his", "her", "my", "me", "do", "does", "did", "done",
        "have", "has", "had", "will", "would", "shall", "should", "can",
        "could", "may", "might", "must", "please", "need", "needs",
        # common adjectives / adverbs / verbs / nouns
        "all", "any", "some", "many", "much", "few", "more", "most", "less",
        "least", "very", "really", "quite", "too", "just", "even",
        "still", "already", "always", "never", "often", "sometimes",
        "required", "optional", "default", "empty", "full",
        "valid", "invalid", "new", "old", "first", "last", "next",
        "left", "right", "top", "bottom", "high", "low", "big", "small",
        "large", "long", "short", "fast", "slow", "early", "late",
        "good", "bad", "true", "false", "ok", "okay", "none",
        "null", "zero", "one", "two", "three", "four", "five", "six",
        "seven", "eight", "nine", "ten", "key", "value", "pair", "name",
        "user", "email", "phone", "date", "time", "day",
        "month", "year", "hour", "minute", "second", "week", "start", "end",
        "begin", "finish", "open", "close", "create", "update", "delete",
        "insert", "remove", "add", "set", "get", "make", "take", "put",
        "send", "receive", "give", "show", "hide", "read", "write", "run",
        "stop", "check", "verify", "test", "build", "deploy",
        "plan", "planner", "trip", "flight", "hotel",
        "hotels", "amenity", "booking", "reference",
        "route", "itinerary", "destination", "city", "country",
        "rating", "score", "review", "reviews", "availability", "capacity",
        "currency",
        "duration", "distance", "discount", "tax", "fee", "deposit",
        "cancellation", "refund", "fare", "seat", "cabin",
        "breakfast", "luggage", "baggage", "transfer", "rental", "car",
        "attraction", "ticket", "visa", "insurance", "weather",
        "id", "ids", "num",
        "budget", "price", "cost", "amount", "total", "sum", "count",
        "number", "quantity", "option", "options",
        "choice", "select",
        "selected", "choose", "pick", "prefer", "preference", "setting",
        "settings", "config", "configuration", "auth", "authentication",
        "mode", "model", "type",
        "version", "status", "state", "level", "rank", "order", "sort",
        "group", "class", "kind", "list", "array", "map",
        "object", "field", "fields", "label", "title", "header", "body",
        "text", "line", "row", "column", "cell", "page", "screen", "view",
        "panel", "window", "menu", "button", "input", "output", "result",
        "results", "error", "message", "info", "note", "notice",
        "warning", "danger", "success", "fail", "failure", "pass", "passes",
        "attempt", "retry", "try", "action", "task", "job", "work", "item",
        "items", "entry", "entries", "record", "records", "file", "folder",
        "path", "dir", "root", "node", "branch", "leaf", "tree",
        "graph", "edge", "vertex", "layer", "engine", "module", "component",
        "service", "api", "app", "application", "client", "server", "host",
        "port", "address", "url", "link", "uri", "endpoint",
        "request", "response", "query", "session", "context", "scope",
        "region", "zone", "area", "part", "parts", "piece", "whole", "half",
        "quarter", "section",
        "segment", "block", "chunk", "unit", "package", "bundle",
        "release", "tag", "commit", "push", "pull",
        "merge", "rebase", "clone", "fetch", "sync", "upload", "download",
        "install", "upgrade", "downgrade", "patch",
        "fix", "bug", "issue", "story", "epic",
        "feature", "spec", "docs", "document", "doc", "guide",
        "manual", "readme", "help", "faq", "tutorial", "example",
        "sample", "demo", "preview", "draft", "final", "ready", "pending",
        "blocked", "cancel", "cancelled", "abort",
        "pause", "resume", "continue", "halt", "exit", "quit",
        "back", "forward", "previous", "prev", "current", "live",
        "prod", "production", "dev", "development", "stage", "staging",
        "testing", "integration", "e2e", "regression",
        "smoke", "sanity", "health", "metric", "metrics",
        "log", "logs", "trace", "span", "samples", "refresh", "reload",
        "renew", "token", "tokens", "exchange",
        "grant", "audience", "issuer", "subject",
        "provider", "providers", "vendor", "supplier", "partner", "tenant",
        "users", "member", "admin", "owner", "guest", "viewer",
        "editor", "agent", "agents", "bot", "worker", "runner",
        "scheduler", "queue", "topic", "subscription", "channel",
        "stream", "event", "events", "pub", "sub", "callback",
        "webhook", "hook", "listener", "handler", "processor",
        "pipeline", "step", "phase", "wave", "round", "cycle",
        "iteration", "runs", "execution", "execute",
    }
)


def _is_english_prose_phrase(text: str) -> bool:
    """True when ``text`` is 2+ words and every word is common English — the
    structure that separates ``is required`` (prose) from ``ab extra`` /
    ``abc extra`` (credential-shaped) and ``ab`` / ``abc`` (lone
    placeholder).  A one-word text is never prose, so a bare ``Basic is``
    fails closed."""
    words = re.findall(r"[A-Za-z]+", text)
    return len(words) >= 2 and all(
        w.lower() in _CREDENTIAL_VALUE_WORDS for w in words
    )


# Field-name occurrences used by the header-prose check (R21 Block 22/23/27):
# a prose-Basic exemption applies ONLY when the matched value is a SINGLE
# header field whose Basic payload is non-credential prose — a second sensitive
# field name inside the same value (``authorization: Basic is required; Cookie:
# sid=abc``) is a real credential the prose exemption must never swallow.
_HEADER_FIELD_NAME_RE = re.compile(
    r"(?i)\b(?:proxy-authorization|set-cookie|x-api-key|api[-_ ]?key|"
    r"authorization|cookie)\b"
)


def _is_whole_header_prose(value: str) -> bool:
    """True when ``value`` is ONE header field whose complete value is Basic
    prose (``authorization: Basic is required``) rather than a real credential.

    R21 Block 22: the FINAL-scan Authorization/Cookie backstops used
    ``.search`` and returned only the FIRST header match — a leading prose
    Basic value (``authorization: Basic auth/setting``) made the whole region
    prose and masked a REAL ``Authorization: Basic YWJjZA==`` / ``Cookie: …``
    later in the same text.  R21 Block 23: a ``Cookie: sid=abc trailing`` line
    masked by a preceding prose-Basic on the same line escaped whole.  This
    helper decides per matched VALUE whether the exemption may apply at all:

    * more than one sensitive field name in the value (``…; Cookie: …``) — a
      real field beyond the prose-Basic one, never prose;
    * no Basic scheme token — a real credential header (``Cookie: sid=abc``,
      ``Authorization: Bearer …``), never prose;
    * a real base64 payload (``Basic YWJjZA==``) — never prose;
    * a SHORT non-empty Basic payload (``Basic a``) — a placeholder credential,
      never prose (R21 Block 27 restores the non-empty-short-value rejection);
    * a payload whose decoded bytes contain ``:`` — a real ``user:pass``
      credential even when the bytes are not UTF-8 (``Basic dXNlcjr/`` decodes
      to ``user:\xff``), never prose (R22 Block 29 closes the Latin-1/base64
      variant);
    * a canonical payload whose decoded bytes are TEXT-LIKE (mostly printable
      ASCII, ``Basic dXNlcv8=`` decodes to ``user\xff``) — a real
      username/password fragment, never prose (R23 Block 34);
    * an invalid-base64 payload that is NOT a proven multi-word English phrase
      (``Basic ab extra`` / ``Basic abc extra`` / lone ``ab`` / ``abc``) —
      never prose (R23 Block 34);
    * otherwise every Basic payload is non-credential prose -> prose
      (``authorization: Basic is required`` — trailing text after a
      non-decodable ``is`` forms the phrase ``is required``;
      ``authorization: Basic auth/setting`` — a decodable body whose bytes are
      binary junk with no ``:`` and a low printable-byte ratio, so prose even
      at the value end).
    """
    if len(list(_HEADER_FIELD_NAME_RE.finditer(value))) != 1:
        return False
    return (
        _BASIC_VALUE_TOKEN_RE.search(value) is not None
        and all(
            _is_basic_payload_prose(bm, value)
            for bm in _BASIC_VALUE_TOKEN_RE.finditer(value)
        )
    )


def _is_basic_payload_prose(bm: re.Match[str], value: str) -> bool:
    """True when ONE Basic payload inside a sensitive-header value is
    non-credential prose (R23 Block 34).  The exemption is a STRUCTURED
    payload-classification, not a length/decode-result/punctuation shortcut:

    * a valid base64 payload is a real credential (``Basic YWJjZA==``);
    * a valid-base64 payload that decodes to bytes containing ``:`` is a real
      ``user:pass`` credential even in Latin-1 (``Basic dXNlcjr/`` ->
      ``user:\xff``);
    * a CANONICAL payload (base64 alphabet only, no ``/`` or ``+``) that
      decodes to TEXT-LIKE bytes — a printable-ASCII ratio >= 0.5, e.g.
      ``Basic dXNlcv8=`` -> ``user\xff`` — is a real username/password
      fragment (R23 Block 34);
    * a decodable payload whose bytes contain ZERO printable ASCII (``Basic
      //8=`` decodes to ``\xff\xff``) is PURE BINARY — a real credential body,
      never prose (R26 Block 42: base64 validity is not a pass);
    * an invalid-base64 payload is prose only when the payload plus any
      trailing text is a proven 2+ word English phrase (``is required``);
      ``ab``/``abc``/``ab extra``/``abc extra`` fail closed;
    * anything else — a binary junk body (``Basic auth/setting`` decodes to
      non-UTF-8 bytes, no ``:``, low printable ratio, at least one printable
      byte) — is prose.
    """
    payload = bm.group("payload")
    if _is_valid_basic_payload(payload):
        return False
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        decoded = None
    if decoded is not None:
        if b":" in decoded:
            return False
        printable = sum(1 for byte in decoded if 32 <= byte < 127)
        # R26 Block 42: a body with ZERO printable ASCII bytes is PURE BINARY —
        # never English prose, always a real credential payload.  ``Basic //8=``
        # decodes to ``\xff\xff`` and fails closed (base64 validity != pass),
        # while ``Basic auth/setting`` keeps >= 1 printable byte and stays a
        # binary-junk prose body.
        if decoded and printable == 0:
            return False
        if decoded and printable / len(decoded) >= 0.5:
            return False
        rest = value[bm.end():].strip()
        return rest == "" or _is_english_prose_phrase(payload + " " + rest)
    # Invalid base64: the payload (plus trailing text) must be a proven English
    # phrase; a lone short token is a placeholder credential.
    if len(payload) < 2 and not value[bm.end():].strip():
        return False
    return _is_english_prose_phrase(payload + " " + value[bm.end():].strip())


class _WholeHeaderScan:
    """The ``whole_header`` shape as a drop-in ``re.Pattern``-like object with
    the round-20 Block 20c Basic-prose exception.

    The whole-header shape matches a COMPLETE ``Authorization`` /
    ``Proxy-Authorization`` / ``Set-Cookie`` / ``Cookie`` field (name + value
    together) for producer/consumer masking and free-form diagnostic rejection.
    The R20 exception only applies to the FINAL-scan ``.search`` role: a value
    that is ``Basic <payload>`` where the payload is NOT valid base64
    (``authorization: Basic auth/setting``, ``authorization: Basic is
    required``) is prose and must stay allowed, while a real Basic payload
    (``Basic YWJjZA==``) and every non-Basic authorization/cookie value are
    still leaks.  ``.sub`` / ``.finditer`` delegate to the raw regex — the
    producer/consumer mask layers stay conservative (masking prose is harmless).

    R21 Block 22: ``.search`` iterates EVERY header match, so a prose-Basic
    field never hides a later real ``Authorization``/``Cookie`` field.
    """

    def search(self, text: str) -> re.Match[str] | None:
        for match in _SHAPE_PATTERN_WHOLE_HEADER_RE.finditer(text):
            if not _is_whole_header_prose(match.group(0)):
                return match
        return None

    def sub(self, repl: str, text: str, count: int = 0) -> str:
        return _SHAPE_PATTERN_WHOLE_HEADER_RE.sub(repl, text, count=count)

    def finditer(self, text: str) -> re.Iterator[re.Match[str]]:
        return _SHAPE_PATTERN_WHOLE_HEADER_RE.finditer(text)


_WHOLE_HEADER_SCAN = _WholeHeaderScan()

# R20 Block 20a: the ``Digest`` auth scheme's ``response`` parameter is a
# per-request hex credential (``Digest username="user", response="<64hex>"``,
# ``Digest response=<64hex>;``).  The committed/failure FINAL scans must reject
# the Digest field independently — the 64-hex is otherwise masked by the gate's
# ``_mask_hex_hash_spans`` (a recomputable digest) before the token-run shape
# sees it, so this recognizer is wired as a RAW-TEXT scan BEFORE hex masking in
# ``_secret_scan_bytes`` / ``_scan_decoded_string_value`` (R20 Block 20a).  A
# server challenge (``WWW-Authenticate: Digest realm=…, nonce=…``) carries no
# ``response=`` and stays allowed.
_SHAPE_PATTERN_DIGEST_AUTH_RE = re.compile(
    # R21 Block 25: the ``Digest`` auth ``response`` hex credential is bound to
    # the REAL Digest-auth field context — the keyword must either be followed
    # by at least one auth parameter (``Digest username="user", response=…``)
    # or immediately by ``response=`` (a bare ``Digest response=<hex>`` value).
    # Prose like ``model digest calculation response=<32hex>`` — a noun
    # ``digest`` with a plain word between it and ``response=`` — no longer
    # matches (business positive preserved), while a real Digest header /
    # decoded value still fails closed on both FINAL scans.
    #
    # R22 Block 32: the leading guard is a FIELD-POSITION guard, not any
    # non-alphanumeric — ``digest`` must sit at a line/value start, after a
    # structural delimiter (``,;{}[]"'\\`` or a real authorization-field
    # colon).  ``result:`` is a plain colon, not an authorization field, so a
    # business sentence ``model result: digest algorithm=md5 response=<hex>``
    # no longer matches (business positive preserved).
    #
    # R23 Block 35: the auth-parameter list is a COMMA-SEPARATED grammar — an
    # optional ``username``/``realm``/… parameter list followed by a final
    # comma then ``response=<hex>`` — never a space-squashed ``algorithm=md5
    # response=``.  ``digest algorithm=md5 response=…`` (space-separated prose
    # narration) is preserved.
    #
    # R24 Block 38: the R23 DESCRIPTOR-NOUN guard (``(?<![A-Za-z0-9_-])
    # [a-z][a-z0-9-]*[ \t]+``) is REMOVED — it fired on ANY preceding noun, so
    # business narration ``model Digest username="user", response=<64hex>`` /
    # ``notice Digest …`` was misrejected as a credential header.  ``Digest``
    # now binds ONLY to the real Authorization/Proxy-Authorization field colon
    # or a STANDALONE structural position (line/value start or after a
    # ``,;{}[]"'\\`` delimiter) — a descriptor-word prefix means the text is a
    # model/business narrative, never a credential header.
    #
    # R26 Block 42: the descriptor guard is RESTORED but narrowed to REAL
    # header-context descriptors — network-entity words that sit in front of an
    # actual upstream/origin HTTP header block in a server log (``upstream
    # Digest username="user", response=<64hex>``).  These are bound to a real
    # credential-header context and REJECT, while the business-narration
    # descriptors (``model`` / ``notice`` / ``the`` / ``result``) stay out and
    # remain accepted.  The hex length is ANY 16-128 run, so ``response=<16/32/
    # 64hex>`` all fail closed the same way.
    r"(?i)(?:^|[\r\n,;{}\[\]\"'\\]|"
    r"(?:authorization|proxy-authorization)[ \t]*:[ \t]*|"
    r"(?<![A-Za-z0-9_-])(?:upstream|origin|backend|gateway|proxy|remote|peer|"
    r"server|client)[ \t]+)digest\b(?:"
    r"[ \t]+response\s*=\s*[\"']?[0-9a-f]{16,128}"
    r"|[ \t]+(?:username|realm|nonce|uri|qop|nc|cnonce|opaque|algorithm|"
    r"stale|domain)\s*=\s*(?:[\"'][^\"']{0,80}[\"']|[^,\r\n]{1,80})"
    r"(?:[ \t]*,[ \t]*(?:username|realm|nonce|uri|qop|nc|cnonce|opaque|"
    r"algorithm|stale|domain)\s*=\s*(?:[\"'][^\"']{0,80}[\"']|[^,\r\n]{1,80}))*"
    r"[ \t]*,[ \t]*response\s*=\s*[\"']?[0-9a-f]{16,128}"
    r")"
)

# R20 Block 17: the redaction-marker RESIDUE — ``[REDACTED]`` immediately
# followed by a credential character (``[REDACTED]mySuperSecret123`` in a
# committed summary, or a ``[REDACTED]<alnum>`` splice after a split value).
# The exact marker followed by a JSON delimiter / space / end is the gate's own
# clean report and never matches this shape.
#
# R22 Block 31: the residue recognizer is case-insensitive and covers the
# normalized (NFKC + casefold) copy — a MIXED-case residue
# (``[Redacted]mySecret1``), a full-width marker
# (``[\\uff32\\uff25\\uff24\\uff21\\uff23\\uff34\\uff25\\uff24]mySecret1``)
# or a zero-width/Cf-obfuscated marker (``[RE\\u200dDACTED]mySecret1``) is the
# same marker-splice leak in an ordinary summary and fails closed on both
# finals, while the gate's OWN clean exact ``[REDACTED]`` marker is blanked
# before this scan (R21 Block 26) and is never followed by a credential char.
_SHAPE_PATTERN_REDACTION_RESIDUE_RE = re.compile(
    r"(?i)\[REDACTED\](?=[A-Za-z0-9+/=])"
)

# R20 Block 17: a BARE credential-shaped value with NO field name — a
# camelCase token with a digit run (``mySuperSecret123``), the classic
# generated-password shape.  A committed artifact must fail closed on the value
# class even when no credential-field name or known secret value is present.
# Word-bounded so prose / snake_case keys never match.
#
# R22 Block 28 + Block 32: the R21 MULTI-DIGIT floor (``{2,}``) is REPLACED by a
# real discriminator so ANY-digit fail-closed returns (``mySuperSecret1`` /
# ``abcD1xyz9`` reject) while business camelCase (``flightOption1`` /
# ``flightOption12`` / ``day2`` / ``plannerV2`` / ``providerV4``) stays
# accepted.  A token is a bare credential when it either contains a
# credential KEYWORD component (``secret`` / ``token`` / ``password`` / … —
# ``mySuperSecret1``) or has a MID-TOKEN digit run followed by letters
# (``abcD1xyz9`` — the digit is not a trailing version number).
_BARE_CREDENTIAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[a-z]+[A-Z][A-Za-z0-9_]*[0-9]+[A-Za-z0-9_]*(?![A-Za-z0-9_])"
)

# R26 Block 41: business-identifier determination is a CLOSED, AUDITABLE
# registry of TripChord business-identifier BASES bound to parsed
# schema/field-path sources — NOT a dictionary, NOT a word list, NOT a
# threshold.  Each base is the EXACT camelCase prefix of a known TripChord
# business identifier and is documented with the schema/field path it comes
# from.  A bare camelCase-and-digit token is a business identifier ONLY when
# its base (after stripping a trailing ``V<digits>`` version marker) EXACTLY
# equals a registered base; anything else — ``qwerTy`` / ``myFlightHotel`` /
# ``flightHotelTrip`` / ``userHotelPlan`` / ``openTripPlan`` /
# ``myHotelPlan`` / ``accessLogCount`` / ``purpleMonkeyDishwasher`` / any
# unknown chain — fails closed BY CONSTRUCTION (an unregistered base is a bare
# credential value, never a business identifier).
#
# This replaces the R23 controlled-vocabulary / R24 207k-dictionary / R25
# Trip-word-list determinations (supervision Block 41: 禁止对任意 free-text 用
# 词典/Trip 词表/阈值做身份猜测).  ``refreshTokenCount`` is a REGISTERED business
# base even though a camelCase segment split would expose a ``Token`` segment —
# the contract fixes it as a business positive (R23 Block 33, restored by R26
# Block 40), and the closed exact-base match is what grants that exemption, so
# fixing Block 39 can never silently flip it back.
_BUSINESS_IDENTIFIER_BASES = frozenset(
    {
        # plan.flight_option[].option — flight option selector (R24 Block 37)
        "flightOption",
        # plan.hotel_amenity[].amenity — hotel amenity selector (R24 Block 37)
        "hotelAmenity",
        # plan.booking_reference — booking reference (R24 Block 37)
        "bookingReference",
        # oauth.refresh_token_count — token-refresh counter (R23 Block 33)
        "refreshTokenCount",
        # plan.planner_version — planner version marker (R24 Block 36)
        "planner",
        # plan.provider_version — provider version marker (R24 Block 36)
        "provider",
        # plan.tokenization_version — tokenization version (R23 Block 33)
        "tokenization",
        # plan.secretariat_version — secretariat version (R23 Block 33)
        "secretariat",
        # plan.day — itinerary day index (R21 Block 25 ``day2``)
        "day",
    }
)


def _is_bare_credential_token(token: str) -> bool:
    """True when a camelCase-and-digit token is a BARE credential value — the
    STRUCTURED recognizer (R23 Block 33 / R24 Block 36 / R26 Block 41), NOT a
    keyword/threshold/heuristic recognizer.  ``qwerTy1`` (a short keyboard
    mash + digit) and ``mySuperSecret1`` structurally share the shape of
    ``flightOption12`` (lower+Upper+lower+digits), so no
    length/digit-position/punctuation rule can separate them.  The decision is
    identifier STRUCTURE + a CLOSED business-identifier registry:

    * a digit run followed by a letter INSIDE the token (``1xyz`` in
      ``abcD1xyz9``) — the digit is embedded, not a trailing number — is a
      credential (fail-closed);
    * a trailing ``V<digits>`` version marker (``plannerV2`` /
      ``tokenizationV1`` / ``secretariatV1``) is a versioned business
      identifier ONLY when the version-stripped base is a registered business
      base — ``mySuperSecretV1`` / ``refreshTokenV1`` / ``qwerTyV1`` are NOT
      registered and stay credentials.  The version branch is SYMMETRIC with
      the non-version branch (R26 Block 41: ``qwerTyV1`` and ``qwerTy1`` judge
      identically, both fail-closed);
    * a trailing-digit token whose base is EXACTLY a registered
      ``_BUSINESS_IDENTIFIER_BASES`` entry (``flightOption1`` /
      ``hotelAmenity3`` / ``bookingReference1`` / ``refreshTokenCount1`` /
      ``day2``) is a business identifier;
    * ANY other digit-bearing camelCase token (``qwerTy1`` /
      ``myFlightHotel1`` / ``purpleMonkeyDishwasher1``) fails closed as a
      bare credential shape.
    """
    if re.search(r"[0-9]+[A-Za-z]", token):
        return True
    m = re.match(r"^([A-Za-z]+)([0-9]+)$", token)
    if m is None:
        return False
    prefix, _digits = m.group(1), m.group(2)
    base = prefix[:-1] if prefix[-1] in "Vv" else prefix
    # R26 Block 41: exact closed-registry match.  ``refreshTokenCount1`` is a
    # registered business base; ``refreshTokenV1`` (base ``refreshToken``) and
    # ``qwerTyV1`` / ``qwerTy1`` (base ``qwerTy``) are not registered and fail
    # closed — the version branch judges by the SAME registry, so there is no
    # asymmetric version exemption to exploit.
    return base not in _BUSINESS_IDENTIFIER_BASES


class _BareCredentialScan:
    """Final-scan ``bare_credential_value`` backstop as a drop-in ``.search``-able
    object: iterates EVERY word-bounded camelCase-and-digit token and rejects it
    via :func:`_is_bare_credential_token` (R22 Block 28/32 restore the any-digit
    contract while keeping business values positive)."""

    def search(self, text: str) -> re.Match[str] | None:
        for m in _BARE_CREDENTIAL_TOKEN_RE.finditer(text):
            if _is_bare_credential_token(m.group(0)):
                return m
        return None


_SHAPE_PATTERN_BARE_CREDENTIAL_VALUE_RE = _BareCredentialScan()

_S = PatternScope
# ``FINAL_VALUE`` deliberately EXCLUDES the dotted-token, whole-header and
# token-run shapes:
#
# * dotted-token + whole-header — a committed evidence artifact legitimately
#   carries dotted domains (``https://flights.ctrip.com/...``,
#   ``api.icomtours.com/...``) and the field-position auth/cookie value scan
#   already covers header forms.  The full set — including dotted JWTs — still
#   applies to FREE-FORM failure diagnostics via ``FINAL_TEXT``, where such
#   text is a real credential signal.  The ONE exception is the
#   ``basic_auth`` shape (R18 Block 1): the complete ``Authorization`` /
#   ``Proxy-Authorization`` Basic field (scheme + base64 payload) is exactly
#   what the weak credential-field branch can NOT see, so the committed
#   decoded-value scan needs its own recognizer for it.
# * token-run — a committed evidence artifact legitimately carries 32+ ASCII
#   runs that are NOT credentials (test names like ``test_booking_planning_
#   integration``, Chrome extension ids like ``chrome-mv3-<40-hex>``).  A 32+
#   run in a decoded VALUE is already redacted by the compact desensitizer
#   (``_desensitize_check_scalar``), the unknown-64-hex rejector owns bare
#   digests, and the FREE-FORM failure-diagnostic level scan rejects a
#   token-run via ``FINAL_TEXT`` — so the decoded-value scan is the one place a
#   token run must NOT be trusted as a leak signal.
_FINAL_VALUE_SHAPES = (
    _S.PRODUCER_MASK
    | _S.CONSUMER_MASK
    | _S.NORMALIZED
    | _S.FINAL_TEXT
    | _S.FINAL_VALUE
)
SHAPE_PATTERN_REGISTRY: tuple[SensitiveShapePattern, ...] = (
    SensitiveShapePattern(
        name="whole_header",
        pattern=_WHOLE_HEADER_SCAN,
        kind="whole Authorization/Cookie header",
        scopes=_S.PRODUCER_MASK | _S.CONSUMER_MASK | _S.NORMALIZED | _S.FINAL_TEXT,
    ),
    SensitiveShapePattern(
        name="url",
        pattern=_SHAPE_PATTERN_CANARY_URL_RE,
        kind="tracking URL",
        scopes=_S.PRODUCER_MASK | _S.CONSUMER_MASK | _S.NORMALIZED,
    ),
    SensitiveShapePattern(
        name="token_run",
        pattern=_SHAPE_PATTERN_TOKEN_RUN_RE,
        kind="token-shaped run",
        # Deliberately NOT ``FINAL_VALUE``: committed evidence legitimately
        # carries 32+ ASCII runs (test names, Chrome extension ids), so a
        # token run is a leak signal on the FREE-FORM failure-diagnostic level
        # scan (``FINAL_TEXT``) and in the mask layers, never in a decoded
        # value of a committed artifact.  See ``_FINAL_VALUE_SHAPES`` above.
        scopes=(
            _S.PRODUCER_MASK
            | _S.CONSUMER_MASK
            | _S.NORMALIZED
            | _S.FINAL_TEXT
        ),
    ),
    SensitiveShapePattern(
        name="akia",
        pattern=_SHAPE_PATTERN_AKIA_RE,
        kind="AKIA-style access key",
        scopes=_FINAL_VALUE_SHAPES,
    ),
    SensitiveShapePattern(
        name="prefix_token",
        pattern=_SHAPE_PATTERN_PREFIX_TOKEN_RE,
        kind="prefixed token",
        scopes=_FINAL_VALUE_SHAPES,
    ),
    SensitiveShapePattern(
        name="bearer",
        pattern=_SHAPE_PATTERN_BEARER_RE,
        kind="short Bearer token",
        scopes=_FINAL_VALUE_SHAPES,
    ),
    SensitiveShapePattern(
        name="dotted_token",
        pattern=_SHAPE_PATTERN_DOTTED_TOKEN_RE,
        kind="dotted bearer token",
        scopes=(
            _S.PRODUCER_MASK
            | _S.CONSUMER_MASK
            | _S.NORMALIZED
            | _S.FINAL_TEXT
        ),
    ),
    # C-122 supervision 09:59 Block 4: the legacy ``opaque_kv`` shape (a
    # ``{3,}`` token-run value under a fixed key list with NO field boundary)
    # falsely rejected real report prose (``pending user authorization: not all
    # certified…``) while missing the stronger credential-FIELD shapes.  It is
    # REMOVED — every name it carried (``token`` / ``password`` / ``passwd`` /
    # ``secret`` / ``apikey`` / ``api_key`` / ``access_key`` / ``secret_key`` /
    # ``client_secret`` / ``authorization`` / ``bearer`` / ``private_key`` /
    # ``session_key``) is now folded into the strong/weak credential_field
    # branches with the shared boundary semantics.
    SensitiveShapePattern(
        name="credential_field",
        pattern=_SHAPE_PATTERN_CREDENTIAL_FIELD_RE,
        kind="credential field name assignment",
        scopes=_FINAL_VALUE_SHAPES,
    ),
    # R18 Block 1: the ONE Basic-scheme header exception to the
    # FINAL_VALUE-excludes-whole-header rule.  The weak credential-field branch
    # needs a single token payload, so ``Authorization: Basic YWJjZA==`` /
    # ``proxy-authorization: Basic YWJjZA==`` pass the committed decoded-value
    # scan untouched.  This is the FINAL-scan independent backstop for the
    # complete Basic field — the whole-header shape (which masks it in the
    # producer / consumer / failure diagnostics) is deliberately not in
    # FINAL_VALUE, so the committed path needed its own recognizer.  The three
    # real ``pending user authorization: …`` prose positives carry no ``Basic``
    # scheme and stay allowed.  Scoped to the FINAL scans only.
    SensitiveShapePattern(
        name="basic_auth",
        # The drop-in validated scan object: regex bounding + base64 payload
        # validity (R20 Block 20c) — its ``.search`` has the same contract as a
        # compiled pattern, so every ``pattern.search`` scan loop is unchanged.
        pattern=_BASIC_AUTH_SCAN,
        kind="Basic Authorization field",
        scopes=_S.FINAL_TEXT | _S.FINAL_VALUE,
    ),
    # R20 Block 17: the redaction-marker RESIDUE (``[REDACTED]`` immediately
    # followed by a credential character) and the BARE credential-shaped value
    # (a camelCase token ending in a digit run) — the committed / failure final
    # scans' independent backstop for the bare-value leak class.  Scoped to the
    # FINAL scans only; the producer / consumer already mask the marker-splice
    # form whole via the credential-field trailing-chars rule.
    SensitiveShapePattern(
        name="redaction_residue",
        pattern=_SHAPE_PATTERN_REDACTION_RESIDUE_RE,
        kind="redaction-marker residue",
        scopes=_S.FINAL_TEXT | _S.FINAL_VALUE,
    ),
    SensitiveShapePattern(
        name="bare_credential_value",
        pattern=_SHAPE_PATTERN_BARE_CREDENTIAL_VALUE_RE,
        kind="bare credential-shaped value",
        scopes=_S.FINAL_TEXT | _S.FINAL_VALUE,
    ),
    # R20 Block 20a: the ``Digest`` auth ``response`` hex credential.  Scoped to
    # the FINAL scans; the gate additionally runs it on the RAW (pre-hex-mask)
    # text because ``_mask_hex_hash_spans`` would otherwise hide the response
    # digest before the registry scan sees it.
    SensitiveShapePattern(
        name="digest_auth",
        pattern=_SHAPE_PATTERN_DIGEST_AUTH_RE,
        kind="Digest-auth response",
        scopes=_S.FINAL_TEXT | _S.FINAL_VALUE,
    ),
)


def registry_patterns(*scopes: PatternScope) -> tuple[re.Pattern[str], ...]:
    """Patterns from the registry whose scope includes ANY of ``scopes``."""
    wanted = PatternScope(0)
    for scope in scopes:
        wanted |= scope
    return tuple(
        entry.pattern for entry in SHAPE_PATTERN_REGISTRY if entry.scopes & wanted
    )


def registry_pattern(name: str) -> re.Pattern[str]:
    """The single registry pattern for ``name`` — what a caller keeps as a
    named module-level variable so a mask layer can apply per-pattern
    replacements while still deriving from the ONE registry (补充 C)."""
    for entry in SHAPE_PATTERN_REGISTRY:
        if entry.name == name:
            return entry.pattern
    raise KeyError(f"no sensitive-shape registry pattern named {name!r}")


def registry_shape_pairs(
    *scopes: PatternScope,
) -> tuple[tuple[re.Pattern[str], str], ...]:
    """``(pattern, kind)`` pairs from the registry whose scope includes ANY of
    ``scopes`` — what the final secret scan iterates to reject a shape."""
    wanted = PatternScope(0)
    for scope in scopes:
        wanted |= scope
    return tuple(
        (entry.pattern, entry.kind)
        for entry in SHAPE_PATTERN_REGISTRY
        if entry.scopes & wanted
    )


def _basic_payload_preserve_spans(text: str) -> list[tuple[int, int]]:
    """Original-text spans of ``Basic``-scheme payload tokens to keep VERBATIM
    through normalization.

    R21 Block 24: a full-width (``\uff22\uff41\uff53\uff49\uff43``) or
    zero-width / Cf-obfuscated (``Basic``) scheme is invisible to
    ``_BASIC_VALUE_TOKEN_RE`` on the RAW text, so its payload would otherwise be
    casefolded and the normalized copy's base64-validity prose-exemption would
    misclassify the real credential as prose (``Basic YWJjZA==`` ->
    ``basic ywjjza==``).  The scheme is NFKC-composed and Cf-dropped on a
    DETECTION copy (NEVER casefolded — base64 payloads are case-sensitive), the
    ASCII scheme+payload is matched there, and the payload span is mapped back
    to the ORIGINAL text.  Only the payload TOKEN is preserved; an unrelated
    uppercase 64-hex digest (``{"sha256": "AAAA..."}``) is not a Basic payload
    and still casefolds.
    """
    pre_chars: list[str] = []
    pre_to_orig: list[int] = []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) == "Cf" or ch == "​":
            continue
        for nc in unicodedata.normalize("NFKC", ch):
            pre_chars.append(nc)
            pre_to_orig.append(i)
    pre = "".join(pre_chars)
    spans: list[tuple[int, int]] = []
    for m in _BASIC_VALUE_TOKEN_RE.finditer(pre):
        ps, pe = m.span("payload")
        spans.append((pre_to_orig[ps], pre_to_orig[pe - 1] + 1))
    return spans


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """NFKC + casefold + drop Cf/U+200B, with an offset map for span mapping.

    C-122 supervision 00:06 (要求 B): sensitive key/value DETECTION runs on a
    NORMALIZED COPY of the text — Unicode NFKC composes full-width forms
    (``\uff21uthorization`` -> ``Authorization``) and canonical equivalents to their
    ASCII core, ``casefold`` is the stronger Unicode case fold (so whatever
    casing is smuggled in compares equal), and Cf (format) category chars plus
    the zero-width space U+200B are DROPPED so a credential can never hide
    between its own letters.  Returns ``(normalized, offsets)`` where
    ``offsets[i]`` is the ORIGINAL character index of normalized char ``i``
    (dropped Cf chars map to no output char) — a caller that masks can map a
    span found on the copy back onto the original text.
    """
    out: list[str] = []
    offsets: list[int] = []
    i = 0
    n = len(text)
    # R20 Block 20c + R21 Block 24: ``Basic`` payload tokens are preserved in
    # their ORIGINAL case (see ``_BASIC_VALUE_TOKEN_RE`` above) so the
    # casefolded copy keeps a real ``Basic YWJjZA==`` payload valid for the
    # prose-exemption — including a full-width / Cf-obfuscated SCHEME spelling
    # (``\uff22\uff41\uff53\uff49\uff43 YWJjZA==``), whose payload the detection copy
    # (:func:`_basic_payload_preserve_spans`) still finds.  Only the payload
    # TOKEN is preserved — an unrelated uppercase 64-hex digest (``{"sha256":
    # "AAAA..."}``) is not a Basic payload and still casefolds.
    preserved_spans = _basic_payload_preserve_spans(text)
    ps_idx = 0
    while i < n:
        while (
            ps_idx < len(preserved_spans) and preserved_spans[ps_idx][1] <= i
        ):
            ps_idx += 1
        if ps_idx < len(preserved_spans):
            ps_start, ps_end = preserved_spans[ps_idx]
            if ps_start <= i < ps_end:
                ch = text[i]
                # R21 Block 24: even INSIDE a preserved payload span, a Cf /
                # zero-width char is dropped (a ``Basic YW​JjZA==``
                # obfuscation must not keep its zero-width and re-break the
                # base64 validity of the normalized copy).  The payload CASE is
                # preserved; format chars never are.
                if unicodedata.category(ch) == "Cf" or ch == "​":
                    i += 1
                    continue
                out.append(ch)
                offsets.append(i)
                i += 1
                continue
        ch = text[i]
        # R20 Block 18: the gate's fixed redaction marker ``[REDACTED]`` is
        # preserved VERBATIM through normalization.  The marker exemption in the
        # credential-field shape is CASE-SENSITIVE (only exact ``[REDACTED]`` is
        # the marker; ``[Redacted]`` / ``[redacted]`` are impersonations to be
        # masked/rejected), and the normalized copy is what the producer /
        # consumer mask layer and the gate scan run the SHARED pattern on \u2014 a
        # casefolded ``[redacted]`` would trip the case-sensitive exemption and
        # re-mask / re-reject the gate's own redacted reports.  Only the exact
        # ASCII span is preserved; a full-width ``\uff3bREDACTED\uff3d`` or a
        # Cf-obfuscated ``[R\u200bEDACTED]`` still normalizes to ``[redacted]`` and
        # stays a masked impersonation.
        if ch == "[" and text.startswith("[REDACTED]", i):
            for j, mc in enumerate("[REDACTED]"):
                out.append(mc)
                offsets.append(i + j)
            i += len("[REDACTED]")
            continue
        if unicodedata.category(ch) == "Cf" or ch == "\u200b":
            i += 1
            continue
        for nc in unicodedata.normalize("NFKC", ch).casefold():
            out.append(nc)
            offsets.append(i)
        i += 1
    return "".join(out), offsets


def _normalize_for_scan(text: str) -> str:
    """Normalize ``text`` for sensitive-pattern detection (no offset map)."""
    return _normalize_with_offsets(text)[0]


def mask_normalized_spans(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
    marker: str = "[REDACTED]",
) -> str:
    """Mask every span of ``text`` that ``patterns`` match on the NORMALIZED
    copy (NFKC + casefold, Cf/U+200B dropped), mapping the span back onto the
    ORIGINAL characters.

    Only the credential-carrying span is masked — the surrounding prose
    survives — and only a COPY is normalized for detection; the original text
    is returned with those spans masked.  Used by the producer / consumer mask
    layers so a full-width / zero-width-obfuscated credential is collapsed even
    though the ASCII shape regexes stopped seeing it on the raw text.
    """
    if not patterns:
        return text
    normalized, offsets = _normalize_with_offsets(text)
    if not normalized:
        return text
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        spans.extend(match.span() for match in pattern.finditer(normalized))
    if not spans:
        return text
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result: list[str] = []
    last = 0
    for start, end in merged:
        orig_start = offsets[start]
        # ``end`` is exclusive on the NORMALIZED copy; the last normalized char
        # of the span is ``end - 1`` and its original char extends one further.
        orig_end = offsets[end - 1] + 1
        result.append(text[last:orig_start])
        result.append(marker)
        last = orig_end
    result.append(text[last:])
    return "".join(result)


def iter_json_levels(
    text: str,
    *,
    on_string_value: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, int, bool]]:
    """Yield ``(level_text, depth, malformed)`` for ``text`` and every nested
    JSON-string value found by bounded recursive ``json.loads``.

    ``on_string_value`` (C-122 supervision 08:30+08:31 缺口③ / 补充 A): when
    given, the SAME budget-counting walk calls it for every decoded string
    value (including the decoded value of a bare JSON string literal at any
    depth) as it visits that node — AFTER the node budget check for that value,
    so a document that overflows the budget is rejected at the first over-budget
    node and the callback never runs past the cap.  The caller's per-value scan
    (known-needle + shapes) therefore runs INSIDE the one bounded walker; there
    is no separate unbounded re-parse that could traverse a 20k-string document
    before the budget fires.  The callback may raise to abort the walk.

    * ``level_text`` — the text at one decoding level (the outer document
      first, then each JSON-string value found inside a parsed level, then each
      JSON-string found inside THAT, and so on).  The ORIGINAL byte scan is
      kept by callers as the first line of defence; this walker re-applies the
      patterns at every decoded level.
    * ``depth`` — nesting depth (top level 0, a directly nested JSON-string
      value 1, ...).
    * ``malformed`` — True when this level STARTS with ``{`` (an OBJECT — the
      shape credential field names / values live in) but does not parse — a
      truncated or obfuscated JSON attempt that could hide a credential.
      Callers fail closed on it.  ``[``- and ``"``-starting strings are NOT
      malformed when unparseable: the gate's own ``[REDACTED]`` marker starts
      with ``[`` and must never be treated as a leak, and a bare array / string
      literal cannot carry a ``name: value`` credential pair (their literal text
      is still covered by the raw-byte and per-level pattern scans).

    Raises :class:`RecursiveJsonBudgetError` when a depth / node / size budget
    is exceeded — a maliciously deep or huge document can never force
    unbounded work.
    """
    budget_nodes = 0
    budget_chars = 0
    stack: list[tuple[str, int]] = [(text, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_JSON_SCAN_DEPTH:
            raise RecursiveJsonBudgetError("JSON scan depth budget exceeded")
        budget_nodes += 1
        if budget_nodes > _MAX_JSON_SCAN_NODES:
            raise RecursiveJsonBudgetError("JSON scan node budget exceeded")
        budget_chars += len(current)
        if budget_chars > _MAX_JSON_SCAN_CHARS:
            raise RecursiveJsonBudgetError("JSON scan size budget exceeded")
        parsed: Any = None
        malformed = False
        if looks_like_json(current):
            try:
                # C-122 supervision 09:59 Block 2: load through the canonical
                # parser — a duplicate object member key fails closed (a
                # published artifact could otherwise smuggle a foreign digest
                # under a whitelisted key, then ``json.loads`` keep only the
                # second value).  The exception is a ``ValueError`` so the
                # OBJECT-shaped start becomes malformed and every caller fails
                # closed at ``depth >= 1``.
                parsed = json_loads_no_dupes(current)
            except (json.JSONDecodeError, ValueError, RecursionError):
                # Only an OBJECT-shaped start can hide a credential field
                # name/value pair; the ``[REDACTED]`` marker and bare arrays /
                # string literals are not malformed-JSON attempts.
                malformed = current.lstrip().startswith("{")
                parsed = None
        yield (current, depth, malformed)
        if parsed is None:
            continue
        if isinstance(parsed, str):
            # A bare JSON string literal whose decoded value is itself JSON
            # text — the next encoding level down.  The DECODED value is a
            # string value too (补充 A: 顶层/嵌套 bare JSON string literal).
            if on_string_value is not None:
                on_string_value(parsed)
            if looks_like_json(parsed):
                stack.append((parsed, depth + 1))
            continue
        # Collect every JSON-string value in the parsed structure so each is
        # scanned at the next depth down.  The SAME hard budgets cover the JSON
        # STRUCTURE itself (C-122 supervision 07:29 gap 1): every dict/list/
        # scalar node counts toward the node cap and the container nesting depth
        # is capped, so a fan-out empty-dict, a 20000-primitive list or a
        # pathologically deep object inside one decoded level fails closed
        # instead of being traversed without bound.  The walk is an explicit
        # stack — never Python recursion.
        pending: list[tuple[Any, int]] = [(parsed, 0)]
        while pending:
            node, struct_depth = pending.pop()
            if struct_depth > _MAX_JSON_SCAN_DEPTH:
                raise RecursiveJsonBudgetError(
                    "JSON structural depth budget exceeded"
                )
            budget_nodes += 1
            if budget_nodes > _MAX_JSON_SCAN_NODES:
                raise RecursiveJsonBudgetError("JSON scan node budget exceeded")
            if isinstance(node, dict):
                for _key, value in node.items():
                    # The object MEMBER KEY counts as a node and is scanned
                    # individually (C-122 supervision 00:06 要求 A) — a 20000-
                    # member object fails closed on its keys alone, and every
                    # key is subject to the caller's normalized key scan.
                    budget_nodes += 1
                    if budget_nodes > _MAX_JSON_SCAN_NODES:
                        raise RecursiveJsonBudgetError(
                            "JSON scan node budget exceeded"
                        )
                    if isinstance(value, str):
                        # A normal/decoded STRING VALUE counts as a node too
                        # (00:06 要求 A) — a 20000-string document fails closed
                        # whether the strings are JSON text (scanned at the next
                        # decoded level) or plain text.
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
                        if on_string_value is not None:
                            on_string_value(value)
                        if looks_like_json(value):
                            stack.append((value, depth + 1))
                    elif isinstance(value, (dict, list)):
                        pending.append((value, struct_depth + 1))
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, str):
                        # Same string-VALUE node accounting as the dict branch.
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )
                        if on_string_value is not None:
                            on_string_value(item)
                        if looks_like_json(item):
                            stack.append((item, depth + 1))
                    elif isinstance(item, (dict, list)):
                        pending.append((item, struct_depth + 1))
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON scan node budget exceeded"
                            )


def bounded_json_mask(
    text: str,
    *,
    mask_level: Callable[[str], str],
    marker: str = "[REDACTED]",
    normalize_patterns: tuple[re.Pattern[str], ...] = (),
    key_patterns: tuple[re.Pattern[str], ...] = (),
) -> str:
    """Bounded recursive JSON / JSON-string masking.

    ``mask_level`` masks ONE free-form text level (URLs, token shapes, whole
    header fields).  If ``text`` is a whole JSON document the walker rebuilds
    it with every nested JSON-string value recursively masked, so a credential
    smuggled through multiple ``json.dumps`` layers is masked at EVERY decoded
    level.  A string that starts with a JSON structural character but does not
    parse (a truncated / obfuscated JSON attempt) is masked whole to
    ``marker`` — parse failures fail closed.  Budget overflow also fails closed
    to ``marker``.

    ``normalize_patterns`` (C-122 supervision 00:06 要求 B): when given, every
    ``mask_level`` result is ALSO re-checked on a NORMALIZED copy (NFKC +
    casefold, Cf/U+200B dropped) via :func:`mask_normalized_spans`, so a
    full-width / zero-width-obfuscated credential span the ASCII shape regexes
    stop seeing is still masked — only a copy is normalized, the artifact text
    is returned with those spans masked.

    ``key_patterns`` (C-122 supervision 09:00): when given, every STRUCTURED
    JSON dict KEY is also checked (on the raw key and its NORMALIZED copy, plus
    the exact ``BARE_CREDENTIAL_FIELD_NAMES`` names) and a credential field
    NAME is masked WHOLE with the value — the key becomes ``marker`` and the
    value still runs the normal mask, so ``{"Session_token":"abc"}`` is
    rebuilt as ``{"[REDACTED]":"[REDACTED]"}`` (valid JSON, field name gone)
    instead of leaking the key that the free-form ``mask_level`` never sees.
    The rebuild stays valid JSON so the gate's own malformed-nested-JSON scan
    never flags the sanitized artifact.
    """
    budget_nodes = 0
    budget_chars = 0

    def apply_mask_level(level_text: str) -> str:
        masked = mask_level(level_text)
        if normalize_patterns:
            return mask_normalized_spans(masked, normalize_patterns, marker=marker)
        return masked

    def is_credential_key(key: object) -> bool:
        if not isinstance(key, str):
            return False
        if key.strip().lower() in BARE_CREDENTIAL_FIELD_NAMES:
            return True
        if any(p.search(key) for p in key_patterns):
            return True
        norm = _normalize_for_scan(key)
        if norm.strip().lower() in BARE_CREDENTIAL_FIELD_NAMES:
            return True
        return norm != key and any(p.search(norm) for p in key_patterns)

    def mask_text(current: str, depth: int) -> str:
        nonlocal budget_nodes, budget_chars
        if depth > _MAX_JSON_SCAN_DEPTH:
            raise RecursiveJsonBudgetError("JSON mask depth budget exceeded")
        budget_nodes += 1
        if budget_nodes > _MAX_JSON_SCAN_NODES:
            raise RecursiveJsonBudgetError("JSON mask node budget exceeded")
        budget_chars += len(current)
        if budget_chars > _MAX_JSON_SCAN_CHARS:
            raise RecursiveJsonBudgetError("JSON mask size budget exceeded")
        if not looks_like_json(current):
            return apply_mask_level(current)
        try:
            # C-122 supervision 09:59 Block 2: canonical parser — a duplicate
            # object member key (a foreign digest smuggled under a whitelisted
            # key) is a parse failure and the whole level is masked (fail
            # closed), never silently keeping the last value.
            parsed = json_loads_no_dupes(current)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # Structural-start but not valid JSON: a truncated / obfuscated
            # JSON attempt — mask the whole level (fail closed).
            return marker
        if isinstance(parsed, str):
            return json.dumps(mask_text(parsed, depth + 1), ensure_ascii=False)
        if isinstance(parsed, (dict, list)):
            return json.dumps(
                mask_structure(parsed, depth), ensure_ascii=False
            )
        return apply_mask_level(current)

    def mask_structure(parsed: Any, level_depth: int) -> Any:
        """Iteratively rebuild ``parsed`` with every nested JSON-string value
        masked.  The SAME hard budgets cover the JSON STRUCTURE itself
        (C-122 supervision 07:29 gap 1): every dict/list/scalar node counts
        toward the node cap and the container nesting depth is capped, so a
        fan-out empty-dict, a 20000-primitive list or a pathologically deep
        object fails closed instead of relying on Python's recursion limit.
        The rebuild is an explicit stack with ``finalize`` markers — never
        Python recursion."""
        nonlocal budget_nodes, budget_chars
        root_slot: dict[str, Any] = {"__out__": None}
        # Work item: ("value", node, container, key, struct_depth) masks
        # ``node`` into ``container[key]``; ("finalize", built, container,
        # key, struct_depth) commits an already-built container.
        stack: list[tuple[str, Any, Any, Any, int]] = [
            ("value", parsed, root_slot, "__out__", 0)
        ]
        while stack:
            kind, node, container, key, struct_depth = stack.pop()
            if kind == "finalize":
                container[key] = node
                continue
            if struct_depth > _MAX_JSON_SCAN_DEPTH:
                raise RecursiveJsonBudgetError(
                    "JSON mask structural depth budget exceeded"
                )
            budget_nodes += 1
            if budget_nodes > _MAX_JSON_SCAN_NODES:
                raise RecursiveJsonBudgetError("JSON mask node budget exceeded")
            if isinstance(node, dict):
                out: dict[str, Any] = {}
                stack.append(("finalize", out, container, key, struct_depth))
                for k, v in node.items():
                    # The object MEMBER KEY counts as a node (C-122 supervision
                    # 00:06 要求 A) — a 20000-member object fails closed on its
                    # keys alone, matching the scan-layer counting.
                    budget_nodes += 1
                    if budget_nodes > _MAX_JSON_SCAN_NODES:
                        raise RecursiveJsonBudgetError(
                            "JSON mask node budget exceeded"
                        )
                    if key_patterns and is_credential_key(k):
                        # A credential FIELD NAME in a structured JSON key is
                        # masked WHOLE (name + value): the key AND the value
                        # both collapse to the marker, so a ``Session_token`` /
                        # full-width ``\uff33\uff45\uff53\uff53\uff49\uff4f\uff4e
# \uff3f\uff54\uff4f\uff4b\uff45\uff4e`` key and its
                        # payload can never survive into the rebuilt artifact
                        # while the JSON stays valid (C-122 supervision 09:00
                        # gap 2: a cookie value ``a=b`` is not itself a
                        # credential SHAPE, so the value is masked by policy,
                        # not by shape — ``{"[REDACTED]": "[REDACTED]"}``).
                        out[marker] = marker
                    elif isinstance(v, str):
                        out[k] = mask_text(v, level_depth + 1)
                    elif isinstance(v, (dict, list)):
                        stack.append(
                            ("value", v, out, k, struct_depth + 1)
                        )
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON mask node budget exceeded"
                            )
                        out[k] = v
            elif isinstance(node, list):
                out_list: list[Any] = [None] * len(node)
                stack.append(
                    ("finalize", out_list, container, key, struct_depth)
                )
                for i, item in enumerate(node):
                    if isinstance(item, str):
                        out_list[i] = mask_text(item, level_depth + 1)
                    elif isinstance(item, (dict, list)):
                        stack.append(
                            ("value", item, out_list, i, struct_depth + 1)
                        )
                    else:
                        budget_nodes += 1
                        if budget_nodes > _MAX_JSON_SCAN_NODES:
                            raise RecursiveJsonBudgetError(
                                "JSON mask node budget exceeded"
                            )
                        out_list[i] = item
            else:
                # A scalar work item is never pushed (scalars are handled
                # inline above); keep the branch for structural completeness.
                container[key] = node
        return root_slot["__out__"]

    try:
        rebuilt = mask_text(text, 0)
    except RecursiveJsonBudgetError:
        return marker
    # Final sweep: a JSON key/value pair reconstructed by the walker is
    # collapsed name-and-value together exactly like the raw scan does (and,
    # with ``normalize_patterns``, re-checked on the normalized copy).
    return apply_mask_level(rebuilt)
