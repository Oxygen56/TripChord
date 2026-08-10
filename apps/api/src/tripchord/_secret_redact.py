"""Bounded recursive JSON / JSON-string secret scanning and masking.

C-122 supervision 06:58: a credential can be smuggled through multiple layers
of JSON encoding — ``{"outer": "{\\"Authorization\\": \\"Basic a\\"}"}`` — where
each ``json.dumps`` adds another layer of backslash escaping that a raw-byte
regex stops seeing after one level.  This module provides:

* :func:`iter_json_levels` — a BOUNDED recursive ``json.loads`` walker that
  yields the text at every decoded level (hard depth / node / size caps,
  parse failures surfaced so callers fail closed, never unbounded recursion
  or waiting);
* :func:`bounded_json_mask` — a BOUNDED recursive masker that rebuilds a
  whole-JSON document with every nested JSON-string value masked, and applies
  a caller-supplied ``mask_level`` to every free-form level.

Both are shared by the canary producer
(``benchmarks/live_canary_certified.py``), the gate consumer and the final
secret scan (``scripts/run_product_done_gate.py``) so the whole chain has ONE
consistent redaction semantic.  This module is the single source of truth —
do NOT re-implement the walker in callers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

# Hard budgets for the recursive JSON walk.  ``_MAX_JSON_SCAN_DEPTH`` covers
# the mandated level 0-3 double/triple-encoding counter-examples plus structural
# margin; the node / size caps stop a maliciously huge or fan-out document from
# forcing unbounded work.  Budget overflow raises ``RecursiveJsonBudgetError``
# and every caller fails closed.
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


def iter_json_levels(text: str) -> Iterator[tuple[str, int, bool]]:
    """Yield ``(level_text, depth, malformed)`` for ``text`` and every nested
    JSON-string value found by bounded recursive ``json.loads``.

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
                parsed = json.loads(current)
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
            # text — the next encoding level down.
            if looks_like_json(parsed):
                stack.append((parsed, depth + 1))
            continue
        # Collect every JSON-string value in the parsed structure so each is
        # scanned at the next depth down.
        pending: list[Any] = [parsed]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for value in node.values():
                    if isinstance(value, str):
                        if looks_like_json(value):
                            stack.append((value, depth + 1))
                    elif isinstance(value, (dict, list)):
                        pending.append(value)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, str):
                        if looks_like_json(item):
                            stack.append((item, depth + 1))
                    elif isinstance(item, (dict, list)):
                        pending.append(item)


def bounded_json_mask(
    text: str,
    *,
    mask_level: Callable[[str], str],
    marker: str = "[REDACTED]",
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
    """
    budget_nodes = 0
    budget_chars = 0

    def mask_value(node: Any, depth: int) -> Any:
        nonlocal budget_nodes, budget_chars
        if isinstance(node, dict):
            budget_nodes += 1
            if budget_nodes > _MAX_JSON_SCAN_NODES:
                raise RecursiveJsonBudgetError("JSON mask node budget exceeded")
            return {
                key: mask_value(value, depth + 1) for key, value in node.items()
            }
        if isinstance(node, list):
            budget_nodes += 1
            if budget_nodes > _MAX_JSON_SCAN_NODES:
                raise RecursiveJsonBudgetError("JSON mask node budget exceeded")
            return [mask_value(item, depth + 1) for item in node]
        if isinstance(node, str):
            return mask_text(node, depth + 1)
        return node

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
            return mask_level(current)
        try:
            parsed = json.loads(current)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # Structural-start but not valid JSON: a truncated / obfuscated
            # JSON attempt — mask the whole level (fail closed).
            return marker
        if isinstance(parsed, str):
            return json.dumps(mask_text(parsed, depth + 1), ensure_ascii=False)
        if isinstance(parsed, (dict, list)):
            return json.dumps(mask_value(parsed, depth), ensure_ascii=False)
        return mask_level(current)

    try:
        rebuilt = mask_text(text, 0)
    except RecursiveJsonBudgetError:
        return marker
    # Final sweep: a JSON key/value pair reconstructed by the walker is
    # collapsed name-and-value together exactly like the raw scan does.
    return mask_level(rebuilt)
