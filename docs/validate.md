# `validate`: JSON Schema validation of a bundle, fast and proved equal to the reference

`gkm-bundle-parquet validate <bundle> --bundle-schema <schema>` checks every record of a bundle against its JSON Schema set. ClinVar-GKM validates
every published bundle strictly with the GA4GH GKM Toolkit (`ga4gh.gkm` 0.4.0), whose validator is Python `jsonschema`. That
takes days of CPU time for a full bundle. This command gives the same verdicts with jsonschema-rs, in minutes. The evidence
that the verdicts are the same is below.

## How it validates

- **One record at a time, as ClinVar-GKM does.** Each record is validated alone, as the one-section bundle
  `{<version property>: <the bundle's own>, <section>: {<key>: <record>}}`; for ClinVar-GKM, with `--version-property
  schema_version`, that is the shape its `src/scripts/validate-bundle.py` passes to the toolkit, so each section's key
  pattern is checked too. An empty section is validated as `{<version property>: ..., <section>: {}}`. A verdict is per
  record.
- **The version property** is `--version-property`; there is no default name (ADR 0009; 1.x defaulted to
  `schema_version`, or to none for a `--bundle-schema` whose root declares no `schema_version`). Without it, each record
  is validated as `{<section>: {<key>: <record>}}`, and a version at the root is validated as a root value, against its
  property in the schema.
- **The root's `required` is checked once, on the whole bundle.** It says which root properties the bundle holds, so a
  one-section mini-bundle can't meet it: CIViC's root `required`s all 19 of its root properties, and until this change
  every CIViC record failed, each only on the other 18. So each mini-bundle is validated against the root schema
  without its own `required` (`validate.record_root`), every other keyword kept, in both engines. Then, after the
  bundle is read, each name that `required` lists and the bundle doesn't hold (as a section, an empty section, a root
  value or the version property) is one failure, reported under that name with no key and no records:
  `{"section": "<name>", "key": null, "errors": [{"path": "", "message": "\"<name>\" is a required property"}]}`. That
  check is the same code for both engines, so they can't disagree on it.

  Nothing else is loosened: each record is still checked against its section's schema in full, and the key pattern, the
  version `const`, the root's `additionalProperties` and `allOf` and each section's `minProperties` still apply to every
  mini-bundle (`tests/test_validate.py::test_leaving_out_the_root_required_weakens_nothing_else`). Only the root's own
  top-level `required` is handled this way. A `required` nested in a root `allOf`, `dependentRequired`, or a root
  `minProperties` above 1 would still be applied to each mini-bundle; none of the published 1.1.0, narrowed or CIViC
  roots has one (ClinVar-GKM's has `minProperties: 1`, which every mini-bundle meets).
- **The bundle is streamed** with the tool's own reader (`bundle.py`), and its sha256 is checked against `--sha256` or a
  `.sha256` sidecar when there is one. Records are parsed and validated in worker processes (`--workers`, default: the CPU
  count).
- **The schema set** is the one a build uses: `--bundle-schema` (a file, URL or schema-set directory), required; without it
  `validate` stops with exit 2. Both engines are built from the same loaded `SchemaSet`
  (`schemas.py`). Nothing is fetched while validating: a `$ref` outside the set stops the run.
- **Exit codes:** 0 when every record passes; 1 when any fails, each named by section, key and instance paths (stderr, and
  every one in `--failures` as JSON lines), or when the bundle lacks a root property its root `required`s; 2 for anything
  that isn't a verdict on a record: an unreadable or malformed bundle, an unknown section, a missing or mismatched
  `schema_version`, a schema set that doesn't load or compile.
- `--json` writes the counts and timings per section.

## The engines

| | `rs` (default) | `python` (reference) |
| --- | --- | --- |
| Library | jsonschema-rs 0.61.1, pinned | `ga4gh.gkm` 0.4.0 `validate_bundle_schema`, on `jsonschema` |
| Install | a dependency of the tool | the `reference` extra (Python 3.11+, pre-release GA4GH packages) |
| Registry | every other file of the set under its `$id` (the root is compiled itself) | every file under its `$id` and its root-relative form, as ClinVar-GKM's `load_registry` |
| Formats | not validated (`validate_formats=False`) | not validated (`jsonschema`'s default) |
| Compiling | the root schema per set of root names a mini-bundle holds (below) | the root schema, once |

**Why rs compiles per section.** jsonschema-rs compiles a whole schema up front. On the published 1.1.0 set, compiling the
whole bundle root didn't finish in over two minutes (and grew past 680 MB), while its sections compile one by one in about
a minute in all: `scv` 19 s, `vcv` and `rcv` 15 s each, `propositionClinvarCustom` 11 s, the rest under a second (M2 Pro,
2026-11-10). The narrowed set compiles whole in 7 s. So the rs engine compiles, on first use, the root schema with its
`properties` cut down to the names a mini-bundle holds (the version property and one section), every other keyword kept. A
document holding only those names gets the same verdict from it as from the whole root, because the names it lacks are
never applied; the equivalence runs below check it anyway. The python engine uses the root unchanged.

## Equivalence: the evidence

The gate for making `rs` the default: both engines give every record the same verdict, on real data and on generated
negative cases, with each schema set:

- **published:** the published 1.1.0 set, vendored as `tests/fixtures/clinvar-gkm/1.1.0/schema-set/` (until 1.1.0, the tool's
  snapshot in `schemas/1.1.0/`);
- **narrowed:** ClinVar-GKM's narrowed profiles at commit `a956b5a` (`tests/fixtures/schema-sets/`, see its README), which
  set unused base properties to `false`, limit pointers to their target section, close extensions, add enums, and match
  each section's keys exactly;
- **civic:** CIViC's schema set (`tests/fixtures/civic/schema-set/`, root `urn:civic:gkm-bundle:schema:0.1.0-a0`), whose
  root `required`s all 19 root properties and declares no version.

Last rerun 2026-11-16, after the root's `required` moved to a check on the whole bundle (above), with the same inputs as
the 2026-11-11 runs.

### Real data

Run with `scripts/validate-equivalence.py`, which runs `validate` with each engine and compares the per-section counts and
the failing records (section and key) of the two runs.

| Input | Records | Schema set | rs failing | python failing | Verdicts |
| --- | ---: | --- | ---: | ---: | --- |
| 2026-10-31 weekly delta, whole (sha256 `443178d9…`, checked against its `.sha256` sidecar, which matches the index) | 495,547 | narrowed | 0 | 0 | identical |
| | | published | 0 | 0 | identical |
| 2026-10-31 weekly full, sample (below) | 922,634 | narrowed | 0 | 0 | identical |
| | | published | 0 | 0 | identical |
| CIViC `civic-assertion-9-bundle.json` | 30 | civic | 0 | 0 | identical |
| CIViC `civic-assertion-251-bundle.json` | 23 | civic | 0 | 0 | identical |

A CIViC count is its records, plus one for each empty collection and each root value that isn't a collection (each is
checked once). Before the change, every one of them failed under both engines (30 and 23), each only on the root
`required`.

The sample is `scripts/sample-bundle.py --every 100 --per-shape 2` of the 2026-10-31 weekly full (91,854,927 records; sha256
`7105eba1…`, checked against its `.sha256` sidecar, which matches the index; streamed from `data.clinvar-gkm.org`): every
100th record of each section plus the first two of each record shape, where a shape is the record's set of paths with each
object tagged by its `type` and each extension by its `name`. It holds all 20 sections and 2,355 shapes (`scv` alone has
2,113): 918,558 records by position and 4,076 more by shape.

### Negative cases

`tests/test_validate.py`, from `tests/_validate_cases.py`. Every case is a real record of the two excerpts (or, for the
civic set, of the two CIViC bundles) with one change, generated from the records and the schema set, not listed by hand:

- **Every property a ClinVar-GKM class narrows to `false`**, at every record node that class validates. Where a class applies
  comes from jsonschema-rs's annotations: the `properties` keyword of a schema object annotates each node it validated.
  The two excerpts (`tests/fixtures/real-1.1.0/` and `real-1.1.0-1031/`, 676 records) reach all 47 of the narrowed set's
  schema objects that set properties to `false`: 204 cases.
- **For the first record of each section:** a key that breaks its section's pattern; an undeclared property at the top
  level (an `allOf` profile; for `variation`, a `oneOf` branch) and at every nested object (and in a `clinvarHgvsList`
  item); every property removed (missing required); every array emptied and every object emptied; every string changed
  (just outside an enum, const or pattern) and turned into an integer; every integer turned into `1.0`, `x.5` and a string;
  every pointer sent to another section, and replaced by the record it points to (inline object where a pointer is
  required); an unknown extension `name`, and every extension `value` of the wrong type.

| Schema set | Cases | Rejected by both | Verdict differences |
| --- | ---: | ---: | ---: |
| narrowed | 1,309 | 962 | 0 |
| published | 1,105 | 652 | 0 |
| civic (the record changes above, for the first record of each of its 12 non-empty collections; no version) | 954 | 590 | 0 |

The civic set rejects all 12 broken keys and all 12 undeclared top-level properties. Its other rejections: 186 strings
turned into integers, 165 missing properties, 77 changed strings, 67 undeclared nested properties, 64 emptied objects
and 7 integers turned into strings or fractions. It has no `false` properties, so there are no banned-property cases.

The narrowed set rejects every kind listed; the published one accepts, as it should, what only the narrowing forbids (no
`false` properties, no pointer patterns). Where both reject, both report the same instance paths, with one known
difference: Python `jsonschema` reports the error of a `false` subschema at the parent object, jsonschema-rs at the
property itself (69 of the narrowed set's cases). On the civic set they report the same paths in every rejected case.

### Dialects

- **Regular expressions.** All 54 `pattern` and `patternProperties` regexes of the two sets compile in both engines and
  match the same of every real key and string in the fixtures. None uses lookaround or backreferences. One difference
  exists: Python's `$` also matches just before a final newline, while ECMA-262 and Rust's don't, so `"clinvar:1\n"` passes
  `^clinvar:[0-9]+$` under the python engine and fails under rs. No real key or string ends in a newline; rs follows the
  JSON Schema specification here. Unicode `\d` and `\w` behave the same.
- **Numbers and equality.** Both treat `1.0` as an integer and equal to `1` (`const`, `enum`, `uniqueItems`), handle
  integers beyond 64 bits, and compare objects regardless of key order. One difference: `multipleOf: 0.1` accepts `0.3`
  under rs (decimal) and rejects it under Python (binary floating point). Neither schema set uses `multipleOf`, and a test
  fails if one starts to.
- **Formats** are validated by neither.

## Timings

Apple M2 Pro (6 performance and 4 efficiency cores), 16 GB, macOS 15.7, Python 3.14.5, 10 workers, nothing else running
(2026-11-11). Wall time is the whole run: reading and checking the bundle, parsing, compiling and validating. Raw results:
`scripts/validate-equivalence.py`'s `summary.json` and each run's `--json`. The 2026-11-16 rerun ran beside other jobs,
so its timings aren't comparable and aren't listed; leaving the root's `required` out of a mini-bundle's schema doesn't
change what is compiled or validated per record.

| Input | Schema set | rs wall | python wall | rs records/s | python records/s |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-10-31 delta, 495,547 records | narrowed | 6.7 s | 988 s | 78,000 | 501 |
| | published | 221 s | 297 s | 2,240 | 1,670 |
| 2026-10-31 full, 922,634-record sample | narrowed | 8.4 s | 1,923 s | 109,000 | 480 |
| | published | 236 s | 618 s | 3,900 | 1,490 |

CPU per record, by section, on the delta (compile time apart):

| Section | Records | rs narrowed | python narrowed | rs published | python published |
| --- | ---: | ---: | ---: | ---: | ---: |
| `variation` | 25,963 | 0.33 ms | 157 ms | 0.13 ms | 56 ms |
| `scv` | 33,417 | 0.051 ms | 18.2 ms | 0.029 ms | 7.4 ms |
| `vcv` | 70,246 | 0.054 ms | 13.4 ms | 0.009 ms | 3.6 ms |
| `rcv` | 67,768 | 0.057 ms | 13.8 ms | 0.008 ms | 3.6 ms |
| `propositionClinvarCustom` | 180 | 0.013 ms | 28.9 ms | 0.011 ms | 16.3 ms |
| `propositionPathogenicity` | 128,868 | 0.006 ms | 3.4 ms | 0.005 ms | 0.74 ms |
| `evidenceLine` | 140,861 | 0.008 ms | 1.7 ms | 0.005 ms | 0.23 ms |
| `allele` | 16,557 | 0.008 ms | 1.7 ms | 0.007 ms | 0.39 ms |
| `location` | 11,127 | 0.005 ms | 0.45 ms | 0.004 ms | 0.18 ms |

- **Where rs spends its time on the published set: compiling.** Each worker compiles the sections it meets, and `scv`,
  `vcv` and `rcv` take it 30 to 50 CPU-seconds each on that set: 1,128 CPU-seconds of compiling across the 10 workers on the
  delta, against 7 of validating. On the narrowed set, compiling takes 4 CPU-seconds in all. The cost is per worker, not
  per record, so it doesn't grow with the bundle.
- **A whole full bundle**, from the sample's per-section rates and the 10-31 full's section counts (an estimate):

  | Schema set | rs | python |
  | --- | ---: | ---: |
  | narrowed | 0.9 CPU-hours (about 6 minutes on 10 workers) | 390 CPU-hours |
  | published | 0.3 CPU-hours, plus about 2 minutes of compiling per worker (about 4 minutes on 10) | 120 CPU-hours |

  ClinVar-GKM's own estimate for the python engine was about 91 CPU-hours with the published schemas and 313 with the
  narrowed ones (2026-11-10, M2 Pro, one core per section).

## Reproducing

```sh
uv sync --extra reference                      # both engines
uv run python scripts/sample-bundle.py clinvar-gkm_2026-1031.json.gz sample.json.gz --every 100 --per-shape 2 \
    --bundle-schema tests/fixtures/clinvar-gkm/1.1.0/schema-set --version-property schema_version
uv run python scripts/validate-equivalence.py sample.json.gz out/ --version-property schema_version \
    --schema-set narrowed=tests/fixtures/schema-sets/clinvar-gkm-narrowed-a956b5a \
    --schema-set published=tests/fixtures/clinvar-gkm/1.1.0/schema-set
uv run python scripts/validate-equivalence.py tests/fixtures/civic/civic-assertion-9-bundle.json out-civic/ \
    --schema-set civic=tests/fixtures/civic/schema-set
uv run pytest tests/test_validate.py
```
