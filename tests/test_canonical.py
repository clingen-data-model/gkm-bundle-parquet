"""The canonical form of a record and the records sha256s (canonical.py; docs/round-trip.md; ADR 0008
decisions 1.2, 1.3).

Expectations come from RFC 8785 (its examples in sections 3.2.2.3, 3.2.3 and 3.2.4, and ECMAScript's
Number-to-String for the numbers) and from hashlib on hand-written line text, not from the code under test. The
values are JSON texts, not bundle records: the record-level tests on real bundles are in test_roundtrip.py.
"""

from __future__ import annotations

import hashlib

import pytest

from gkm_bundle_parquet.canonical import (
    EMPTY_SHA256,
    CanonicalError,
    Hasher,
    canonical_text,
    failure,
    key_line,
    record_sha256,
    records_sha256,
    root_sha256,
)


def same(a: str, b: str) -> bool:
    return canonical_text(a) == canonical_text(b)


# --- objects: key order ----------------------------------------------------------------------------------------


def test_key_order_does_not_count():
    assert canonical_text('{"b": 1, "a": {"d": [], "c": null}}') == '{"a":{"c":null,"d":[]},"b":1}'
    assert same('{"a":1,"b":2}', '{"b":2,"a":1}')


def test_names_sort_by_utf16_code_units():
    """RFC 8785 3.2.3's example: U+1F600 (a surrogate pair, D83D DE00) sorts before U+FB33, though its code point
    is higher."""
    text = (
        '{"\\u20ac": "Euro Sign", "\\r": "Carriage Return", "\\ufb33": "Hebrew Letter Dalet With Dagesh", '
        '"1": "One", "\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control", '
        '"\\u00f6": "Latin Small Letter O With Diaeresis"}'
    )
    out = canonical_text(text)
    order = [
        "Carriage Return",
        "One",
        "Control",
        "Latin Small Letter O With Diaeresis",
        "Euro Sign",
        "Emoji: Grinning Face",
        "Hebrew Letter Dalet With Dagesh",
    ]
    assert [out.index(v) for v in order] == sorted(out.index(v) for v in order)


def test_a_name_written_twice_is_refused():
    with pytest.raises(CanonicalError, match=r"names \['a'\] more than once"):
        canonical_text('{"a": 1, "b": {"a": 2}, "a": 1}')
    assert canonical_text('{"a": 1, "b": {"a": 2}}')  # the same name in two objects is fine


# --- arrays: element order -------------------------------------------------------------------------------------


def test_array_order_does_not_count_at_any_depth():
    a = '{"x": [3, 1, 2], "y": [{"k": ["b", "a"]}, {"k": []}]}'
    b = '{"y": [{"k": []}, {"k": ["a", "b"]}], "x": [2, 3, 1]}'
    assert same(a, b)
    assert canonical_text(a) == '{"x":[1,2,3],"y":[{"k":["a","b"]},{"k":[]}]}'


def test_an_array_is_a_multiset():
    assert not same("[1, 1, 2]", "[1, 2, 2]")
    assert not same("[1, 1]", "[1]")
    assert same('["a", "b", "a"]', '["a", "a", "b"]')


def test_elements_sort_by_the_utf8_bytes_of_their_canonical_form():
    # "1e+30" < "1e-27" ('+' 0x2B < '-' 0x2D); '"' 0x22 < digits < '[' 0x5B < 'f' < 'n' < 't' < '{' 0x7B
    assert canonical_text("[1e-27, 1e30]") == "[1e+30,1e-27]"
    assert canonical_text('[{}, true, null, false, [], "", 0]') == '["",0,[],false,null,true,{}]'
    # U+FFFF (EF BF BF) sorts before U+1F600 (F0 9F 98 80) in UTF-8, after it in UTF-16
    assert canonical_text('["\\ud83d\\ude00", "\\uffff"]') == '["\uffff","\U0001f600"]'


def test_rfc8785_example_with_arrays_sorted():
    """RFC 8785 3.2.4's input; its output, with this form's one change: each array sorted by its elements'
    canonical forms."""
    text = (
        '{\n  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],\n'
        '  "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",\n'
        '  "literals": [null, true, false]\n}'
    )
    want = (
        '{"literals":[false,null,true],"numbers":[0.002,1e+30,1e-27,333333333.3333333,4.5],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonical_text(text) == want


# --- numbers ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spellings",
    [
        ["1", "1.0", "1e0", "1E+0", "10e-1", "0.1e1"],
        ["0", "-0", "0.0", "-0.0", "0e5"],
        ["0.1", "0.10", "1e-1", "1.0e-1"],
        ["100", "1e2", "100.000"],
        ["-1.5", "-15e-1", "-1.50"],
    ],
)
def test_number_spelling_does_not_count(spellings):
    assert len({canonical_text(s) for s in spellings}) == 1


@pytest.mark.parametrize(
    "text, want",
    [
        # RFC 8785 3.2.2.3 and ECMAScript's Number::toString
        ("1e21", "1e+21"),
        ("1e20", "100000000000000000000"),
        ("1e-7", "1e-7"),
        ("0.000001", "0.000001"),
        ("333333333.33333329", "333333333.3333333"),
        ("1E30", "1e+30"),
        ("4.50", "4.5"),
        ("2e-3", "0.002"),
        ("0.000000000000000000000000001", "1e-27"),
        ("5e-324", "5e-324"),
        ("1.7976931348623157e308", "1.7976931348623157e+308"),
        ("295147905179352825856", "295147905179352830000"),  # 2^68
        ("-9007199254740992", "-9007199254740992"),  # -2^53, exact
        ("123456789012345678901234567890", "1.2345678901234568e+29"),
    ],
)
def test_numbers_are_written_as_rfc8785_writes_doubles(text, want):
    assert canonical_text(text) == want


def test_integers_beyond_2_53_count_as_their_double():
    """As in RFC 8785 (I-JSON): 2^53 + 1 is not a double, and reads as 2^53."""
    assert canonical_text("9007199254740993") == "9007199254740992" == canonical_text("9007199254740992")


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "[1e400]", '{"a": -1e999}', "1" + "0" * 400])
def test_non_numbers_and_out_of_range_numbers_are_refused(text):
    with pytest.raises(CanonicalError):
        canonical_text(text)


# --- strings: escapes and Unicode ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a, b",
    [
        ('"\\u00e9"', '"é"'),
        ('"\\/"', '"/"'),
        ('"\\u0041\\u0042"', '"AB"'),
        ('"\\ud83d\\ude00"', '"\U0001f600"'),
        ('"\\u000A"', '"\\n"'),
        ('"\\u001F"', '"\\u001f"'),
        ('"\\""', '"\\u0022"'),
    ],
)
def test_escapes_do_not_count(a, b):
    assert same(a, b)


def test_strings_are_written_as_rfc8785_writes_them():
    text = '"\\b\\t\\n\\f\\r\\u0000\\u001f\\u007f\\u2028\\"\\\\/é\\ud83d\\ude00"'
    assert canonical_text(text) == '"\\b\\t\\n\\f\\r\\u0000\\u001f\x7f\u2028\\"\\\\/é\U0001f600"'


def test_unicode_is_not_normalized():
    """No Unicode normalization: é composed (U+00E9) and decomposed (e U+0301) are different text."""
    assert not same('"\\u00e9"', '"e\\u0301"')


def test_a_lone_surrogate_is_refused():
    with pytest.raises(CanonicalError, match="isn't Unicode text"):
        record_sha256('{"a": "\\ud800"}')


def test_whitespace_and_bytes_do_not_count():
    assert record_sha256(b'{ "a" : [ 1 ,2 ] }\n') == record_sha256('{"a":[2,1]}')
    assert record_sha256('{"a":[2,1]}') == hashlib.sha256(b'{"a":[1,2]}').hexdigest()


@pytest.mark.parametrize("text", ["", "{", '{"a": 1} x', b"\xff"])
def test_text_that_is_not_json_is_refused(text):
    with pytest.raises(CanonicalError):
        canonical_text(text)


# --- the records sha256s ---------------------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_key_line_quotes_the_key():
    assert key_line('a\tb"', "f" * 64) == '"a\\tb\\""\t' + "f" * 64


def test_records_sha256_is_of_the_sorted_section_lines_and_the_root_line():
    root = root_sha256([("schema_version", '"1.1.0"'), ("b", None), ("a", None), ("empty", None)])
    assert root == _sha('{"a":{},"b":{},"empty":{},"schema_version":"1.1.0"}')
    sections = {"b": "1" * 64, "a": "2" * 64, "empty": EMPTY_SHA256}
    want = _sha(f'"a"\t{"2" * 64}\n"b"\t{"1" * 64}\n$\t{root}\n')  # a section with no records has no line
    assert records_sha256(sections, root) == want


def test_root_values_count_by_value():
    a = root_sha256([("v", '"x"'), ("meta", '{"b": [2, 1], "a": 1.0}'), ("s", None)])
    b = root_sha256([("s", None), ("meta", '{"a":1,"b":[1,2]}'), ("v", '"x"')])
    assert a == b
    with pytest.raises(CanonicalError, match="twice"):
        root_sha256([("s", None), ("s", None)])


# --- the hasher ------------------------------------------------------------------------------------------------


def test_the_hasher_gives_the_same_results_in_processes_and_inline():
    texts = [f'{{"a": [{i}, 1]}}' for i in range(50)] + ["NaN", '{"a": 1, "a": 2}']
    batches = [(i, texts[i : i + 7]) for i in range(0, len(texts), 7)]
    inline = [r for _, rs in Hasher(workers=1).map(batches) for r in rs]
    with Hasher(workers=2) as h:
        pooled = list(h.map(batches))
    assert [tag for tag, _ in pooled] == [tag for tag, _ in batches]
    assert [r for _, rs in pooled for r in rs] == inline
    assert inline[:50] == [record_sha256(t) for t in texts[:50]]
    assert failure(inline[50]) == "NaN is not a JSON number" and "more than once" in failure(inline[51])
    assert failure(inline[0]) is None
