# 0007 — The default set is generated from the bundle schema

- **Status:** Proposed
- **Date:** 2026-11-16
- **Plan (if any):** [`docs/plans/2026-11-16-stabilize-and-2.0.md`](../plans/2026-11-16-stabilize-and-2.0.md) §4 (this
  ADR) and §6 step 2 (the work that implements it). Builds on [ADR 0008](0008-end-state-reversible-default-custom-projections-filters.md)
  (decisions 1.1–1.5, 3) and [ADR 0009](0009-generic-gkm-bundle-tool-renamed.md); amends
  [ADR 0006](0006-sections-spec-compiled-from-schemas.md) (decisions 2 and 3). Replaces the draft of closed
  [PR #13](https://github.com/clingen-data-model/gkm-bundle-parquet/pull/13) (`363ed1f`).

## Context

ADR 0008 makes the default set the product: one per bundle schema, generated from the schema alone, with a
record-identical round trip checked by canonical record digests (1.2, 1.3), no `ordinal` (1.1), optional narrowing
(1.4) and foreign keys declared by `pattern` (1.5). It leaves the generation policy to this ADR. The first draft
(PR #13, 2026-11-05/06) predates ADR 0008: it promised a byte-identical round trip through `key_order`, kept `ordinal`
and required ClinVar-GKM to narrow its profiles. The maintainer closed it on 2026-11-16 (plan §2 decision 1). This
ADR keeps its generation policy and its pointer rule, and takes the rest from ADR 0008.

**What the compiler does today** (`main` @ `ce1cfd6`, `src/clinvar_gkm_parquet/`):

- It needs a hand-written column set: `build --bundle-schema` without `--column-set` is refused (`build.py:1272-1273`).
- It stops where the schema can't settle a position (`compiler.py:21-23`): a string-or-object (`compiler.py:325-330`),
  an integer-or-Range inside a value (`:315-322`), a map whose keys are data (`:307-310`), a keyword it can't read
  (`:311-312`), a class that reaches itself (`:241-244`, `:356-358`), an extension element that isn't a closed
  `{name, value}` or whose value is open (`:486-492`).
- JSON text is allowed only on a record property (`:395-396`), and so is `<prop>_section` (`:642-656`).
- Pointers are typed only by the column set's `->a|b` (`:69`, `:424-433`). The compiler reads neither `pattern` nor
  `format`.
- It ignores `<prop>: false`: `_own` skips a declaration that isn't an object (`:281`), so the base's declaration stands.
- Every table ends with `ordinal` (`:590-591`) and carries a key order (`:574-575`, `:592`; `columnset.py:20`).
- The rules version is `1.0` (`:51`), written as the compiled spec's `spec_version` (`:732`).

**Evidence** (ClinVar-GKM pilot and generator prototype, 2026-11-05 to 2026-11-09; scratch, not committed):

- **Width.** The generated default of the unnarrowed ClinVar-GKM 1.1.0 schema has about 253 columns and 981 leaves
  (counted with the key and `ordinal`), against 169 and 425 in the hand-written column set. CIViC's: 193 and 981,
  against 126 and 198. Every fixture rebuilt JSON-equal.
- **What 1.1.0 leaves open:** 67 string-or-object positions, 58 open extension values, 59 self-reaching positions (all
  `Extension.extensions`), 5 nested integer-or-Range. CIViC adds 4 maps keyed by data and one `if`.
- **Narrowing passes through the GKM tooling.** `gkm-metaschema` accepts `<prop>: false` and a string-or-object
  restated as only the object (it warns `LostIriReferenceWarning`). The `ga4gh.gkm` validator enforces both, and
  enforces an `iriReference` with a `pattern`.
- **Narrowed ClinVar-GKM.** Its 67 string-or-object positions become 27 pointers, 21 inline objects and 19 `false`, so
  its default has no `<prop>_json` column. It closes extension values as standalone closed extension classes (each
  fixing `name` by `const`) in the `extensions` items' `oneOf`. The pilot's patched compiler needed four rules (N1–N4)
  to read them.
- **Neither published schema declares a pointer pattern.** No `^#/` pattern appears in `schemas/1.1.0/` or
  `tests/fixtures/civic/schema-set/`. Both reach gkm-core's `iriReference` (`type: string`, `format: iri-reference`;
  `schemas/1.1.0/ga4gh/schema/gkm-core/1.3.0/json/iriReference`).

## Decision

### 1. The default policy

1. **The default is a versioned policy, and this is version 2.0.** One number names the policy, the compiler's rules
   version (`RULES_VERSION`) and the table spec `sections`; it moves from `1.0` to `2.0` with this ADR. A change to the
   policy bumps it by the table in `.claude/rules/table-spec.md`. The default of a bundle schema depends only on the
   schema set and the policy version: the same two always give the same bytes.
2. **One table per collection**, found by ADR 0006's root rule; root values stay in `root_values`. The key column `id`
   comes first. Then every property the record schema allows, through its whole `$ref`, `allOf`, `oneOf` and `anyOf`
   closure, one column per name (ADR 0008 1.1), in schema order: a union contributes its first branch's properties,
   then each further branch's new ones. No `ordinal`, no key order.
3. **Nothing is flattened.** An object is a STRUCT and an array a list, at every depth, with every allowed property a
   field.
4. **`false` removes a property.** A property any `allOf` member, or the class itself, declares `false` is not a column
   or a field. `additionalProperties: false` and `unevaluatedProperties: false` close an object, as today.
5. **`id`.** The key column holds the bundle key. Where the record schema requires `id`, each record's `id` must equal
   its key. Where it allows `id` without requiring it, an `id_present` BOOLEAN says whether the record carried one.
   Where it declares `id` `false` or doesn't allow it, there is neither. A record whose `id` differs from its key stops
   the build.
6. **A pointer is declared by its `pattern`** (ADR 0008 1.5). A string position whose schema has a `pattern` beginning
   `^#/<section>/` or `^#/(<section>|<section>…)/`, each name a collection of the bundle, holds the bare id. A
   `<prop>_section` beside it holds the section when there are several targets. This applies at any depth and in
   lists. These positions are the table's foreign keys. A pattern beginning `^#/` in any other form, or naming
   something that isn't a collection, stops generation.
7. **An undeclared IRI keeps the generic rule** (ADR 0008 1.4). A string position whose schema has
   `format: iri-reference` and no pointer pattern gets `<prop>` and `<prop>_section`. A value of the form
   `#/<section>/<rest>`, where `<section>` is a collection, is stored as `<rest>` with its section; any other string is
   stored as written, with a NULL section. A non-NULL section rebuilds `#/<section>/<rest>`.
8. **String or object is split.** A position that may hold a string or an object is `<prop>`, VARCHAR, for the string
   (with rule 6 or 7 if it applies), and `<prop>_json`, the object as compact JSON text. A producer that always writes
   one form narrows the position, and then it is one typed column or STRUCT.
9. **A Range is recognised by its shape, at any depth** (ADR 0009 §4): an array of exactly two integers or nulls, as
   the compiler reads it today (`compiler.py:292-302`). An integer-or-Range is `<prop>`, `<prop>_min` and `<prop>_max`:
   three columns at the top level, three fields inside a STRUCT.
10. **In a list, derived parts are parallel lists.** `<prop>_section`, `<prop>_json`, `<prop>_min` and `<prop>_max` of
    a list position are lists of the same length as `<prop>`, as `<prop>_section` is today (`compiler.py:642-656`).
11. **Extensions are recognised by shape** (ADR 0009 §4): a property named `extensions` whose elements are objects
    with `name` and `value`. It is a list of STRUCTs: `name`, the element's other allowed fields (`id`, `description`
    for gkm-core's `Extension`), then one `value_<kind>` slot per JSON kind its value can take, in the order of
    `compiler.py:57-68`. Object values from several classes merge into one `value_object` STRUCT by the union rule. An
    open value is a `value_json` slot of JSON text. A `oneOf` of closed extension classes in `items` is read as one
    element type whose value is the union of the classes' values. An element's own `extensions` re-enters `Extension`,
    so it is JSON text (rule 12).
12. **Where the schema can't settle a position, it is JSON text.** A map whose keys are data, a keyword the rules
    can't read, an open object, a position with no shape keyword, a union of scalar types, and a class that reaches itself (at the position where it re-enters) keep the property's name and hold
    compact JSON text, as VARCHAR, at any depth. Generation never stops on a position.
13. **Names are the schema's.** The tool quotes every name. A derived name (`<prop>_section`, `<prop>_json`,
    `<prop>_min`, `<prop>_max`, `id_present`, `value_<kind>`) that equals a property name at the same level stops
    generation, naming both.

Generation stops only on rule 6's bad pattern, rule 13's collision, or a root that doesn't follow ADR 0009 §4. A record
the default can't reproduce still stops the build, through the canonical row check (ADR 0008 1.3).

### 2. Generating, building, recording

1. **`generate --bundle-schema <file|url|dir>`** writes the default column set, in the column-set format, and reports:
   every IRI position without a pointer pattern (ADR 0008 1.5), and every JSON-text position, with the reason.
2. **`build --bundle-schema` needs no `--column-set`.** With none, it compiles the generated default in memory. The
   compiler stays the single path from a column set to a table spec (ADR 0006 decision 1).
3. **Foreign keys are recorded.** Each table lists its rule-6 positions (the column, or nested field path, and the
   target sections) in `table-spec.json` and in every file's key-value metadata. Rule-7 positions aren't foreign keys.
4. **Every set says what it is.** The manifest and every file's key-value metadata record the policy version and
   whether the set is the default or a projection (with the projection's name).

### 3. Compliance

1. **A column set complies when it compiles, under the same policy version, to exactly the generated default's
   tables:** an equal `table_spec_sha256` (ADR 0006 decision 7). A compliant set is the default, and is recorded as
   such. `generate --check <column-set>` reports compliant, or lists each difference with its position and both sides.
2. **Anything else is a named projection** (ADR 0008 §3). It declares itself a projection, with a name, in the column
   set, and is built, labelled and rebuilt as ADR 0008 §3 says. A column set that neither complies nor declares itself
   a projection is refused, with the differences listed.
3. **Narrowing is the producer's way to change the default** (ADR 0008 1.4): `false`, an object-only restatement, a
   pointer pattern, a closed extension class. Each changes the schema, so it arrives as a new schema version with a new
   default. None is needed for the round trip.

## Consequences

- **Any bundle schema has a default with no column set to write.** The default can't drift from a producer's code, and
  ADR 0006's "a column set is only as complete as the records it has been run on" no longer applies to it. A record
  holding something its schema doesn't allow still stops the build.
- **Unnarrowed schemas give wide tables.** About 253 columns and 981 leaves for ClinVar-GKM 1.1.0 (prototype), most
  NULL in every row. Parquet stores an all-NULL column cheaply, but `DESCRIBE` is long, and JSON-text positions need
  parsing in a query.
- **Narrowed schemas give narrow, typed tables.** ClinVar-GKM's narrowed profiles (clinvar-gkm #215) would give no
  `<prop>_json` column and 27 declared pointer positions. They arrive as a new ClinVar-GKM schema version, and the
  table shape changes then, without a tool release.
- **`sections` 2.0 is a table-spec MAJOR:** no `ordinal`, no key order, new derived columns, and a different column
  list for every section.
- **The column-set format changes.** It gains the shapes the default needs (the string-or-object split, the generic
  IRI rule, JSON text inside a value, a Range at any depth, extension elements with more than `name` and `value`) and a
  projection declaration; `key_order` goes. `docs/column-set-format.md` changes with the code.
- **ClinVar-GKM's hand-written 1.1.0 column set** doesn't comply. It becomes a test fixture (ADR 0009 decision 1),
  usable as a projection. The golden test (`tests/test_compiler.py` against `tests/fixtures/sections-golden/`) is
  re-based in the same work.
- **Compiler work** (plan §6 step 2): read `false`, `pattern` and `format`; the string-or-object split; JSON text and
  derived parts at any depth; extension elements with extra fields and `value_json`; `id_present`; foreign keys in
  `table-spec.json` and key-value metadata; `generate` and `generate --check`; drop `ordinal` and `key_order`; set
  `RULES_VERSION` to `2.0`. The generic-core test (ADR 0006 decision 8) still applies: no producer is named.
- **On acceptance,** ADR 0006 gets an "Amended by ADR 0007" note.

## Earlier ADRs touched

| ADR | What changes |
| --- | --- |
| 0006 | Decision 2: the column set is required only for a projection; the default is generated. Decision 3: the stops on string-or-object, nested integer-or-Range, maps, unreadable keywords and self-reaching classes become rules 8, 9 and 12; `->a\|b` stays, and a schema `pattern` now says the same. |
| 0008 | Nothing changes. This ADR is the policy 1.1 and 1.4 refer to, and fills in the generic rule (1.4) and pointer declaration (1.5). Its table entry for "0007 (proposed)" is carried out here. |
| 0009 | The column set "generated from the schema once ADR 0007's generator lands" (decision 1) is this ADR's `generate`. |
| 0002 | The default's table-spec version is the policy version; the compatibility matrix states it. |

## Open questions for the maintainer

1. **Which strings get the generic IRI rule (1.7):** only `format: iri-reference` (proposed), or every string
   position? Every string is safe for the round trip, but adds a `_section` column to every string.
2. **Lists of split or derived positions (1.10):** parallel lists (proposed, as today's top-level pointers), or a list
   of STRUCTs `{<prop>, <prop>_section, <prop>_json}` per element, which keeps the parts together for `UNNEST`.
3. **`id` (1.5):** is `id_present` the right shape, and should a record whose `id` differs from its key stop the
   build (proposed), or be stored?
4. **Extension value slots (1.11):** one slot per JSON kind (proposed, as today), or, where every element class fixes
   `name` by `const` (the narrowed ClinVar-GKM form), one typed slot per extension name?
5. **The version property:** the default names none unless `--version-property` is given, so it is otherwise a root
   value (which round-trips). Should `generate` infer it from the schema instead?
6. **Projections that drop nothing:** a non-compliant column set that only renames or retypes (ClinVar-GKM's 1.1.0 set
   is close to this) is labelled lossy under 3.2. Is that acceptable, or does it need a third label?
7. **JSON text type:** VARCHAR (proposed, as today's `json` columns), or DuckDB's `JSON` logical type.
