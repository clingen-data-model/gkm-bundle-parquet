"""The canonical form of a JSON record, and the digests of the record-identical round trip (ADR 0008 decisions 1.2,
1.3; the definition is in docs/round-trip.md, which the Rust core implements).

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

Generic core: names no producer. The Rust core computes all of this (`crates/gkm-bundle-core`, through the
extension module `_core`; ADR 0010), and this module names it for the package. A JSON text is a `str` or bytes; one
with no canonical form raises CanonicalError, a ValueError. `Hasher` hashes batches in this process, on threads.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator

from ._core import (
    EMPTY_SHA256,  # no lines: a section with no records
    ROOT_LINE,  # the root's line in the bundle's records sha256
    CanonicalError,
    canonical_text,
    key_line,
    record_sha256,
    record_sha256s,
    records_sha256,
    root_sha256,
    string,
)
from ._core import FAIL as _FAIL  # a Hasher result that starts with it is a reason, not a sha256

__all__ = [
    "EMPTY_SHA256",
    "ROOT_LINE",
    "CanonicalError",
    "Hasher",
    "canonical_text",
    "failure",
    "key_line",
    "record_sha256",
    "records_sha256",
    "root_sha256",
    "string",
]


def failure(result: str) -> str | None:
    """Why a Hasher result is not a sha256, or None when it is one."""
    return result[1:] if result.startswith(_FAIL) else None


class Hasher:
    """Record sha256s of batches of JSON texts, in order, hashed in this process with the GIL released, on up to
    `workers` threads (default the CPU count). A text with no canonical form gives `failure`'s reason instead of a
    sha256. It holds nothing, so closing it is optional."""

    def __init__(self, workers: int | None = None):
        self.workers = workers if workers is not None else (os.cpu_count() or 1)

    def map(self, batches: Iterable[tuple[object, list[str]]]) -> Iterator[tuple[object, list[str]]]:
        """(tag, texts) -> (tag, results), in the order given."""
        for tag, texts in batches:
            yield tag, record_sha256s(texts, self.workers)

    def close(self) -> None:
        pass

    def __enter__(self) -> Hasher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
