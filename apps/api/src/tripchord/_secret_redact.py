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
    * otherwise a Basic payload is non-credential prose ONLY when the payload
      plus any trailing text is a proven 2+ word English phrase
      (``authorization: Basic is required`` — trailing text after a
      non-decodable ``is`` forms the phrase ``is required``;
      ``authorization: Basic auth/setting`` — a decodable body whose bytes are
      binary junk yet the payload itself is the two-word phrase
      ``auth/setting``).  A non-prose body — including a bare non-UTF-8
      payload at the value end (``Basic AP9h`` / ``Basic /0H//w==``) — fails
      closed (R27 Block 44).
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
    * a decodable binary-junk body (``Basic auth/setting`` — non-UTF-8 bytes,
      no ``:``, not pure binary, low printable ratio) is prose ONLY when the
      payload plus any trailing text is a proven 2+ word English phrase
      (``auth/setting``); a non-prose binary body (``Basic AP9h`` ->
      ``\x00\xffa``, ``Basic /0H//w==`` -> ``\xffA\xff\x0d``) fails closed (R27
      Block 44 — a base64-valid but non-UTF-8 payload is never prose by
      itself).
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
        # R27 Block 44: a decodable binary body is prose ONLY when the payload
        # plus any trailing text is a proven 2+ word English phrase.  The old
        # ``rest == ""`` shortcut let a bare non-UTF-8 body (``Basic AP9h`` ->
        # ``\x00\xffa``, ``Basic /0H//w==`` -> ``\xffA\xff\x0d``) pass as prose
        # at the value end — a non-UTF-8 sensitive header value must fail
        # closed on BOTH finals.
        rest = value[bm.end():].strip()
        return _is_english_prose_phrase(payload + " " + rest)
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
#
# R27 Block 45: the recognizer is rewritten from the R26 descriptor/parameter
# FIXED WORD LISTS to REAL RFC 7616 structure parsing — the review forbids
# descriptor word lists, parameter word lists, punctuation or length special
# cases.  A Digest credential now requires
#   * a syntactically real comma-separated ``name=value`` parameter list whose
#     names are ANY legal RFC 7230 token identifier (never a fixed set —
#     ``userhash``/``nc``/``uri``/… all parse), with the keyword DIRECTLY
#     followed by ``name=value`` (never a space-squashed narration);
#   * a ``response`` parameter whose (quote-stripped) value is a pure hex run —
#     the per-request request-digest (ANY hex length, no ``{16,128}`` bound);
#   * a binding that is a REAL Authorization/Proxy-Authorization field colon, a
#     STANDALONE structural position, or a DESCRIPTOR context that also carries
#     the request-IDENTITY parameters ``username`` / ``userhash``.  The identity
#     params are the only structural signal that names the credential HOLDER
#     rather than describing a digest algorithm, so ``service Digest
#     username="user", response=<16hex>`` and ``upstream Digest username="user",
#     userhash=true, response=<64hex>`` fail closed while ``client digest
#     algorithm=md5, response=<32hex>`` (an algorithm description, no identity)
#     stays accepted — the review's exact trilemma.
#
# R27 semantic correction to R24 Block 38: the R24 rule that ANY descriptor-noun
# prefix (``model``/``notice``/``the``…) is business narration is DELETED — it
# was a word list and it failed open on real credential structure.  A
# descriptor-prefixed ``Digest username="user", response=<hex>`` carries the
# identity + request-digest and fails closed, exactly like ``service``/
# ``upstream``; the surviving business positives are the ones WITHOUT a real
# ``username=…, response=<hex>`` structure.
_DIGEST_AUTH_KEYWORD_RE = re.compile(r"(?i)digest\b")
# R33 Block 56: the auth-parameter list is tokenized by RFC 7235 / RFC 7230
# grammar, NOT by the R27 regex ``[A-Za-z][A-Za-z0-9_-]*`` pair matcher — the
# regex silently truncated the parameter list at the first param whose name
# used an RFC ``tchar`` outside ``[A-Za-z0-9_-]`` (``foo*``), began with a
# digit (``1st``), or whose quoted value contained an RFC quoted-pair
# (``username=\"use\\\"r\"``), so a real ``response=<hex>`` credential AFTER
# that param was dropped and the header ACCEPTED both finals.  A param name is
# any RFC 7230 ``token`` run (``tchar`` = ``!#$%&'*+-.^_`|~`` + alnum); a value
# is a token or a quoted-string whose backslash-quoted-pair / JSON-escape
# (``\"``) is consumed literally.  The tokenizer is strict about the grammar
# (a ``name`` must be followed by ``=`` and a value) but tolerant of a trailing
# non-comma boundary, so a space-squashed algorithm narration
# (``model digest algorithm=md5 response=<hex>``) still parses only the first
# pair and stays accepted.
_DIGEST_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEX_FULL_RE = re.compile(r"(?i)[0-9a-f]+")

# R35 Block 60: the Digest request-IDENTITY parameter class — ``username`` /
# ``userhash`` and their RFC 7616 extended-parameter forms (``username*`` /
# ``userhash*``).  RFC 7616 ``username*=UTF-8''user`` is the extended username;
# the ``*`` suffix is RFC 5987 parameter-name syntax, so ``_DIGEST_TOKEN_RE``
# tokenizes ``username*`` as the NAME (``*`` is an RFC 7230 tchar).  The old
# exact ``"username" in params`` check missed ``username*``, so a descriptor
# digest carrying ``username*=UTF-8''user`` fell into the algorithm-description
# exception and ACCEPTED both finals; any identity parameter (including
# ``username*``) combined with any real 16/32/64-hex ``response`` now fails
# closed order-independently.
_DIGEST_IDENTITY_PARAM_RE = re.compile(r"(?i)^(?:username|userhash)\*?$")


def _is_digest_identity_params(params: dict[str, list[str]]) -> bool:
    return any(_DIGEST_IDENTITY_PARAM_RE.match(name) for name in params)


# R36 Block 62: a REAL Digest response credential inside a MALFORMED member —
# an unterminated quoted-string swallowed the ``response=<hex>`` that followed
# it (``service Digest username="user", bad="unterminated, response=<32hex>``),
# so the raw member body must be re-checked for the credential before the
# syntax error can hide it.  The value is a genuine 16+ hex run — the same
# lengths the response credential accepts — with complete boundaries, so
# ``response=abc`` prose or a hex run inside a longer token never matches.
# R38 Block 68: the token is captured up to the next member separator
# (``,;`` / end of line) — NOT truncated at whitespace, so a whitespace-padded
# wrapped value (``response=" deadbeef "`` / ``response=( deadbeef )``) stays a
# single token — and :func:`_digest_response_hex_value` recursively strips the
# legal paired wrappers + JSON-artifact escape layer, so an illegal-wrapped
# value (``response=(deadbeef)`` / ``response=[deadbeef]`` /
# ``response=((deadbeef))`` / ``response=“deadbeef”``) and a JSON-escaped /
# quoted-pair value (``response=\"deadbeef\"``) resolve to the SAME
# any-non-empty-hex determination as a normal parsed response value
# (``_HEX_FULL_RE.fullmatch``), never a hand-enumerated character table.
_DIGEST_RESPONSE_HEX_RE = re.compile(
    r"(?i)(?:^|[^\w])response[ \t]*=[ \t]*(?P<token>[^,;\r\n]+)"
)


def _digest_response_hex_value(value: str) -> bool:
    """R38 Block 68: the any-non-empty-hex determination the NORMAL parse makes
    on a parsed response value (``_HEX_FULL_RE.fullmatch``), applied after a
    BOUNDED (max 8) RECURSIVE strip of one leading and one trailing wrapper
    layer with inner-whitespace tolerance — the R36 Block 64 single leading /
    trailing quote (TORN by the member boundary or JSON escaping,
    ``response="deadbeef``) and the JSON-artifact backslash-quoted-pair
    (``\"`` / ``\'``) are the same wrapper layer, now applied repeatedly so a
    whitespace-padded or nested value — ``( deadbeef )`` / ``((deadbeef))`` /
    ``\"deadbeef\"`` / ``“deadbeef”`` — resolves to the SAME ``deadbeef`` the
    normal parse sees.  An empty wrapper (``()``), a hex prefix of a longer
    non-hex token (``deadbeefxyz``) or a mixed value (``deadbeef.g``) stays a
    non-credential, exactly like the normal parse."""
    tok = value.strip()
    depth = 0
    while depth < _STRUCTURAL_WRAPPER_DEPTH_LIMIT:
        nxt = tok
        stripped = False
        if nxt.startswith(('\\"', "\\'")):
            nxt = nxt[2:]
            stripped = True
        elif nxt[:1] in _DIGEST_VALUE_WRAPPER_CHARS:
            nxt = nxt[1:]
            stripped = True
        nxt = nxt.strip()
        if nxt.endswith(('\\"', "\\'")):
            nxt = nxt[:-2]
            stripped = True
        elif nxt[-1:] in _DIGEST_VALUE_WRAPPER_CHARS:
            nxt = nxt[:-1]
            stripped = True
        nxt = nxt.strip()
        if not stripped or nxt == tok:
            break
        tok = nxt
        depth += 1
    # R39 Block 70: the strip budget is exhausted while a wrapper layer is
    # STILL open (nesting deeper than the bound, or an unclosed wrapper that
    # kept stripping in lockstep) — the value is still a wrapped credential
    # shape, so fail CLOSED instead of falling back to a fullmatch on the
    # wrapper residue, which would read ``(deadbeef)`` as a non-hex phrase and
    # ACCEPT the credential.
    if depth >= _STRUCTURAL_WRAPPER_DEPTH_LIMIT and (
        tok[:1] in _DIGEST_VALUE_WRAPPER_CHARS or tok[-1:] in _DIGEST_VALUE_WRAPPER_CHARS
    ):
        return True
    return bool(_HEX_FULL_RE.fullmatch(tok))


def _digest_malformed_response_hex(text: str, start: int, end: int) -> bool:
    """R36 Block 64: a ``response=<value>`` buried in a malformed Digest member
    — after an unterminated quoted-string or a non-token value — is a credential
    iff the value, once a surrounding quote pair is stripped, is a NON-EMPTY
    hex run: the SAME determination the normal parse makes on a parsed response
    value (``_HEX_FULL_RE.fullmatch``).  The old ``{16,}`` threshold lowered the
    gate (an 8-hex response hidden behind an unclosed quote ACCEPTED) and
    ignored a quoted response (``response="<32hex>"``) entirely; both are now
    detected and fail closed.  ``response=abc`` prose, an empty value, or a hex
    prefix of a longer non-hex token (``response=deadbeefxyz``) are not
    credentials — matching the normal parse exactly.  R38 Block 68: the value
    resolution is the shared :func:`_digest_response_hex_value` — a wrapper
    (paren/bracket/quoted-pair/backtick) around the hex is stripped before the
    same any-non-empty-hex determination runs."""
    for m in _DIGEST_RESPONSE_HEX_RE.finditer(text, start, end):
        if _digest_response_hex_value(m.group("token")):
            return True
    return False


class _DigestAuthScan:
    """Structural Digest-auth recognizer (R27 Block 45).  Exposes the same
    ``.search(text)`` surface as a compiled pattern so the registry and the
    gate's RAW-text scan (``run_product_done_gate.py``) work unchanged; the
    gate only truth-tests the result."""

    _FIELD_RE = re.compile(r"(?i)(?:authorization|proxy-authorization)[ \t]*:[ \t]*$")
    _STANDALONE_RE = re.compile(r"(?:^|[\r\n,;{}\[\]\"'\\])[ \t]*$")

    def _classify_digest_binding(self, text: str, start: int) -> str:
        """The context immediately before the ``digest`` keyword: ``field`` (a
        real Authorization/Proxy-Authorization field colon), ``standalone``
        (line/value start or a structural delimiter ``,;{}[]"'\\``), ``descriptor``
        (a word character — the previous token is a noun), or ``none`` (a plain
        colon/dot — business prose, never a header)."""
        prefix = text[:start].rstrip()
        if not prefix:
            return "standalone"
        if self._FIELD_RE.search(prefix):
            return "field"
        if self._STANDALONE_RE.search(prefix):
            return "standalone"
        if prefix[-1].isalnum() or prefix[-1] in "-_":
            return "descriptor"
        return "none"

    def _parse_digest_auth_params(
        self, text: str, end: int
    ) -> tuple[dict[str, list[str]], int, bool] | None:
        """Parse the comma-separated ``name=value`` list that must directly
        follow the keyword (at ``end``) by RFC 7235 / RFC 7230 token and
        quoted-string grammar (R33 Block 56).  ``None`` when the text is not a
        syntactic Digest parameter list.  The parameter list ends at the first
        non-comma boundary after a value — so a space-squashed algorithm
        narration (``algorithm=md5 response=<hex>``) parses only the first pair
        and stays accepted, while ``username="use\\"r", foo*=bar, response=<hex>``
        parses every pair including the real ``response`` credential.  Returns
        ``(params, span_end, malformed_response_hex)`` — ``span_end`` is the
        index the producer masks through (R33 Block 56 producer side), and the
        third element is True when a MALFORMED member's body carried a real
        ``response=<hex>`` credential (an unterminated quoted-string swallowed
        it), so the caller can still fail closed.  R34 Block 58: EVERY
        occurrence of a duplicated parameter name is preserved in insertion
        order (``dict[str, list[str]]``), never overwritten — the caller checks
        ALL ``response`` values, so ``response=<32hex>, response=xyz`` and its
        reverse fail closed identically.

        R36 Block 62 (incremental fail-closed): a malformed member — an
        unterminated quoted-string or a non-token value (``bad=@``) — is a
        syntax ERROR, not a list terminator.  It contributes nothing but
        preserves every already-parsed pair, so a real response hex parsed
        BEFORE it is never dropped (``response=<hex>, bad="unterminated``
        rejects); a real response inside the malformed member itself
        (``bad="unterminated, response=<hex>``) is flagged via
        ``malformed_response_hex`` and the span extends to cover the swallowed
        body, so the producer masks it whole."""
        i = end
        n = len(text)
        # A real Digest header has OWS after the scheme; require at least one
        # SP / HTAB so a bare ``digest`` keyword never parses as a header.
        if i >= n or text[i] not in " \t":
            return None
        params: dict[str, list[str]] = {}
        malformed_response_hex = False
        while True:
            while i < n and text[i] in " \t":
                i += 1
            # RFC 7230 ``#rule`` (R35 Block 60): an EMPTY element — a trailing
            # comma (``response=<hex>,``) or a ``response=<hex>, , foo=bar``
            # empty member between commas — contributes nothing and must NOT
            # invalidate an already-parsed list.  It used to ``return None``
            # here, which DROPPED a real response hex parsed before the empty
            # member and ACCEPTED the header both finals.
            if i >= n:
                break
            if text[i] == ",":
                i += 1
                continue
            m = _DIGEST_TOKEN_RE.match(text, i)
            if m is None:
                # Non-token boundary: the list ends here — a space-squashed
                # narration (``algorithm=md5 response=<hex>``) parses only the
                # first pair and stays accepted.  Without a real pair this is
                # not a Digest parameter list at all.
                if not params and not malformed_response_hex:
                    return None
                break
            name = m.group(0).lower()
            i = m.end()
            while i < n and text[i] in " \t":
                i += 1
            if i >= n or text[i] != "=":
                # RFC ``#rule``: a non-empty element that is not ``name=value``
                # is a malformed member and contributes nothing.  Skip to the
                # next comma so a real response hex parsed BEFORE it is
                # preserved rather than the whole list being dropped.
                while i < n and text[i] != ",":
                    i += 1
                if i >= n:
                    break
                i += 1
                continue
            i += 1
            while i < n and text[i] in " \t":
                i += 1
            if i >= n:
                # A trailing ``name=`` with no value — malformed member,
                # preserve what was parsed before it.
                break
            if text[i] == '"' or (
                text[i] == "\\" and i + 1 < n and text[i + 1] == '"'
            ):
                # quoted-string — plain RFC quotes, or the ``\"`` a Digest
                # header inside a committed JSON artifact uses.  Quoted-pair /
                # JSON ``\X`` escapes are consumed as literal characters, so a
                # real response AFTER a quoted value that itself contains an
                # escaped quote is still parsed (``username="use\\"r", response=…``).
                value_start = i
                parsed = self._parse_digest_quoted_value(text, i)
                if parsed is None:
                    # R36 Block 62: an UNTERMINATED quoted-string swallows
                    # everything to the end of the line — including a real
                    # ``response=<hex>`` that followed it.  Record the
                    # credential in the malformed member body, extend the span
                    # to cover the swallowed body, and stop: nothing after the
                    # unclosed quote can be parsed as a new pair.
                    region_end = text.find("\n", i)
                    if region_end == -1:
                        region_end = n
                    if _digest_malformed_response_hex(text, i, region_end):
                        malformed_response_hex = True
                    i = region_end
                    break
                val, i = parsed
                # R36 Block 64: a TERMINATED quoted value can still tear a
                # ``response=<hex>`` binding — ``bad="unterminated, response="
                # <32hex>"`` — the closing quote cuts the member short and the
                # quoted hex dangles after it.  Re-scan the line from the
                # opening quote with the SAME any-non-empty-hex determination
                # and extend the span to cover the swallowed tail, so the
                # credential is not erased by the early closing quote.
                # R37 Block 66: the re-scan fires ONLY when the value ENDS
                # with a torn ``response=`` binding (the tail the closing quote
                # cut off) — a value that merely CONTAINS ``response=`` in its
                # prose (``note="response=deadbeef"`` /
                # ``algorithm="md5 response=deadbeef"``) is ordinary quoted
                # prose, shares the normal parse's value determination, and is
                # never re-scanned as a credential.
                # R38 Block 68: a ``\"...\"`` quoted-pair wrapper can ALSO tear
                # the member so the credential DANGLES right after the closing
                # quote with no comma (``bad=\"unterminated, \"response=deadbeef\"``)
                # — the escaped-quote wrapper cut the value short and the hex
                # sits in the torn member tail.  Re-scan the non-comma TAIL
                # from the closing quote with the same any-non-empty-hex
                # determination and extend the span over the swallowed tail.
                # The scan starts one char back (the closing quote) so the
                # ``response`` at the tail start still sits behind a boundary.
                line_end = text.find("\n", i)
                if line_end == -1:
                    line_end = n
                tail_comma = text.find(",", i)
                tail_end = (
                    tail_comma
                    if tail_comma != -1 and tail_comma < line_end
                    else line_end
                )
                if _digest_malformed_response_hex(text, max(i - 1, 0), tail_end):
                    malformed_response_hex = True
                    i = line_end
                elif re.search(r"(?i)response[ \t]*=[ \t]*$", val):
                    if _digest_malformed_response_hex(text, value_start, line_end):
                        malformed_response_hex = True
                        i = line_end
            else:
                m = _DIGEST_TOKEN_RE.match(text, i)
                if m is None:
                    # R36 Block 62: a non-token value (``bad=@``) is a
                    # malformed member, not a list terminator — preserve every
                    # already-parsed pair and continue after the comma.  The
                    # member body (to the next comma) is re-checked for a real
                    # ``response=<hex>`` credential.
                    region_end = text.find(",", i)
                    if region_end == -1:
                        region_end = n
                    # R38 Block 68: when the member itself is ``response``, its
                    # non-token value can carry the credential behind an ILLEGAL
                    # wrapper (``response=(deadbeef)`` / ``response=[deadbeef]``)
                    # — the region after ``=`` has no ``response=`` binding for
                    # the recovery scan, so the member VALUE is resolved with the
                    # same any-non-empty-hex determination directly.
                    if name == "response" and _digest_response_hex_value(
                        text[i:region_end]
                    ):
                        malformed_response_hex = True
                    if _digest_malformed_response_hex(text, i, region_end):
                        malformed_response_hex = True
                    i = region_end
                    if i >= n:
                        break
                    i += 1  # consume the comma
                    continue
                val = m.group(0)
                i = m.end()
            # R34 Block 58: keep EVERY duplicate — the old ``params[name] = val``
            # let the last occurrence win, so ``response=<32hex>, response=xyz``
            # (real credential first) ACCEPTED because the overwrite left ``xyz``
            # behind; the reversed order rejected.  Any real response hex must
            # fail closed regardless of order, so no value is dropped.
            params.setdefault(name, []).append(val)
            # OWS then either a comma (more pairs) or the end of the list.
            while i < n and text[i] in " \t":
                i += 1
            if i < n and text[i] == ",":
                i += 1
                continue
            break
        return params, i, malformed_response_hex

    @staticmethod
    def _parse_digest_quoted_value(text: str, i: int) -> tuple[str, int] | None:
        """RFC 7230 quoted-string, tolerant of the ``\"`` form a Digest header
        inside a committed JSON artifact uses.  ``text[i]`` is the opening ``"``
        (or ``\"`` when JSON-escaped).  Returns ``(unescaped_value, index_after
        _closing_quote)`` or ``None`` when the value is unterminated."""
        n = len(text)
        json_escaped = text[i] == "\\"
        i += 2 if json_escaped else 1
        out: list[str] = []
        while i < n:
            ch = text[i]
            if json_escaped:
                if ch == "\\" and i + 1 < n and text[i + 1] == '"':
                    return "".join(out), i + 2
            elif ch == '"':
                return "".join(out), i + 1
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
        return None

    def _digest_credential_spans(self, text: str) -> list[tuple[int, int]]:
        """``(start, end)`` spans of every REAL Digest credential descriptor in
        ``text`` — the keyword through the END of its RFC tokenized parameter
        list (R33 Block 56 producer half).  A credential is exactly what
        ``search()`` accepts (field / standalone / non-algorithm descriptor
        binding + a hex ``response``), so the producer can mask the WHOLE
        descriptor — identity params and a 16/32/64-hex response together —
        before the 0600 seal, never leaving a ``username=`` residue for the
        consumer sanitizer to trip on.  An algorithm-description narration
        (``client digest algorithm=md5, response=<hex>``, Block 45) is NOT a
        credential and is left for the narration masker."""
        spans: list[tuple[int, int]] = []
        for m in _DIGEST_AUTH_KEYWORD_RE.finditer(text):
            binding = self._classify_digest_binding(text, m.start())
            if binding == "none":
                continue
            parsed = self._parse_digest_auth_params(text, m.end())
            if parsed is None:
                continue
            params, end, malformed_response_hex = parsed
            responses = params.get("response") or []
            # R34 Block 58 / R36 Block 62: ANY real response credential hex
            # (16/32/64) in ANY position fails closed — the dict preserves every
            # duplicate, so a hex credential is never hidden behind a later
            # ``response=xyz`` overwrite — and a real response swallowed by a
            # malformed member (``bad="unterminated, response=<hex>``) is a
            # credential too.
            if not (
                malformed_response_hex
                or any(_HEX_FULL_RE.fullmatch(r.strip()) for r in responses)
            ):
                continue
            if binding == "descriptor":
                # R37 Block 66: a malformed-member response is evaluated with
                # the SAME syntax/value determination as a normally-parsed
                # response — an algorithm-description digest (no identity)
                # stays accepted even when the hex was swallowed by a
                # malformed member, and the descriptor only fails closed on a
                # real identity+response structure.
                if _is_digest_identity_params(params):
                    spans.append((m.start(), end))
                    continue
                if "algorithm" in params:
                    continue
            spans.append((m.start(), end))
        return spans

    def search(self, text: str) -> re.Match[str] | None:
        for m in _DIGEST_AUTH_KEYWORD_RE.finditer(text):
            binding = self._classify_digest_binding(text, m.start())
            if binding == "none":
                continue
            parsed = self._parse_digest_auth_params(text, m.end())
            if parsed is None:
                continue
            params, _end, malformed_response_hex = parsed
            responses = params.get("response") or []
            # R34 Block 58: same any-hex-any-position rule as the span builder —
            # a real response credential in ANY duplicate position fails closed.
            # R36 Block 62: a real response swallowed by a malformed member is
            # the same credential signal.
            if not (
                malformed_response_hex
                or any(_HEX_FULL_RE.fullmatch(r.strip()) for r in responses)
            ):
                continue
            # R28 Block 49: a descriptor-prefixed digest is a credential EXCEPT
            # when it is a pure algorithm description — ``client digest
            # algorithm=md5, response=…`` narrates the ALGORITHM (no request
            # identity, no bare hex-secret position) and stays accepted.  A
            # descriptor digest carrying a request-identity parameter (``service
            # Digest username=…`` / ``upstream … userhash=true``) fails closed
            # (R27 Block 45), and so does a descriptor digest with NO identity
            # and NO algorithm — ``origin Digest response=<hex>`` /
            # ``upstream Digest response=<hex>`` are not descriptions of
            # anything, they are the credential form the R27 fix over-relaxed
            # and must fail closed (恢复描述符上下文 Digest 任意 hex 拒绝, 含仅
            # response= 无身份参数形态).
            if binding == "descriptor":
                # R37 Block 66: recovery shares the normal parse's
                # determination — a malformed-member ``response=<hex>`` is the
                # same credential signal as a parsed one (identity or
                # identity-less descriptor with no algorithm fails closed;
                # ``R28 Block 49``), and an algorithm-description digest with
                # no identity stays accepted even when the hex sat inside a
                # malformed member.
                if _is_digest_identity_params(params):
                    return m
                if "algorithm" in params:
                    continue
                return m
            return m
        return None


# R27 Block 45: a scan object, not a compiled pattern — the registry entry and
# the gate's ``.search()`` callers are unchanged.
_SHAPE_PATTERN_DIGEST_AUTH_RE = _DigestAuthScan()

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

# R27 Block 43: a registered business base is exempted ONLY in the FORM its
# schema field documents.  The version-marker fields (``plan.planner_version``
# / ``plan.provider_version`` / ``plan.tokenization_version`` /
# ``plan.secretariat_version``) carry a ``V<digits>`` version marker
# (``plannerV2`` / ``providerV4`` / ``tokenizationV1`` / ``secretariatV1``);
# the index/selector/counter fields (``plan.flight_option`` /
# ``plan.hotel_amenity`` / ``plan.booking_reference`` /
# ``oauth.refresh_token_count`` / ``plan.day``) carry a plain ``<digits>``
# (``flightOption1`` / ``hotelAmenity3`` / ``bookingReference1`` /
# ``refreshTokenCount1`` / ``day2``).  A registered base in the WRONG form — a
# version base without its ``V`` (``planner1`` / ``provider9``) or an index
# base with a ``V`` (``flightOptionV1``) — is NOT the documented schema value
# and fails closed as a bare credential.
_VERSION_MARKER_BUSINESS_BASES = frozenset(
    {
        # plan.planner_version — version marker (R24 Block 36)
        "planner",
        # plan.provider_version — version marker (R24 Block 36)
        "provider",
        # plan.tokenization_version — version marker (R23 Block 33)
        "tokenization",
        # plan.secretariat_version — version marker (R23 Block 33)
        "secretariat",
    }
)

# R27 Block 43: the camelCase shape regex above cannot see the ALL-LOWERCASE
# registered bases (``day`` / ``planner`` / ``provider`` / ``tokenization`` /
# ``secretariat`` — the base itself has no uppercase letter), so a free-text
# run like ``day1 planner1 provider9`` was invisible to the bare-credential
# scan and slipped through.  This recognizer surfaces EVERY registered
# business-identifier base followed by a ``<digits>`` or ``V<digits>`` suffix
# to the form validation in :func:`_is_bare_credential_token`.  It is the
# SAME closed auditable registry — no word list, no threshold.  R28 Block 47
# / R29 Block 50: the recognizer is CASE-INSENSITIVE and covers ALL NINE
# registered bases (``day`` / ``flightOption`` / ``hotelAmenity`` /
# ``bookingReference`` / ``refreshTokenCount`` / ``planner`` / ``provider`` /
# ``tokenization`` / ``secretariat``) — a PascalCase / all-uppercase /
# all-lowercase variant of an index base (``FlightOption7`` /
# ``FLIGHTOPTION1`` / ``flightoption7`` / ``Flightoption7`` /
# ``HotelAmenity1`` / ``BookingReference2`` / ``RefreshTokenCount2``) is the
# same credential-shaped base and fails closed; a capitalized version form
# (``PlannerV2`` / ``PLANNERV2``) is not the documented lowercase
# ``baseV<digits>`` form and fails closed too.
_REGISTERED_LOWER_BASE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:refreshTokenCount|bookingReference|hotelAmenity|tokenization"
    r"|secretariat|flightOption|provider|planner|day)"
    r"(?:V[0-9]+|[0-9]+)(?![A-Za-z0-9_])"
)

# R28 Block 48 / R33 Block 55: credential-NARRATION binding — NOT a flat
# wordlist.  The R27 ``_CREDENTIAL_NARRATION_RE`` was a closed set of STRONG
# words whose mere presence anywhere in a scanned value bound every registered
# token in it; the review found it bypassable (``passphrase is flightOption1`` /
# ``login is flightOption1`` / ``pwd is flightOption1`` / ``the passcode was
# plannerV2`` / ``key: day2`` / ``userpass is day1`` — none of the narration
# words was in the list, so ``narration=False`` and the registered base stayed
# accepted).
#
# The replacement is SYNTACTIC + a closed SEMANTIC CLASS: a credential-
# designation word binds a registered base ONLY when it is in a FIELD-NAME
# position, immediately followed by a binding operator (copula
# ``is/was/...``/``:``/``=``/arrow — or the Chinese copula ``是``/``为``/``等于``/
# ``成为``) and a value that carries a registered-base token (``passphrase is
# flightOption1`` / ``key: day2`` / ``the passcode was plannerV2`` /
# ``pin is plannerV2`` / ``口令是 plannerV2``).  A designation word alone in
# prose never binds — the word has to actually designate a value, so a synonym
# can never silently unbind a registered base (Block 48: 叙述/凭据上下文绑定不得用
# 可绕过的固定词表, 须基于真实字段名/句法解析).  The designation set is closed by
# meaning: every word that NAMES a credential in a field-name position — the
# STRONG field-name words plus ``key``/``login``/``pwd``/``passphrase``/
# ``passcode``/``userpass``/``pin`` and the Chinese credential-designation
# words (``口令`` password / ``密码``/``密碼`` password / ``密钥``/``密鑰`` secret
# key / ``通行码``/``通行碼`` passcode / ``登录``/``登入`` login / ``账号``/``帳號``/
# ``账户``/``帳戶`` account / ``凭证``/``憑證`` credential / ``秘密`` secret) —
# NOT a reviewed-points enumeration.  A composite narration (``password value``
# / ``login password``) is the same designation word followed by a plain
# field-noun modifier and is covered by the optional qualifier suffix
# (``value``/``word``/``name``/``string``/``text``/``number``), so
# ``password value is plannerV2`` fails closed too (R33 Block 55: 禁止逐词补表式
# 打补丁 — 按凭据指称语义类闭合, 含中文与组合叙述).
# R34 Block 57: the designation HEAD class — every word that NAMES a credential
# in a field-name position (R33 Block 55), including the compound key/token/
# secret/authorization forms and the Chinese designations.  ``pass`` /
# ``access`` are deliberately NOT here — see ``_CREDENTIAL_DESIGNATION_PREFIX_ALT``.
_CREDENTIAL_DESIGNATION_STANDALONE_ALT = (
    r"(?:password|passwd|passphrase|passcode|pwd|userpass|login|key|secret"
    r"|pin|token|credentials?|api[_-]?key|access[_-]?key|session[_-]?key"
    r"|private[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|auth[_-]?token|session[_-]?token|client[_-]?secret|secret[_-]?key"
    r"|authorization|proxy[_-]?authorization|bearer|auth"
    r"|口令|密码|密碼|密钥|密鑰|通行码|通行碼|登录|登入|账号|帳號|账户|帳戶"
    r"|凭证|憑證|秘密"
    r")"
)
# R34 Block 57: ``pass`` / ``access`` are the English ROOT / access-grant words
# of the designation class (pass = the root of password/passphrase/passcode;
# access = the access-grant credential) but are ambiguous ALONE, so they bind
# only inside a composite noun phrase (``pass phrase`` / ``access code``).
_CREDENTIAL_DESIGNATION_PREFIX_ALT = r"pass|access"
# R34 Block 57: the composite MODIFIER class — a noun that denotes the content
# or value of a field (value/word/name/string/text/number/code/phrase) plus the
# designation words themselves (``login password`` / ``password passcode``).
_CREDENTIAL_DESIGNATION_MODIFIER_ALT = (
    r"(?:value|word|name|string|text|number|code|phrase"
    r"|" + _CREDENTIAL_DESIGNATION_STANDALONE_ALT + r")"
)
# R34 Block 57: the Chinese content-noun class — concatenated, no space, onto a
# Chinese designation head (``口令值`` / ``口令密码``).  Same semantic closure as
# the English modifier class; ``是/为/等于/成为`` are copulas, not content nouns.
_CREDENTIAL_DESIGNATION_CJK_NOUN_ALT = (
    r"(?:值|词|詞|名|号|號|码|碼|短语|短語|文本|数字|數字"
    r"|口令|密码|密碼|密钥|密鑰|通行码|通行碼|登录|登入|账号|帳號|账户|帳戶"
    r"|凭证|憑證|秘密"
    r")"
)
# R34 Block 57: the composite designation is a noun PHRASE — a designation head
# optionally followed by 0-2 spaced value/field nouns (``password value`` /
# ``login password`` / ``pin code`` / ``access code`` / ``pass phrase``), plus
# the Chinese content nouns that concatenate onto a Chinese head with no space
# (``口令值`` / ``口令密码``).  The modifier classes are closed BY MEANING (a noun
# that denotes the CONTENT of a field), so ``code`` / ``phrase`` / ``值`` bind
# exactly like the old fixed qualifier list and the reviewer's composites are
# instances of the class — not words added one-by-one (R33 Block 55: 禁止逐词补表
# 式打补丁 — 按凭据指称语义类闭合, 含中文与组合叙述).  ``pass`` / ``access`` bind
# ONLY when compounded with a content noun (``pass phrase`` / ``access code`` /
# ``access key``); bare ``pass is …`` / ``access is …`` are ordinary business
# prose and never bind, so the added words cannot flip narration onto a plain
# sentence.
_CREDENTIAL_DESIGNATION_ALT = (
    r"(?:" + _CREDENTIAL_DESIGNATION_STANDALONE_ALT + r")"
    r"(?:[ \t]+(?:" + _CREDENTIAL_DESIGNATION_MODIFIER_ALT + r")){0,2}"
    r"(?:" + _CREDENTIAL_DESIGNATION_CJK_NOUN_ALT + r"){0,2}"
    r"|"
    r"(?:" + _CREDENTIAL_DESIGNATION_PREFIX_ALT + r")"
    r"[ \t]+(?:" + _CREDENTIAL_DESIGNATION_MODIFIER_ALT + r")"
    r"(?:[ \t]+(?:" + _CREDENTIAL_DESIGNATION_MODIFIER_ALT + r"))?"
)
# R29 Block 51 / R30 Block 52 / R31 Block 53 / R32 Block 54: the binding-operator
# arrow class is CLOSED BY UNICODE ARROW BLOCK RANGE plus a principled
# rightward-arrow FAMILY closure, not by glyph enumeration.  NFKC leaves every
# arrow codepoint unchanged (=> U+21D2 / ⟶ U+27F6 / ⤳ U+2933 / ➔ U+2794 /
# ⇛ U+21DB / ⭢ U+2B62 / 🡂 U+1F802 ...), so normalizing the scanned text can never
# collapse them - the operator set itself must accept the whole family.  R29-R31
# closed all SIX Unicode arrow blocks (Arrows U+2190-U+21FF, Supplemental Arrows-A
# U+27F0-U+27FF, Supplemental Arrows-B U+2900-U+297F, Dingbats U+2794-U+27BF,
# Misc Symbols & Arrows U+2B00-U+2BFF, Supplemental Arrows-C U+1F800-U+1F8FF) and
# 8 scattered rightward codepoints (U+0362 / U+2348 / U+1F4F2 / U+1F500-U+1F502 /
# U+1F51C / U+1FBB6), but Block 54 review found the closure was STILL a
# reviewed-points enumeration: 17 same-family rightward/bidirectional
# arrow/arrowhead codepoints OUTSIDE those blocks (U+02C3 / U+02F2 / U+034D /
# U+0350 / U+0355 / U+0356 / U+08F8-U+08FD / U+1DFF / U+20D7 / U+20E1 / U+20EF /
# U+29B3 / U+1F51B - combining marks, modifier letters, Arabic variants and an
# enclosed bidirectional arrow) escaped both finals, e.g. U+0362 was closed while
# same-block same-family U+034D / U+0350 stayed open.  The class now ALSO closes
# the whole out-of-block rightward / bidirectional ARROW / ARROWHEAD family by
# name semantics (:func:`_rightward_arrow_family_ranges`): any codepoint whose
# Unicode name marks a RIGHT / RIGHTWARDS / LEFT-RIGHT arrow or arrowhead binds,
# plus the Arabic arrowhead sub-family (U+08F7-U+08FD).  Over-inclusion here only
# ever binds (fail-closed) - a designation word + arrow + registered-base value is
# credential narration regardless of which arrow glyph the author typed; ordinary
# RIGHT-POINTING symbols (triangles / quotes / brackets / magnifier: U+25B6-25BB,
# U+23E9-23F5, U+00BB, U+203A, U+232A, U+1F50E, U+10878) are NOT arrow glyphs and
# stay non-binding.
def _rightward_arrow_family_ranges() -> str:
    """Regex-range escapes for every out-of-block rightward / bidirectional arrow
    or arrowhead in the Unicode database (Block 54: close the FAMILY, not the
    codepoints a review happens to name).  A codepoint is in the family when its
    name marks an ARROW/ARROWHEAD glyph with a rightward or bidirectional
    direction (``RIGHT`` / ``RIGHTWARDS`` / ``LEFT RIGHT``), or is an Arabic
    arrowhead (U+08F7-U+08FD sub-family).  Codepoints already inside the six
    closed arrow blocks or the 8 scattered codepoints are skipped (the static
    ranges below already cover them)."""
    blocks = (
        (0x2190, 0x21FF), (0x27F0, 0x27FF), (0x2900, 0x297F), (0x2794, 0x27BF),
        (0x2B00, 0x2BFF), (0x1F800, 0x1F8FF),
    )
    scattered = frozenset(
        (0x0362, 0x2348, 0x1F4F2, 0x1F500, 0x1F501, 0x1F502, 0x1F51C, 0x1FBB6)
    )

    def closed(cp: int) -> bool:
        return cp in scattered or any(lo <= cp <= hi for lo, hi in blocks)

    family: list[int] = []
    for cp in range(0x110000):
        name = unicodedata.name(chr(cp), "")
        if not name or "ARROW" not in name or closed(cp):
            continue
        if (
            "RIGHT" in name
            or "RIGHTWARDS" in name
            or "LEFT RIGHT" in name
            or ("ARABIC" in name and "ARROWHEAD" in name)
        ):
            family.append(cp)
    # Collapse the ascending codepoints into contiguous ranges.
    parts: list[str] = []
    start = prev = family[0]
    for cp in [*family[1:], None]:
        if cp is None or cp != prev + 1:
            if start == prev:
                parts.append(
                    rf"\u{start:04X}" if start < 0x10000
                    else rf"\U{start:08X}"
                )
            else:
                parts.append(
                    rf"\u{start:04X}-\u{prev:04X}"
                    if prev < 0x10000
                    else rf"\U{start:08X}-\U{prev:08X}"
                )
            if cp is None:
                break
            start = cp
        prev = cp
    return "".join(parts)


_UNICODE_ARROW_BLOCK_CLASS = (
    r"[\u2190-\u21FF\u27F0-\u27FF\u2900-\u297F\u2794-\u27BF"
    r"\u2B00-\u2BFF\U0001F800-\U0001F8FF"
    r"\u0362\u2348\U0001F4F2\U0001F500-\U0001F502\U0001F51C\U0001FBB6"
    + _rightward_arrow_family_ranges()
    + "]")

_CREDENTIAL_NARRATION_BIND_OP = (
    r"(?:is|was|were|are|equals?|becomes)\b[ \t]*"
    r"|[:=]|" + _UNICODE_ARROW_BLOCK_CLASS + r"|->|=>"
    # Chinese copula / equality (R33 Block 55): 是 (is), 为 (is/equals), 等于
    # (equals), 成为 (becomes).  No ``\b`` — CJK has no ASCII word boundary, so
    # ``口令是plannerV2`` binds exactly like ``口令是 plannerV2``.
    r"|(?:是|为|等于|成为)[ \t]*"
)
# A designation word in FIELD-NAME position immediately followed by a binding
# operator and the bound value.  ``value`` is bounded and stops at a structural
# delimiter (``,;{}`` / line end) — the registered-base token must sit inside it.
_CREDENTIAL_NARRATION_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(" + _CREDENTIAL_DESIGNATION_ALT + r")"
    r"[ \t]*(?:" + _CREDENTIAL_NARRATION_BIND_OP + r")[ \t]*"
    r"(?P<value>[^\r\n,;{}]{0,120})"
)


def _is_business_identifier_token(token: str) -> bool:
    """True when ``token`` is a REGISTERED TripChord business identifier in its
    DOCUMENTED schema form.  R28 Block 47: ONLY the version-marker bases
    (``planner`` / ``provider`` / ``tokenization`` / ``secretariat``) keep the
    business-identifier exemption, and only as ``baseV<digits>`` (``plannerV2``
    / ``tokenizationV1``) — the ``V<digits>`` marker is a real, auditable
    structural feature.  The index/selector/counter bases (``day`` /
    ``flightOption`` / ``hotelAmenity`` / ``bookingReference`` /
    ``refreshTokenCount``) are NOT exempted in ANY free-text context: ``day1``
    / ``flightOption7`` / ``hotelAmenity1`` / ``bookingReference2`` /
    ``refreshTokenCount2`` alone or wrapped in ``{"summary": …}`` fail closed.
    The claimed field paths (``plan.day`` / ``plan.flight_option[].option`` /
    ``plan.hotel_amenity[].amenity`` / ``plan.booking_reference`` /
    ``oauth.refresh_token_count``) appear 0 times in the actual codebase, so the
    "documented schema form" exemption has no auditable provenance and every
    such bare token is treated as a credential-shaped value.  A token that is
    not a registered base (``qwerTy1`` / ``myFlightHotel1``), or a version base
    in the wrong form (``planner1`` / ``provider9`` / ``flightOptionV1``), is
    NOT a business identifier."""
    if re.search(r"[0-9]+[A-Za-z]", token):
        return False
    m = re.match(r"^([A-Za-z]+)([0-9]+)$", token)
    if m is None:
        return False
    prefix, _digits = m.group(1), m.group(2)
    base = prefix[:-1] if prefix[-1] in "Vv" else prefix
    if base not in _VERSION_MARKER_BUSINESS_BASES:
        # R28 Block 47: index/selector/counter bases and unknown bases fail
        # closed in every free-text context — the exemption is only ever the
        # version-marker ``baseV<digits>`` form.
        return False
    # version-marker base: valid only as ``baseV<digits>``
    return prefix[-1] in "Vv" and prefix[:-1] == base


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
    * a trailing-digit token whose base is EXACTLY a registered version-marker
      ``_VERSION_MARKER_BUSINESS_BASES`` entry in its documented ``baseV<digits>``
      form (``plannerV2`` / ``tokenizationV1`` / ``secretariatV1``) is a
      business identifier (R28 Block 47: ONLY the version-marker bases keep the
      exemption — the ``V<digits>`` marker is auditable structure).  The
      index/selector/counter bases (``flightOption1`` / ``hotelAmenity3`` /
      ``bookingReference1`` / ``refreshTokenCount1`` / ``day2``) fail closed in
      every free-text context, as do ``planner1`` / ``provider9`` /
      ``flightOptionV1`` (wrong form);
    * ANY other digit-bearing camelCase token (``qwerTy1`` /
      ``myFlightHotel1`` / ``purpleMonkeyDishwasher1``) fails closed as a
      bare credential shape.
    """
    if re.search(r"[0-9]+[A-Za-z]", token):
        return True
    return not _is_business_identifier_token(token)


class _BareCredentialScan:
    """Final-scan ``bare_credential_value`` backstop as a drop-in ``.search``-able
    object: iterates EVERY word-bounded camelCase-and-digit token AND every
    registered all-lowercase base token (``day2`` / ``planner1``) and rejects
    it via :func:`_is_bare_credential_token` (R22 Block 28/32 restore the
    any-digit contract while keeping business values positive).  R27 Block 43 /
    R28 Block 48: a registered business identifier is exempted ONLY outside a
    credential-NARRATION context — a SYNTACTIC designation assignment binding a
    registered base (``password is flightOption1`` / ``key: day2`` / ``the
    passcode was plannerV2``) fails the registered token closed."""

    def search(self, text: str) -> re.Match[str] | None:
        narration = _credential_narration_binds(text)
        for m in _BARE_CREDENTIAL_TOKEN_RE.finditer(text):
            if _is_bare_credential_token(m.group(0)):
                return m
            if narration:
                return m
        for m in _REGISTERED_LOWER_BASE_RE.finditer(text):
            if _is_bare_credential_token(m.group(0)):
                return m
            if narration:
                return m
        return None


_SHAPE_PATTERN_BARE_CREDENTIAL_VALUE_RE = _BareCredentialScan()


def _mask_digest_credential_text(text: str) -> str:
    """Mask every REAL Digest credential descriptor (the keyword through the end
    of its RFC-tokenized parameter list) to ``[REDACTED]`` — the producer half of
    R33 Block 56.  The token-run masker only collapses a 32+ response, so a
    16-hex ``response`` and the identity params beside it (``username="user"``)
    could otherwise reach the sealed diagnostic, where the consumer sanitizer
    sees ``username`` as a credential field name and trips.  Masking the WHOLE
    descriptor fails the credential closed on the producer side too; the
    algorithm-description narrations (``client digest algorithm=md5,
    response=…``) are not credentials and stay untouched (Block 45)."""
    spans = _SHAPE_PATTERN_DIGEST_AUTH_RE._digest_credential_spans(text)
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
        result.append(text[last:start])
        result.append("[REDACTED]")
        last = end
    result.append(text[last:])
    return "".join(result)


def _mask_bare_credential_text(text: str) -> str:
    """R27 Block 43: the producer/consumer free-text bare-credential masker —
    masks every camelCase-and-digit token and every registered all-lowercase
    base token that is (a) NOT a registered business identifier in its
    documented schema form (``qwerTy1`` / ``myFlightHotel1`` / ``planner1`` /
    ``provider9``), or (b) a registered business identifier sitting in a
    credential-NARRATION context (``password is flightOption1`` / ``key: day2``
    / ``the passcode was plannerV2``).  R28 Block 47: the index/selector/counter
    bases are NOT business identifiers in free text anymore — ``day2`` /
    ``flightOption1`` / ``refreshTokenCount1`` are masked here too — so only the
    version-marker business identifiers in their documented schema form with no
    narration (``plannerV2 providerV4`` / ``tokenizationV1``) are left
    untouched.  R33 Block 55: the narration test runs on the NORMALIZED copy
    (NFKC folds a full-width colon / equals to ``:``/``=``) so a full-width
    Chinese copula or colon binds a registered base the same way the final scan
    sees it (``口令 + full-width-colon + plannerV2`` -> ``口令 +
    [REDACTED]``); R33 Block 56: a real Digest credential descriptor is masked
    WHOLE first (16/32/64-hex response and its identity params)."""
    text = _mask_digest_credential_text(text)
    narration = _credential_narration_binds(_normalize_for_scan(text))

    def repl(m: re.Match[str]) -> str:
        if _is_bare_credential_token(m.group(0)):
            return "[REDACTED]"
        if narration:
            return "[REDACTED]"
        return m.group(0)

    masked = _BARE_CREDENTIAL_TOKEN_RE.sub(repl, text)
    return _REGISTERED_LOWER_BASE_RE.sub(repl, masked)


# R27 Block 43: a registered business-identifier BASE is a VALUE at a
# documented schema/field-path (``plan.day`` = ``day2``,
# ``plan.flight_option`` = ``flightOption1``), never a field NAME.  A
# registered base used as a JSON object KEY or an HTTP/header field name
# (``{"day1": …}`` / ``X-Day1: …`` / ``plannerV2: …``) is a credential-shaped
# field with NO schema/field-path binding and must fail closed — the closed
# registry grants the exemption only to the documented VALUE position
# (supervision Block 43).  ``_REGISTERED_BASE_ALT`` is the same closed auditable
# base set, longest-first so ``refreshTokenCount1`` never leaves a ``Token`` /
# ``Count`` residue for a shorter base to steal.
_REGISTERED_BASE_ALT = "|".join(
    re.escape(base)
    for base in sorted(_BUSINESS_IDENTIFIER_BASES, key=len, reverse=True)
)
# A registered base followed by its ``<digits>`` / ``V<digits>`` suffix, in ANY
# digit form (``planner1`` is as credential-shaped a key as ``plannerV2``), at
# a word boundary on both sides — case-insensitive so ``Day1`` / ``DAY1`` keys
# are the same field name once NFKC + casefold runs.
_REGISTERED_BASE_KEY_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:" + _REGISTERED_BASE_ALT + r")"
    r"(?:V[0-9]+|[0-9]+)(?![A-Za-z0-9_])"
)
# A registered base used as a HEADER / free-form field NAME: the base is the
# first name segment (optionally after an ``X-`` / ``Foo-`` / ``my_`` name
# prefix), carries the ``<digits>``/``V<digits>`` suffix, then a ``:``/``=``
# field separator.  ``day1: …`` / ``X-Day1: …`` / ``plannerV2: …`` are all
# credential-shaped field names and fail closed on BOTH final paths; a plain
# prose value (``day2`` / ``flightOption1 day2 plannerV2 providerV4``) has no
# ``:``/``=`` after the base and stays positive.
_REGISTERED_BASE_HEADER_FIELD_RE = re.compile(
    r"(?im)(?:^|[ \t\r\n,;{}])[ \t]*(?:[A-Za-z0-9_-]+[ \t]*[-_][ \t]*)?"
    r"(?:" + _REGISTERED_BASE_ALT + r")(?:V[0-9]+|[0-9]+)[ \t]*[:=]"
)


# R28 Block 48: the value a credential-designation word must bind — a registered
# business-identifier base token in ANY digit form (``day2`` / ``flightOption1``
# / ``plannerV2`` / ``planner1``), word-bounded.  Used by
# :func:`_credential_narration_binds` to decide whether a designation assignment
# is a credential-narration context.
_REGISTERED_BASE_TOKEN_IN_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:" + _REGISTERED_BASE_ALT + r")"
    r"(?:V[0-9]+|[0-9]+)(?![A-Za-z0-9_])"
)

# R36 Block 61: the exact registered-base VALUE form parser — a business
# identifier value is ``<base><V<digits>|<digits>>`` (``plannerV2`` /
# ``flightOption1``), matched on the WHOLE value with complete boundaries so a
# phrase (``plannerV2 providerV4``) or a non-base token (``qwerTy1``) is not an
# exact base value.  Returns ``(base_lower, is_version_form)`` or ``None``.
_REGISTERED_BASE_VALUE_RE = re.compile(
    r"(?i)^(?P<base>(?:" + _REGISTERED_BASE_ALT + r"))"
    r"(?P<suffix>V[0-9]+|[0-9]+)$"
)

# R37 Block 65: the exact-base PREFIX (no trailing ``$``) used to inspect what
# immediately follows an opener in the illegal-structure branch — a base then a
# WRONG closer or nothing (``(plannerV2]`` / ``[plannerV2``) is illegal
# structure, a base then its MATCHING closer and prose (``(plannerV2) in the
# report``) is a phrase, and a base then a word char (``(plannerV2plus)``) is a
# longer non-base token.
_REGISTERED_BASE_VALUE_PREFIX_RE = re.compile(
    r"(?i)(?:" + _REGISTERED_BASE_ALT + r")(?:V[0-9]+|[0-9]+)"
)


# R37 Block 65: the generic STRUCTURAL wrapper PAIRS a JSON/prose value uses
# around an exact registered base — ``(plannerV2)`` / ``[plannerV2]`` /
# ``{plannerV2}`` / ``<plannerV2>`` / ``'plannerV2'`` / ``"plannerV2"``.  A value
# that OPENS with a wrapper character is a structural appearance: the wrapper is
# a tolerable mask around the base only when it closes with its own PAIR.  A
# missing or MISMATCHED closer (``(plannerV2]`` / ``[plannerV2``) is an ILLEGAL
# STRUCTURAL APPEARANCE and must FAIL CLOSED (Block 65: 非法结构外观 一律
# fail-closed), not read as a non-base phrase — 禁止继续枚举分隔符/括号, so the
# wrapper set is the paired closure, not an enumerable list.
#
# R38 Block 67: the closure is now the generic Unicode Ps/Pe pair set PLUS the
# self-pair quote / apostrophe / backtick — every PS code point whose Unicode
# name mirrors a Pe ("LEFT X" -> "RIGHT X") is paired by NAME, not enumerated,
# so a CJK corner bracket (``「plannerV2」`` / ``【plannerV2】`` / ``《plannerV2》``),
# a full-width bracket (``（plannerV2）`` / ``［plannerV2］`` / ``｛plannerV2｝``)  # noqa: RUF003
# or a backtick (``\`plannerV2\```) wrapper is recognized by STRUCTURE, and any
# future Ps/Pe pair is covered without a table edit (supervision: 按真实结构 +
# 通用 Unicode 配对包装解析, 禁止继续补字符表).
def _build_wrapper_pairs() -> dict[str, str]:
    pairs: dict[str, str] = {
        "(": ")",
        "[": "]",
        "{": "}",
        "<": ">",
        '"': '"',
        "'": "'",
        "`": "`",
    }
    # Generic Unicode paired closure by STRUCTURE (R38 Block 67 —
    # 禁止继续补字符表, never a hand-maintained table).  Two rules cover the
    # whole BMP+ range in one import-time scan (≈0.2s):
    #   1. NAME mirroring — every ``LEFT``-named opener (category Ps or Pi)
    #      closes with the ``RIGHT``-named closer (category Pe or Pf) of the
    #      SAME name.  This pairs the Ps/Pe bracket families AND the
    #      LEFT/RIGHT quotation marks (``“”`` U+201C/U+201D, ``‘’``  # noqa: RUF003
    #      U+2018/U+2019, ``«»`` U+00AB/U+00BB, ``‹›`` U+2039/U+203A) and the  # noqa: RUF003
    #      substitution / transposition / omission brackets — the Pi/Pf scan
    #      is what the old Ps/Pe-only scan missed.
    #   2. CODE-POINT adjacency — an opener that is NOT LEFT/RIGHT-named
    #      closes with the code point exactly at ``cp+1`` when that is an
    #      unclaimed close-category (Pe/Pf) character.  The pairs the Unicode
    #      standard assigns for these families ARE adjacent: Tibetan
    #      ``༺༻`` U+0F3A/U+0F3B + ``༼༽`` U+0F3C/U+0F3D, Ogham ``᚛᚜``
    #      U+169B/U+169C, the arc brackets U+2993/U+2994 + U+2995/U+2996, the
    #      double-prime quotes U+301D/U+301E, and the vertical lenticular
    #      brackets U+FE17/U+FE18.
    close_by_name: dict[str, str] = {}
    open_chars: list[tuple[str, str]] = []
    for cp in range(0x110000):
        name = unicodedata.name(chr(cp), "")
        if not name:
            continue
        cat = unicodedata.category(chr(cp))
        if cat in ("Pe", "Pf"):
            close_by_name[name] = chr(cp)
        elif cat in ("Ps", "Pi"):
            open_chars.append((name, chr(cp)))
    for name, ch in open_chars:
        if "LEFT" in name:
            closer = close_by_name.get(name.replace("LEFT", "RIGHT", 1))
            if closer is not None:
                pairs[ch] = closer
    used_close = set(dict.fromkeys(pairs.values()))
    for _name, ch in open_chars:
        if ch in pairs:
            continue
        nxt = chr(ord(ch) + 1)
        if unicodedata.category(nxt) in ("Pe", "Pf") and nxt not in used_close:
            pairs[ch] = nxt
            used_close.add(nxt)
    return pairs


_WRAPPER_PAIR: dict[str, str] = _build_wrapper_pairs()
# Char classes for the open/close groups of the exact-assignment regex, built
# from the same paired closure so a wrapper char is never hard-coded twice.
_WRAPPER_OPEN_CLASS = "[" + "".join(re.escape(c) for c in _WRAPPER_PAIR) + "]"
_WRAPPER_CLOSE_CLASS = "[" + "".join(
    re.escape(c) for c in dict.fromkeys(_WRAPPER_PAIR.values())
) + "]"
# Every opener and closer of the paired closure, as a plain char set — the
# digest response-value wrapper strip (R38 Block 68) uses it, and it is defined
# after ``_WRAPPER_PAIR`` so the earlier digest helpers resolve it at call time.
_DIGEST_VALUE_WRAPPER_CHARS = frozenset(
    dict.fromkeys([*_WRAPPER_PAIR, *_WRAPPER_PAIR.values()])
)
# R40 Block 71: the CLOSER chars of the paired closure (a bounded-stack pair
# matcher resolves each closer against the TOP of its stack), and the
# SELF-PAIRING quote / apostrophe / backtick — an unclosed SELF-PAIRING residue
# is the R39 JSON string delimiter, never an illegal wrapper.
_WRAPPER_CLOSE_CHARS = frozenset(dict.fromkeys(_WRAPPER_PAIR.values()))
_SELF_PAIRING_QUOTES = frozenset(ch for ch, closer in _WRAPPER_PAIR.items() if closer == ch)
# The BOUNDED structural wrapper-strip depth shared by every wrapper resolver
# (R39 Block 69 / R38 Block 68): ``_registered_base_value_info`` and
# ``_digest_response_hex_value`` strip at most this many nested wrapper layers
# before the value is too structurally deep to be a legitimate phrase — a value
# whose budget is exhausted while a wrapper layer is still open fails CLOSED
# (never falls back to reading the wrapper residue as a non-base phrase).  The
# bound is an internal DoS/structure limit, not an enumerated layer count.
_STRUCTURAL_WRAPPER_DEPTH_LIMIT = 8
# Sentinel returned by :func:`_registered_base_value_info` for an ILLEGAL
# STRUCTURAL wrapper (opener present, matching pair absent) — callers fail
# closed (never exempt) on it.
_WRAPPED_BASE_ILLEGAL = "WRAPPED_BASE_ILLEGAL"


def _registered_base_value_info(value: str) -> tuple[str, bool] | str | None:
    """Resolve ``value`` to ``(base_lower, is_version_form)`` when it is an
    EXACT registered base value.  R36 Block 63: a balanced ``(...)`` /
    ``'...'`` / ``"..."`` wrapper around the exact base (``{"otp":
    "(plannerV2)"}`` — a JSON string or prose parenthetical wrapping the
    value) is unwrapped before the match, so an UNBOUND path carrying a
    wrapped exact base still fails closed instead of reading as a non-base
    phrase.  R37 Block 65: the wrapper set is the generic structural PAIRS
    ``()`` / ``[]`` / ``{}`` / ``<>`` / ``""`` / ``''``, and a value that
    OPENS with a wrapper character must close with its PAIR — a missing or
    mismatched closer (``(plannerV2]`` / ``[plannerV2``) returns the
    ``_WRAPPED_BASE_ILLEGAL`` sentinel so the caller fails closed.  A phrase
    that merely mentions a base (``"see (tokenizationV1)"``) or opens with a
    parenthetical base then continues (``"(tokenizationV1) in the report"``)
    is unchanged and stays a non-credential.  R40 Block 71: a value that
    opens with a wrapper char is parsed by a REAL bounded-stack pair matcher —
    every opener is pushed, every closer must close the TOP of the stack
    (LIFO), and a cross-mismatched closer (``([plannerV2)]`` /
    ``【(plannerV2】)``), an unclosed opener (``([plannerV2]`` /
    ``(plannerV2``), or a nesting deeper than the shared structural bound is an
    ILLEGAL structural appearance that fails closed — never read back as a
    phrase.  The only structural exception is the R39 JSON-string delimiter: a
    SELF-PAIRING quote / apostrophe / backtick still unclosed at the end
    (``"plannerV2`` — ``"summary":"plannerV2 providerV4"``) is the delimiter
    that opens a LONGER value, so the exact base right after it still binds."""
    v = value.strip()
    # A value that does NOT open with a wrapper char is not a structural
    # wrapper appearance — resolve the exact base or a phrase directly.
    if not v or v[0] not in _WRAPPER_PAIR:
        m = _REGISTERED_BASE_VALUE_RE.match(v)
        if m is None:
            return None
        return m.group("base").lower(), m.group("suffix").startswith("V")
    # Real bounded-stack pair matcher (R40 Block 71): consume the leading
    # openers first (inner whitespace between wrapper layers is tolerated).
    stack: list[str] = []
    i = 0
    n = len(v)
    while i < n and v[i] in _WRAPPER_PAIR:
        if len(stack) >= _STRUCTURAL_WRAPPER_DEPTH_LIMIT:
            # R39 Block 69: the strip budget is exhausted while a wrapper layer
            # is STILL open (nesting deeper than the bound) — fail CLOSED
            # instead of reading the residue as a non-base value.
            return _WRAPPED_BASE_ILLEGAL
        stack.append(v[i])
        i += 1
        while i < n and v[i].isspace():
            i += 1
    m = _REGISTERED_BASE_VALUE_PREFIX_RE.match(v, i)
    if m is None:
        # no exact base right after the openers — a phrase, never a credential
        return None
    exact = _REGISTERED_BASE_VALUE_RE.match(m.group(0))
    if exact is None:
        return None
    base = exact.group("base").lower()
    is_version = exact.group("suffix").startswith("V")
    base_end = m.end()
    # a word char immediately after the base is a longer non-base token
    # (``(plannerV2plus)``) — a phrase, not an exact value
    if base_end < n and (v[base_end].isalnum() or v[base_end] == "_"):
        return None
    j = base_end
    while j < n and v[j].isspace():
        j += 1
    if j < n and v[j] not in _WRAPPER_CLOSE_CHARS:
        # content between the base and any closer — the wrapped value is a
        # phrase (``(plannerV2-1)`` / ``(plannerV2 providerV4)``)
        return None
    while j < n and v[j] in _WRAPPER_CLOSE_CHARS:
        if not stack:
            # a dangling closer after a fully balanced wrapper — prose
            return None
        if _WRAPPER_PAIR[stack[-1]] != v[j]:
            # cross-mismatched closer (``(plannerV2]`` / ``([plannerV2)]`` /
            # ``【(plannerV2】)``) — ILLEGAL structural appearance
            return _WRAPPED_BASE_ILLEGAL
        stack.pop()
        j += 1
        while j < n and v[j].isspace():
            j += 1
    if stack:
        # unclosed openers at end of text.  R39: a SELF-PAIRING quote /
        # apostrophe / backtick residue is the JSON string delimiter that opens
        # a LONGER value (``"plannerV2``) — the exact base right after it still
        # binds; any other unclosed opener (``(plannerV2`` / ``([plannerV2]``)
        # is an ILLEGAL structural appearance.
        if all(ch in _SELF_PAIRING_QUOTES for ch in stack):
            return base, is_version
        return _WRAPPED_BASE_ILLEGAL
    if j < n:
        # trailing prose after a fully balanced wrapper
        # (``(tokenizationV1) in the report``) — a phrase
        return None
    return base, is_version


# R36 Block 61: the exact JSON member-key PATHS where a registered business
# identifier is the DOCUMENTED value position.  Path keys are the exact
# member-key path tuple (a list index collapses to the ``[]`` placeholder); the
# value is the closed set of base names allowed at that path.  The
# version-marker schema fields (``planner_version`` = ``plannerV2`` …,
# optionally under the documented ``plan.`` prefix) and the free-form
# diagnostic fields (``summary`` / ``detail`` / ``reason``) are documented; the
# index/selector/counter fields (``plan.day`` / ``plan.flight_option`` /
# ``plan.hotel_amenity`` / ``plan.booking_reference`` /
# ``oauth.refresh_token_count``) are NOT — R27 Block 43 fail-closes them even
# at the VALUE position of their own declared schema field (``{"day":
# "day2"}``).  The R35 prefix regex (``[\w\u4e00-\u9fff.-]*``) that let
# ``evilplanner_version`` / ``foo.planner_version`` reach the exemption is gone
# — a member key is matched COMPLETE, so only the exact documented paths grant
# the exemption (``evilplanner_version`` is the full field name, never a
# prefix-stripped ``planner_version``).
_DOCUMENTED_BUSINESS_VALUE_PATHS: dict[tuple[str, ...], frozenset[str]] = {
    # Free-form diagnostic fields carry version-marker business identifiers
    # (R21 Block 25 / R27 Block 43 positives ``{"summary":
    # "tokenizationV1"}``); an index/selector base inside them
    # (``{"summary": "flightOption1 day2 plannerV2 providerV4"}``) still fails
    # closed because the base is not in the allowed set.
    ("summary",): frozenset(_VERSION_MARKER_BUSINESS_BASES),
    ("detail",): frozenset(_VERSION_MARKER_BUSINESS_BASES),
    ("reason",): frozenset(_VERSION_MARKER_BUSINESS_BASES),
}
for _base in sorted(_VERSION_MARKER_BUSINESS_BASES):
    _version_field = _base + "_version"
    _DOCUMENTED_BUSINESS_VALUE_PATHS[(_version_field,)] = frozenset((_base,))
    _DOCUMENTED_BUSINESS_VALUE_PATHS[("plan", _version_field)] = frozenset(
        (_base,)
    )
del _base, _version_field


def _registered_base_value_exempt_at_path(
    path: tuple[str, ...], value: str
) -> bool:
    """True when the exact registered-base ``value`` may sit at the JSON
    member-key ``path`` without being a credential (R36 Block 61): either it is
    not an exact registered-base value at all (a phrase / non-base token —
    nothing to exempt), or the path is DOCUMENTED and its allowed base set
    contains the value's base.  A base at any other path (``("otp",)`` +
    ``plannerV2``), a cross-field value (``("planner_version",)`` +
    ``providerV4``) and a fake-suffix field (``("evilplanner_version",)`` +
    ``plannerV2``) all return False and fail closed."""
    info = _registered_base_value_info(value)
    if info is None:
        return True
    if info is _WRAPPED_BASE_ILLEGAL:
        return False
    base, _is_version = info
    allowed = _DOCUMENTED_BUSINESS_VALUE_PATHS.get(path)
    if allowed is None:
        return False
    return base in allowed


# R35 Block 59: the free-text business-identifier exemption (a registered base
# like ``plannerV2`` in a VALUE position) is bound to the DOCUMENTED structured
# schema/field paths — the version-marker fields ``plan.planner_version`` /
# ``plan.provider_version`` / ``plan.tokenization_version`` /
# ``plan.secretariat_version`` (R24 Block 36).  A credential-STYLE assignment of
# an EXACT registered base to an ARBITRARY free-text field name (``OTP code is
# plannerV2`` / ``verification code is plannerV2`` / ``验证码是plannerV2`` /
# ``口令内容是plannerV2``) is an assignment narration at an UNBOUND path and
# fails closed REGARDLESS of the designation word — NO head/modifier/CJK word
# list is involved (supervision Block 59: 禁止继续补 head/modifier/CJK 词表或语言
# 样例).  The designation semantic class cannot name a new synonym, so the
# closure is STRUCTURAL: a bind operator + an EXACT registered-base value, with
# the surrounding ``"`` / ``'`` / ``(`` wrappers a JSON field or prose
# parenthetical uses (``{"planner_version": "plannerV2"}`` /
# ``verification code is 'plannerV2'`` / ``verification code is (plannerV2)``)
# tolerated — R36 Block 61 extends the wrapper class from ``\"?`` to all three.
# R37 Block 65: the wrapper is now the generic structural PAIR set ``()`` /
# ``[]`` / ``{}`` / ``<>`` / ``""`` / ``''`` (``verification code is
# [plannerV2]``), and the field continuation is a PRINCIPLED NEGATIVE class —
# every structural separator (``\`` / ``::`` / ``/`` / ``.`` / ``[`` …) is a
# legal path character, so no separator is ever enumerated.
# ``evil\planner_version = plannerV2`` / ``evil::planner_version = plannerV2``
# are one complete field and fail closed; a missing/mismatched wrapper pair
# (``code is [plannerV2`` / ``code is (plannerV2]``) is an illegal structural
# appearance and fails closed.
# ``access is granted to plannerV2 users`` (value is a phrase, not the exact
# base) and a bare ``plannerV2 providerV4`` run (no bind operator) stay exempt
# — only the exact-value assignment is a credential.
# R38 Block 67: the field continuation is the non-space class, BUT a quote /
# apostrophe is a legal field-name character only when the char BEFORE it is
# not a JSON string-opening structural delimiter (``:`` / ``,`` / ``{`` / ``[``
# / space) — ``evil"planner_version`` (quote embedded in the name) stays ONE
# field and fails closed, while a JSON member ``"summary":"plan.planner_version
# = …`` (the ``"`` after the ``:`` OPENS the string value) never lets the field
# swallow the member structure: the assignment binds the REAL field
# ``plan.planner_version`` and the documented-path exemption decides it.
_EXACT_REGISTERED_BASE_VALUE_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_\u4e00-\u9fff])"
    r"(?P<field>[\w\u4e00-\u9fff/\[\]]"
    r"(?:[^\s\"']|(?<![:,\s{[])['\"])*)"
    r"[ \t]*(?:" + _CREDENTIAL_NARRATION_BIND_OP + r")[ \t]*"
    r"(?P<token>(?:" + _WRAPPER_OPEN_CLASS + r"[ \t]*)*"
    r"(?:" + _REGISTERED_BASE_ALT + r")(?:V[0-9]+|[0-9]+)"
    r"(?:[ \t]*" + _WRAPPER_CLOSE_CLASS + r")*)"
    r"(?![A-Za-z0-9_\u4e00-\u9fff])"
)


def _documented_version_field_exempt(m: re.Match[str]) -> bool:
    """True when the exact-registered-base assignment ``m`` (R35 Block 59 / R36
    Block 61) binds at a DOCUMENTED business-value field path.  The field name
    is the exact member-key path derived from the closed
    ``_DOCUMENTED_BUSINESS_VALUE_PATHS`` registry (``planner_version`` /
    ``plan.planner_version`` / ``summary`` …) with COMPLETE member-key boundary
    matching — ``evilplanner_version`` is the field ``evilplanner_version``,
    never a prefix-stripped ``planner_version`` (R36 fake-suffix), and the
    value base must be in the path's allowed set (``planner_version =
    providerV4`` is a cross-field value, not exempt).  R38 Block 67: a single
    trailing ``"`` / ``'`` — the JSON field-name delimiter the non-space field
    continuation now keeps inside the name (``{"planner_version":
    "plannerV2"}`` yields field ``planner_version"``) — is stripped before the
    path build, so the DOCUMENTED key is still matched exactly; a field that
    merely EMBEDS the delimiter (``evil"planner_version``) is untouched and
    never documented."""
    field = m.group("field").strip()
    if len(field) >= 2 and field[-1] in "\"'":
        field = field[:-1].strip()
    path = tuple(part.strip().lower() for part in field.split("."))
    return _registered_base_value_exempt_at_path(path, m.group("token"))


# R40 Block 72: a semantic boundary that TERMINATES an exact-value assignment —
# end of line / end of text / a structural separator (the same value-bounding
# separators the designation-value capture uses, ``[^\r\n,;{}]``, plus the bind
# operators).  A letter / digit / CJK / ``_`` after the wrapped value is prose
# continuation (``verification code is (plannerV2) in the report``) and stays
# accepted; a separator means the value COMPLETELY consumed the boundary and the
# exact-value rule applies.
_EXACT_VALUE_BOUNDARY_CHARS = frozenset(",;{}:=|\\")


def _exact_value_at_semantic_boundary(text: str, end: int) -> bool:
    """R40 Block 72: True when the exact-base assignment token that ends at
    ``end`` COMPLETELY consumes a semantic boundary — end of text, end of line,
    or a structural separator — so the exact-value rule applies.  When normal
    prose follows the wrapped value (``verification code is (plannerV2) in the
    report``) the assignment is a phrase, not an exact value, and stays
    accepted.  An ILLEGAL wrapper is rejected BEFORE this check, so Block 69-71
    closure is never reopened by appending prose."""
    after = text[end:]
    if not after:
        return True
    if re.match(r"[ \t]*(?:\r?\n|$)", after):
        return True
    return after.lstrip(" \t")[:1] in _EXACT_VALUE_BOUNDARY_CHARS


def _credential_narration_binds(text: str) -> bool:
    """True when ``text`` puts a registered business-identifier base in a
    credential-narration context (R28 Block 48): a credential-designation word
    (``passphrase`` / ``login`` / ``pwd`` / ``passcode`` / ``key`` / ``userpass``
    / ``password`` / ``token`` …) in FIELD-NAME position, immediately followed by
    a binding operator (``is`` / ``was`` / ``:`` / ``=`` / arrow), with the
    bound value carrying a registered-base token (``passphrase is
    flightOption1`` / ``key: day2`` / ``the passcode was plannerV2``).  The
    binding is SYNTACTIC — a designation word alone in prose never binds a
    token, so the R27 flat wordlist cannot be bypassed with a synonym.  R35
    Block 59: the closure is now STRUCTURAL too — an ARBITRARY free-text field
    name binding an EXACT registered base (``OTP code is plannerV2`` /
    ``验证码是plannerV2`` / ``口令内容是plannerV2``) is credential-style narration
    at an unbound path and fails closed with NO new designation word; the only
    exempted assignment is the documented business-value field path
    (``planner_version = plannerV2`` / ``plan.planner_version = plannerV2``), the
    auditable schema path the business-identifier exemption is bound to.  R36
    Block 61: the exemption is a FUNCTIONAL member-key + base-set check against
    ``_DOCUMENTED_BUSINESS_VALUE_PATHS`` (the R35 ``<base>_version`` prefix regex
    is gone) — the wrapper forms ``'plannerV2'`` / ``(plannerV2)`` bind too, and
    a fake-suffix field (``evilplanner_version = plannerV2``) or a cross-field
    value (``planner_version = providerV4``) is NOT exempt."""
    for m in _CREDENTIAL_NARRATION_ASSIGN_RE.finditer(text):
        if _REGISTERED_BASE_TOKEN_IN_VALUE_RE.search(m.group("value")):
            return True
    for m in _EXACT_REGISTERED_BASE_VALUE_ASSIGN_RE.finditer(text):
        # R37/R39: the exact-base assignment resolves through the SHARED bounded
        # structural wrapper parser ``_registered_base_value_info`` — the SAME
        # resolver the JSON path uses — so free text and JSON share one nested +
        # inner-whitespace strip with a budget.  A NESTED / inner-whitespace
        # wrapper (``((plannerV2))`` / ``【“plannerV2”】`` / ``[ plannerV2 ]``)
        # resolves to the exact base, and a budget-exhausted / mismatched /
        # unclosed wrapper (``(plannerV2]`` / ``(plannerV2``) is an illegal
        # structural appearance that fails closed BEFORE the documented-path
        # exemption decides.
        info = _registered_base_value_info(m.group("token"))
        if info is _WRAPPED_BASE_ILLEGAL:
            return True
        if info is None:
            # a phrase (``see (tokenizationV1)``) — never an exact-value
            # assignment, never a credential
            continue
        # R40 Block 72: the exact-value rule applies ONLY when the wrapped value
        # COMPLETELY consumes a semantic boundary (end of line / end of text /
        # structural separator).  Trailing normal prose (``verification code is
        # (plannerV2) in the report``) makes the assignment a phrase and stays
        # accepted — an ILLEGAL wrapper was rejected above, so Block 69-71
        # closure is never reopened by appending prose.
        if not _exact_value_at_semantic_boundary(text, m.end()):
            continue
        if _documented_version_field_exempt(m):
            continue
        return True
    return False


def _is_registered_base_key_token(key: str) -> bool:
    """True when ``key`` is a REGISTERED business-identifier BASE token used as
    a JSON object KEY (``day1`` / ``plannerV2`` / ``planner1`` /
    ``flightOption1`` / ``refreshTokenCount1`` …), in ANY digit form — a base
    in a key position is a credential-shaped field regardless of the schema
    form its VALUE would need (R27 Block 43).  A plain documented schema field
    name (``day`` / ``planner`` / ``flight_option`` / ``planner_version``) has
    no registered suffix and returns False."""
    return _REGISTERED_BASE_KEY_TOKEN_RE.search(key) is not None


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
        nonlocal budget_nodes, budget_chars
        root_slot: dict[str, Any] = {"__out__": None}
        # Work item: ("value", node, container, key, struct_depth, path) masks
        # ``node`` into ``container[key]``; ("finalize", built, container,
        # key, struct_depth, path) commits an already-built container.  ``path``
        # is the exact member-key path of ``node`` (a list index collapses to
        # the ``[]`` placeholder).
        stack: list[tuple[str, Any, Any, Any, int, tuple[str, ...]]] = [
            ("value", parsed, root_slot, "__out__", 0, ())
        ]
        while stack:
            kind, node, container, key, struct_depth, path = stack.pop()
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
                stack.append(
                    ("finalize", out, container, key, struct_depth, path)
                )
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
                        # full-width ``Session_token`` key and its
                        # payload can never survive into the rebuilt artifact
                        # while the JSON stays valid (C-122 supervision 09:00
                        # gap 2: a cookie value ``a=b`` is not itself a
                        # credential SHAPE, so the value is masked by policy,
                        # not by shape — ``{"[REDACTED]": "[REDACTED]"}``).
                        out[marker] = marker
                    else:
                        child_path = path + ((k,) if isinstance(k, str) else ("",))
                        if isinstance(v, str):
                            # R36 Block 61: an EXACT registered business base
                            # in a string VALUE at a member path the documented
                            # business-value registry does not grant
                            # (``{"otp": "plannerV2"}``) is masked WHOLE — the
                            # producer / consumer must not seal an unbound
                            # business value the finals reject.  A documented
                            # path (``{"planner_version": "plannerV2"}`` /
                            # ``{"summary": "tokenizationV1"}``) is left to the
                            # normal level masker and survives.
                            if _registered_base_value_exempt_at_path(
                                child_path, v
                            ):
                                out[k] = mask_text(v, level_depth + 1)
                            else:
                                out[k] = marker
                        elif isinstance(v, (dict, list)):
                            stack.append(
                                ("value", v, out, k, struct_depth + 1, child_path)
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
                    ("finalize", out_list, container, key, struct_depth, path)
                )
                for i, item in enumerate(node):
                    if isinstance(item, str):
                        child_path = (*path, "[]")
                        if _registered_base_value_exempt_at_path(child_path, item):
                            out_list[i] = mask_text(item, level_depth + 1)
                        else:
                            out_list[i] = marker
                    elif isinstance(item, (dict, list)):
                        stack.append(
                            ("value", item, out_list, i, struct_depth + 1, (*path, "[]"))
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
