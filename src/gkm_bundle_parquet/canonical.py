"""The canonical form of a JSON record, and the digests of the record-identical round trip (ADR 0008 decisions 1.2,
1.3; the definition is in docs/round-trip.md, which this module implements).

A record's canonical form is RFC 8785 (JCS) with every array sorted:

- an object: its members sorted by name, the names compared as UTF-16 code units (RFC 8785 3.2.3), written
  `{"<name>":<value>,...}`. A name written twice in one object is refused.
- an array: each element in canonical form, the elements then sorted by the UTF-8 bytes of those forms (equal
  elements all stay, so an array is compared as a multiset), written `[<element>,...]`.
- a string: its decoded text, written as RFC 8785 3.2.2.2 writes it: `"` and `\\` escaped, U+0008, U+0009, U+000A,
  U+000C and U+000D as `\\b \\t \\n \\f \\r`, any other U+0000 to U+001F as `\\u00xx` (lower-case hex), everything
  else as itself in UTF-8. Text that isn't Unicode (a lone surrogate) is refused.
- a number: by its value, not its spelling. As in RFC 8785 3.2.2.3, its value is the IEEE-754 double nearest to
  it, written in ECMAScript's shortest form: `1`, `1.0`, `1E0` and `10e-1` are all `1`; `0.1`, `1e-7`, `1e+21`,
  `-0` is `0`. So, as in RFC 8785, integers beyond 2^53 in magnitude count as their nearest double. NaN, Infinity
  and a number outside the double range are refused.
- `true`, `false`, `null` as themselves. No whitespace anywhere.

A record's sha256 is the sha256 of its canonical form's UTF-8 bytes. A section's records sha256 is the sha256 of
the lines `<key> TAB <record sha256> LF`, one per record, `<key>` being the record's bundle key in canonical string
form (with its quotes), the lines sorted by their UTF-8 bytes. The root sha256 is the sha256 of the canonical form
of the bundle's root object with each section's value replaced by `{}`. The bundle's records sha256 is the sha256
of the lines `<section> TAB <section records sha256> LF`, one per section that holds a record (`<section>` in
canonical string form), plus the line `$ TAB <root sha256> LF`, sorted by their UTF-8 bytes. A canonical string
holds no raw TAB or LF, so every line reads one way.

Generic core: names no producer. Canonicalizing is pure Python; `Hasher` spreads large batches over worker
processes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import deque
from collections.abc import Iterable, Iterator
from decimal import Decimal
from json.encoder import encode_basestring
from json.encoder import encode_basestring as _string  # RFC 8785's string form, ensure_ascii off
from multiprocessing import get_context

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()  # no lines: a section with no records
ROOT_LINE = "$"  # the root's line in the bundle's records sha256
_EXACT = 2**53  # integers up to this magnitude are their own double
_FAIL = (
    "!"  # Hasher marks a record it can't canonicalize with this, then the reason; a sha256 never starts with it
)


class CanonicalError(ValueError):
    """A value has no canonical form: a name written twice, NaN or Infinity, a number outside the double range,
    text that isn't Unicode, or text that isn't JSON."""


# --- reading ---------------------------------------------------------------------------------------------------


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    d = dict(pairs)
    if len(d) != len(pairs):
        seen: set[str] = set()
        twice = sorted({k for k, _ in pairs if k in seen or seen.add(k)})
        raise CanonicalError(f"an object names {twice} more than once")
    return d


def _constant(name: str) -> object:
    raise CanonicalError(f"{name} is not a JSON number")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def loads(text: str | bytes) -> object:
    """A JSON text as Python values (integers as int, other numbers as float)."""
    if isinstance(text, bytes | bytearray):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as e:
            raise CanonicalError(f"not UTF-8: {e}") from e
    try:
        return _DECODER.decode(text)
    except json.JSONDecodeError as e:
        raise CanonicalError(f"not JSON: {e}") from e
    except ValueError as e:  # an integer too long for int() (sys.int_info.str_digits_check_threshold)
        if isinstance(e, CanonicalError):
            raise
        raise CanonicalError(str(e)) from e


# --- writing ---------------------------------------------------------------------------------------------------


def string(s: str) -> str:
    """A string in canonical form, with its quotes."""
    out = encode_basestring(s)
    _utf8(out)
    return out


def _utf8(s: str) -> bytes:
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError as e:
        raise CanonicalError(f"a string isn't Unicode text ({e.reason})") from None


def _utf16(name: str) -> bytes:
    return name.encode("utf-16-be", "surrogatepass")


def _es_double(x: float) -> str:
    """ECMAScript's Number::toString of a finite double (RFC 8785 3.2.2.3)."""
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    _, digits, exp = Decimal(repr(abs(x))).normalize().as_tuple()
    s = "".join(map(str, digits))
    k, n = len(s), len(s) + exp  # x = 0.s * 10**n
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * -n + s
    else:
        e = n - 1
        body = (s[0] + ("." + s[1:] if k > 1 else "")) + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def _number(x: int | float) -> str:
    if type(x) is int:
        if -_EXACT <= x <= _EXACT:
            return str(x)
        try:
            x = float(x)  # correctly rounded
        except OverflowError:
            raise CanonicalError(f"the number {x} is outside the IEEE-754 double range") from None
    if not math.isfinite(x):
        raise CanonicalError("a number outside the IEEE-754 double range")
    if x.is_integer() and -_EXACT <= x <= _EXACT:
        return str(int(x))
    return _es_double(x)


def canonical(v: object) -> str:
    """The canonical form of a value as `loads` returns it (module docstring). Its strings are not checked for
    lone surrogates here: `_utf8` refuses them when the form is hashed."""
    t = type(v)
    if t is str:
        return _string(v)
    if t is dict:
        names = sorted(v)
        if not all(map(str.isascii, names)):
            names.sort(key=_utf16)
        return "{" + ",".join([_string(k) + ":" + canonical(v[k]) for k in names]) + "}"
    if t is list:
        parts = [canonical(e) for e in v]
        parts.sort()  # str order is code point order, which is UTF-8 byte order
        return "[" + ",".join(parts) + "]"
    if t is int:
        return str(v) if -_EXACT <= v <= _EXACT else _number(v)
    if t is bool:
        return "true" if v else "false"
    if v is None:
        return "null"
    if t is float:
        return _number(v)
    raise CanonicalError(f"no JSON value of type {t.__name__}")


def canonical_text(text: str | bytes) -> str:
    """The canonical form of a JSON text."""
    return canonical(loads(text))


def sha256_text(s: str) -> str:
    return hashlib.sha256(_utf8(s)).hexdigest()


def record_sha256(text: str | bytes) -> str:
    """The sha256 of a JSON text's canonical form."""
    return sha256_text(canonical_text(text))


def key_line(key: str, sha: str) -> str:
    """A record's line in its section's records sha256 (without the LF)."""
    return f"{string(key)}\t{sha}"


def root_sha256(members: Iterable[tuple[str, str | bytes | None]]) -> str:
    """The root sha256 of a bundle whose root members are `(name, JSON text)`, a section `(name, None)`."""
    root = {}
    for name, text in members:
        if name in root:
            raise CanonicalError(f"the root names {name!r} twice")
        root[name] = {} if text is None else loads(text)
    return sha256_text(canonical(root))


def records_sha256(sections: dict[str, str], root: str) -> str:
    """The bundle's records sha256 from each non-empty section's records sha256 and the root sha256."""
    lines = sorted(f"{string(s)}\t{sha}\n" for s, sha in sections.items() if sha != EMPTY_SHA256)
    lines.append(f"{ROOT_LINE}\t{root}\n")
    lines.sort()
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


# --- many records ----------------------------------------------------------------------------------------------


def _shas(texts: list[str]) -> list[str]:
    """Each text's record sha256, or _FAIL and the reason."""
    out = []
    for t in texts:
        try:
            out.append(record_sha256(t))
        except CanonicalError as e:
            out.append(_FAIL + str(e))
    return out


def failure(result: str) -> str | None:
    """Why a Hasher result is not a sha256, or None when it is one."""
    return result[1:] if result.startswith(_FAIL) else None


class Hasher:
    """Record sha256s of batches of JSON texts, in order. A stream of more than one batch is spread over worker
    processes (`workers`, default the CPU count), with up to two batches per worker in flight; a stream of one
    batch is hashed in this process, so small sections start no pool. A text with no canonical form gives
    `failure`'s reason instead of a sha256. Close it when done."""

    def __init__(self, workers: int | None = None):
        self.workers = workers if workers is not None else (os.cpu_count() or 1)
        self._pool = None

    def map(self, batches: Iterable[tuple[object, list[str]]]) -> Iterator[tuple[object, list[str]]]:
        """(tag, texts) -> (tag, results), in the order given."""
        it = iter(batches)
        first = next(it, None)
        if first is None:
            return
        second = next(it, None)
        if second is None or self.workers <= 1:
            yield first[0], _shas(first[1])
            if second is not None:
                yield second[0], _shas(second[1])
                for tag, texts in it:
                    yield tag, _shas(texts)
            return
        if self._pool is None:
            self._pool = get_context("spawn").Pool(self.workers)
        pending: deque = deque()
        for tag, texts in _chain(first, second, it):
            pending.append((tag, self._pool.apply_async(_shas, (texts,))))
            if len(pending) >= 2 * self.workers:
                tag0, r = pending.popleft()
                yield tag0, r.get()
        while pending:
            tag0, r = pending.popleft()
            yield tag0, r.get()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.terminate()
            self._pool.join()
            self._pool = None

    def __enter__(self) -> Hasher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _chain(first, second, rest):
    yield first
    yield second
    yield from rest
