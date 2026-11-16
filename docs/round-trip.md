# The round trip: record-identical

A full build of the `sections` spec encodes every record of the bundle
([ADR 0005](decisions/0005-lossless-sections-build.md)), and `rebuild` writes the bundle back from the Parquet files.
From tool 2.0.0 the promise is **record-identical** ([ADR 0008](decisions/0008-end-state-reversible-default-custom-projections-filters.md)
decisions 1.2 and 1.3): every rebuilt record has the same *canonical form* as the source record of the same key, and
nothing else is promised about the text. Key order, array order, whitespace, escapes and the spelling of numbers don't
count; a changed, added or dropped value, record or root member does.

The tool checks this with sha256s of canonical forms, so the check is the same for any layout of the source (the line
layout, pretty-printed, minified) and doesn't depend on how the rebuilt text is laid out. `src/clinvar_gkm_parquet/canonical.py`
implements this page.

## The canonical form of a JSON value

It is [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) (the JSON Canonicalization Scheme, JCS) with one change: every
array is sorted.

- **Object**: its members sorted by name, the names compared as UTF-16 code units (RFC 8785 §3.2.3), written
  `{"<name>":<value>,...}`. A name written twice in one object has no canonical form.
- **Array**: each element in canonical form, then the elements sorted by the UTF-8 bytes of those forms, written
  `[<element>,...]`. Equal elements all stay: an array is compared as a multiset, so `[1,1,2]` and `[1,2,2]` differ, and
  `[2,1]` and `[1,2]` don't.
- **String**: its decoded text, written as RFC 8785 §3.2.2.2 writes it: `"` and `\` escaped as `\"` and `\\`; U+0008,
  U+0009, U+000A, U+000C and U+000D as `\b`, `\t`, `\n`, `\f`, `\r`; any other character from U+0000 to U+001F as
  `\u00xx` with lower-case hex; every other character as itself, in UTF-8 (so `é`, `é`, `\/` and `/` read as
  their characters). No Unicode normalization: a composed `é` and `e` followed by U+0301 differ. Text that isn't Unicode
  (a lone surrogate, `"\ud800"`) has no canonical form.
- **Number**: by its value, not its spelling. As in RFC 8785 §3.2.2.3, the value is the IEEE-754 double nearest to the
  number, written as ECMAScript's `Number.prototype.toString` writes it: `1`, `1.0`, `1E0` and `10e-1` are all `1`;
  `-0` is `0`; `0.1`, `1e-7`, `1e+21`, `333333333.3333333`. Integers beyond 2^53 in magnitude are therefore their
  nearest double too (`9007199254740993` reads as `9007199254740992`). NaN, Infinity and a number outside the double
  range have no canonical form.
- `true`, `false` and `null` as themselves. No whitespace anywhere.

A record with no canonical form stops the build, naming its section and key.

## The sha256s

| Name | Where it is recorded | The sha256 of |
| --- | --- | --- |
| record sha256 | (not recorded) | a record's canonical form, in UTF-8 |
| section records sha256 | each file's key-value metadata, `clinvar_gkm_parquet.section_records_sha256` | the lines `<key>` TAB `<record sha256>` LF, one per record of the section, sorted by their UTF-8 bytes |
| root sha256 | (not recorded; part of the next) | the canonical form of the bundle's root object with each section's value replaced by `{}` |
| records sha256 | the manifest, `records_sha256` | the lines `<section>` TAB `<section records sha256>` LF, one per section that holds a record, plus the line `$` TAB `<root sha256>` LF, sorted by their UTF-8 bytes |

`<key>` and `<section>` are the record's key and the section's name as canonical strings, with their quotes (`"SCV000012345"`).
A canonical string holds no raw TAB or LF, so every line reads one way, and `$` (unquoted) can't be a section's line. A
section with no records has the sha256 of no text (`e3b0c442...b855`) and no line in the records sha256. The root sha256
covers the version property and every root value (CIViC's `metadata`, say) and which sections the root names; the section
lines cover their records.

The records sha256 depends only on the records, so two texts of one bundle (the published line layout and a
pretty-printed copy, or a rebuilt bundle) have the same records sha256, and anyone can recompute it from a bundle with a
JSON parser and sha256: load the bundle, canonicalize each record, and hash the lines as above.

## What checks it

- **The build** hashes every source record as it streams (`section_records_sha256`, then the manifest's
  `records_sha256`) and, after writing each file, writes every row back as a record and checks that its record sha256 is
  its source record's. The first that differs stops the build, naming the table, the key and the first differing
  character of the two canonical forms.
- **`rebuild`** writes the bundle in the line layout (one record per line, sections in the root's order, records in their
  source order, keys in column order), hashes it as it goes, and checks each section against the file's
  `section_records_sha256` and the whole against the manifest's `records_sha256`. A difference stops it, and no output is
  left. It prints the records sha256.
- **`verify --roundtrip`** does the same without writing; with `--full <bundle>` it also hashes that bundle, from its own
  text, and compares the two records sha256s, naming the sections (and the root) that differ.

## Sets built by tool 1.x

Tool 1.x promised a byte-identical text and recorded the sha256 of the text (`section_text_sha256` in each file,
`source_text_sha256` in the manifest), not a records sha256. Tool 2.0.0 still reads such a set (its `table-spec.json`
may hold the 1.x `key_order` and `sorted_columns`, which it ignores) and:

- `rebuild` writes it back, prints its records sha256, and says it is **not checked**: there is nothing recorded to check
  it against. The text sha256s are not compared: the rebuilt text is laid out by tool 2.0.0's rules, not 1.x's.
- `verify --roundtrip` refuses it unless `--full <bundle>` is given; with the bundle it compares the rebuilt records
  with the bundle's, which is a full check.

To get a set whose own records sha256 is recorded, build it again with tool 2.0.0.
