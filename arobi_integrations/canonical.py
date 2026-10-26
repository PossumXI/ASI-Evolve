"""Immaculate-compatible canonical JSON.

Immaculate hashes and signs ASI dispatch packets with ``stableStringify`` from
``apps/harness/src/utils.ts``:

- arrays keep their order;
- object keys are sorted with ``String.prototype.localeCompare`` (the host's
  default ICU collation, en-US on the harness hosts);
- every primitive is serialized with ``JSON.stringify``, so strings are raw
  UTF-8 (no ``\\uXXXX`` escaping of non-ASCII) and numbers use the ECMAScript
  ``Number#toString`` form.

Python's ``json.dumps(sort_keys=True)`` differs on all three points (code-point
key order, ``ensure_ascii`` escaping, ``1.0`` vs ``1``), so the bridge must not
use it for anything Immaculate verifies. This module reproduces the Immaculate
bytes exactly. The golden vectors in ``tests/fixtures`` were produced by
Immaculate's own code and pin the equivalence.

Keys are limited to printable ASCII. For that alphabet ICU root collation is a
fixed table (below): every character carries one primary weight, letters share
a primary weight with their other case, and lowercase sorts before uppercase at
the tertiary level. Keys outside the alphabet raise ``CanonicalizationError``
instead of silently producing bytes Immaculate would hash differently.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

# ICU root collation order of printable ASCII (0x20-0x7E), as reported by
# Node's ``localeCompare``. Adjacent lower/upper case letters share a primary
# weight. Regenerate with:
#   node -e 'const c=[];for(let i=32;i<127;i++)c.push(String.fromCharCode(i));
#            console.log(JSON.stringify(c.sort((a,b)=>a.localeCompare(b)).join("")))'
ICU_ASCII_ORDER = " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$0123456789aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ"

_PRIMARY: dict[str, int] = {}
_TERTIARY: dict[str, int] = {}


def _build_collation_tables() -> None:
    rank = 0
    for char in ICU_ASCII_ORDER:
        if char.isalpha() and char.isupper():
            _PRIMARY[char] = _PRIMARY[char.lower()]
            _TERTIARY[char] = 1
            continue
        rank += 1
        _PRIMARY[char] = rank
        _TERTIARY[char] = 0


_build_collation_tables()
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_UNDEFINED_TOKEN = "undefined"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be canonicalized exactly like Immaculate."""


class _JsUndefined:
    """Marker for a JavaScript ``undefined`` object value.

    ``stableStringify`` renders ``{key: undefined}`` as ``"key":undefined``.
    Immaculate relies on that when it hashes its own receipts
    (``sha256Json({...receipt, receiptSha256: undefined})``), so verifying an
    Immaculate receipt hash needs the same token.
    """

    _instance: "_JsUndefined | None" = None

    def __new__(cls) -> "_JsUndefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "JS_UNDEFINED"


JS_UNDEFINED = _JsUndefined()


def locale_compare_key(key: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Sort key equivalent to ``localeCompare`` for printable-ASCII strings."""
    primary: list[int] = []
    tertiary: list[int] = []
    for char in key:
        weight = _PRIMARY.get(char)
        if weight is None:
            raise CanonicalizationError(
                f"object key {key!r} contains {char!r}; only printable ASCII keys have a "
                "reproducible localeCompare order"
            )
        primary.append(weight)
        tertiary.append(_TERTIARY[char])
    return tuple(primary), tuple(tertiary)


def _js_string(value: str) -> str:
    # Re-pair any surrogate halves, as a JavaScript string would hold them.
    paired = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    encoded = json.dumps(paired, ensure_ascii=False)
    # JSON.stringify escapes lone surrogates (well-formed JSON.stringify).
    return _LONE_SURROGATE.sub(lambda match: f"\\u{ord(match.group(0)):04x}", encoded)


def js_number(value: float) -> str:
    """Format a float exactly like ECMAScript ``Number#toString``."""
    if math.isnan(value) or math.isinf(value):
        return "null"  # JSON.stringify(NaN|Infinity) === "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))  # shortest round-trip digits, like ECMAScript
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    digits = integer_part + fraction
    point = len(integer_part) + exponent
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    count = len(digits)
    if count <= point <= 21:
        body = digits + "0" * (point - count)
    elif 0 < point <= 21:
        body = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        body = f"0.{'0' * -point}{digits}"
    else:
        power = point - 1
        suffix = f"e{'+' if power >= 0 else '-'}{abs(power)}"
        body = f"{digits}{suffix}" if count == 1 else f"{digits[0]}.{digits[1:]}{suffix}"
    return sign + body


def _js_primitive(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) <= 2**53:
            return str(value)
        return js_number(float(value))
    if isinstance(value, float):
        return js_number(value)
    if isinstance(value, str):
        return _js_string(value)
    if value is JS_UNDEFINED:
        return _UNDEFINED_TOKEN
    raise CanonicalizationError(f"unsupported value type for canonical JSON: {type(value).__name__}")


def stable_stringify(value: Any) -> str:
    """Byte-compatible port of Immaculate ``stableStringify``."""
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(stable_stringify(entry) for entry in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(f"object keys must be strings, got {type(key).__name__}")
        ordered = sorted(value.items(), key=lambda item: locale_compare_key(item[0]))
        return "{" + ",".join(f"{_js_string(key)}:{stable_stringify(entry)}" for key, entry in ordered) + "}"
    return _js_primitive(value)


def canonical_bytes(value: Any) -> bytes:
    text = stable_stringify(value)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as error:  # pragma: no cover - lone surrogates are escaped above
        raise CanonicalizationError(str(error)) from error


def sha256_canonical(value: Any) -> str:
    """Immaculate ``sha256Json``: sha256 over the UTF-8 canonical bytes."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
