# The generated default

A bundle schema is enough to build Parquet: the tool **generates** the schema's default column set by a fixed policy
([ADR 0007](decisions/0007-default-generated-from-bundle-schema.md)), so a producer publishes its bundle schema and no
column set. `build`, `select`, `compile-spec` and `verify` take the default whenever `--bundle-schema` comes with no
`--column-set`. `generate` writes the default out, says what the policy settled, and checks a column set against it.
On `main` for 2.0.0 (planned).

```sh
# The default column set, to a file (or stdout), with the report on stderr
gkm-bundle-parquet generate --bundle-schema <file|URL|dir> [--version-property NAME] [-o FILE]

# Is this column set the default? Exit 0 if it is, 1 with the differences if not
gkm-bundle-parquet generate --check --bundle-schema <file|URL|dir> --column-set FILE
```

`--version-property` names the root property that holds the bundle's version (ClinVar-GKM: `schema_version`); there is
no default name. The written file is an ordinary column set ([column-set-format.md](column-set-format.md)): passing it
back with `--column-set` builds the same tables, labelled `default`.

## The policy

The policy has a version, `2.0` (`compiler.RULES_VERSION`), recorded in every file's key-value metadata
(`policy_version`) and in the manifest's `table_spec`. A change to what it generates is a new version.

- **Tables.** One table per collection, named as the collection, with `id` (the bundle key) first. Where the record's
  class allows `id` but doesn't require it, `id_present` (BOOLEAN) follows. No `ordinal`: rows are in key order.
- **Columns.** Every property the schema allows is a column, and inside an object a STRUCT field, at any depth, in
  schema order; nothing is flattened. A property set to `false` has none.
- **Pointers.** A string whose `pattern` is `^#/<section>/` or `^#/(a|b)/` is a foreign key into those collections, at
  any depth: the bare id, plus `<prop>_section` when there are several targets. A `pattern` beginning `^#/` of another
  form, or naming something that isn't a collection, stops generation.
- **IRIs.** A string with `format: iri-reference` and no such pattern takes the generic rule: `<prop>` is the bare id of
  a `#/<collection>/<rest>` value, or the string as written, and `<prop>_section` the collection, or NULL. It is not a
  foreign key.
- **A string or an object** is `<prop>` (the string, by the rules above) and `<prop>_json` (the object as JSON text).
- **An integer-or-Range**, known by its shape (a two-element array of integers or nulls) at any depth, is `<prop>`,
  `<prop>_min` and `<prop>_max`.
- **In a list**, an element with derived parts is one STRUCT of them (`id`, `string`, `json`, `section`, `integer`,
  `min`, `max`).
- **Extensions** are known by their shape, a closed object with a string `name` and a `value`: a list of STRUCTs with
  `name`, the element's other fields, and one `value_<kind>` slot per JSON kind the value may take; `value_json` when the
  value may be an open object. A `oneOf` of closed extension classes is one element type.
- **JSON text.** Anything else the schema can't settle is the value as compact JSON text (VARCHAR): an open object, a
  map whose keys are data, a union of scalar types, a keyword the rules can't read (`if`, ...), a position the schema
  says nothing about, a class that reaches itself (where it re-enters).
- **Stops.** Besides the pointer patterns above, a derived name equal to a property name at the same level stops
  generation (rename one in the schema), and so does a `$ref` that resolves to nothing.

The build then checks every row: a record whose `id` differs from its key, or that holds a property no column covers,
stops it, and so does any row that doesn't write back to its record ([round-trip.md](round-trip.md)).

## The report

`generate` writes to stderr every position the policy settled in a way a reader should know about:

- each IRI position with no pointer pattern (the generic rule; no foreign key);
- each JSON-text position, with the reason;
- each record class that is open (its declared properties are columns; a record with another property stops the build).

Each line is `<section>: <position>`, the position written as in a JSON path (`mappings[*].coding.iris[*]`).

## Default or projection

A column set passed with `--column-set` compiles to the default (the same `table_spec_sha256`) or must declare itself a
**named projection** with `projection: <name>` (ADR 0008 section 3); otherwise it is refused, with up to 20 of its
differences listed. `generate --check` lists them all. The build records which it is: `column_set_kind` (`default` or
`projection`) and `projection` (the name) in each file's key-value metadata and in the manifest's `table_spec`. A
projection is built and checked as before; its own round trip to a projected bundle (ADR 0008 section 3) is later work.

## Foreign keys

A table's foreign keys (from a schema `pattern`, or a column set's `->` shape) are in `table-spec.json` (`foreign_keys`
on each table: `position`, `targets` and, with several targets, `section`, where the section is held) and in each file's
key-value metadata (`gkm_bundle_parquet.foreign_keys`, JSON).

## On ClinVar-GKM

Measured on the ClinVar-GKM 2026-10-31 delta (495,547 records, 17 sections), 2026-11-16:

| Schema | Tables | Columns | STRUCT leaves | Top-level JSON-text columns | JSON-text positions | IRI positions | Foreign keys |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Published 1.1.0 (w3id) | 20 | 339 | 901 | 43 | 181 | 84 | 0 |
| Narrowed profiles (`tests/fixtures/schema-sets/clinvar-gkm-narrowed-a956b5a`) | 20 | 163 | 629 | 0 | 51 | 29 | 26 |

The published 1.1.0 schema declares no pointer patterns and leaves many classes open, so its default is wide and keeps
much as JSON text; the narrowed profiles close the classes, set unused properties to `false` and declare each pointer's
target, so their default is narrow and typed. Both builds round-trip record-identical (`verify --roundtrip --full`) and
pass `verify --bundle-recompute`. The narrowed set's remaining JSON-text positions are nested `Extension.extensions`
(the class reaches itself), a few open extension values, and the string-or-object positions of Cat-VRS constraints.
