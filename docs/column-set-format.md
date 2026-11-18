# Column-set format

A **column set** is a YAML file that states the Parquet tables built from bundles of one bundle schema. A bundle schema
says what a bundle *may* hold; a column set says what is *built*: which properties become columns, and how each is read.
The tool compiles the two, a *pairing*, into a table spec
([ADR 0006](decisions/0006-sections-spec-compiled-from-schemas.md)).

From tool 2.0.0 (planned; ADR 0007) a column set is **optional**. With `--bundle-schema` and no `--column-set`,
`build`, `select`, `compile-spec` and `verify` take the schema's **default column set**, which the tool generates from
the schema by a fixed policy ([generate.md](generate.md)). A producer publishes its bundle schema; it publishes no column
set for the default. A column set you pass is one of two things:

- **the default**, written out (`generate` writes it): it compiles to the same table spec, so the build is labelled
  `default`;
- **a named projection** (ADR 0008 section 3): any other column set, which must say so with `projection: <name>`. The
  build is labelled with that name.

A column set that is neither is refused, with its differences from the default listed. Examples: ClinVar-GKM's 1.1.0
column set (`tests/fixtures/clinvar-gkm/1.1.0/columns.yaml`, projection `clinvar-gkm-producer`) and CIViC's
(`tests/fixtures/civic/civic.columns.yaml`, projection `civic-fixture`). The tool has no default bundle and ships no
producer's column set ([ADR 0009](decisions/0009-generic-gkm-bundle-tool-renamed.md)).

```sh
gkm-bundle-parquet build --bundle-schema my.schema.json --full my-bundle.json --out out/        # the default
gkm-bundle-parquet generate --bundle-schema my.schema.json -o default.columns.yaml             # write it out
gkm-bundle-parquet generate --check --bundle-schema my.schema.json --column-set my.columns.yaml  # is it the default?
gkm-bundle-parquet build --bundle-schema my.schema.json --column-set my.columns.yaml --full my-bundle.json --out out/
```

## A small example

```yaml
schema: urn:example:gkm-bundle:1.0                # the bundle schema's $id
projection: example-narrow                        # this set narrows the default, so it names itself
sections:
  gene: {id, type, name, mappings: {coding: {code, system}, relation}}
  variant: {id, type, name, gene: ->gene, start, end as stop_pos, extensions: [string, boolean]}
```

## The file

| Key | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | The `$id` of the bundle schema it pairs with. Any other schema is refused. |
| `sections` | yes | One entry per collection of the bundle: its properties, as shapes (below). |
| `projection` | for a projection | The projection's name (letters, digits, `.`, `_`, `-`). A set that compiles to the default is the default whatever it says. |
| `version_property` | no | The root property that holds the bundle's version, if it has one. There is no default name; with no column set, `--version-property` says it. |
| `collections` | no | Root properties that are collections, only where the schema can't say. |

**What is a collection.** A root property of the schema is a collection when it has `patternProperties`, or
`additionalProperties` holding a schema. It is one table, named as the property. Any other root property is a root value
(such as `metadata`): it needs no entry, and the tool keeps it as JSON text in the table `root_values`, name and value,
which exists only when a bundle has any. A root member the schema doesn't name is kept the same way, unless the schema's
root has `additionalProperties: false`; then it stops the build. The `version_property` is neither. Name a property
under `collections` only if the schema is silent about it, such as a bare `{"type": "object"}`.

## Tables

A table holds `id` (the bundle key) first, then one column per listed property, in schema order. There is no `ordinal`
column: the tool keeps no record order (ADR 0008 decision 1.1), and writes rows in key order.

- When the record's class **requires** `id`, the record's `id` must equal its key, or the build stops; it is not a
  column of its own.
- When the class **allows** `id` but doesn't require it, the table has `id_present` (BOOLEAN) after `id`: whether the
  record carries its id. Where it does, it must equal the key.
- When the class doesn't allow `id`, there is neither.

List `id` in an entry where the schema allows it; the rule above follows from the schema.

## Shapes

An entry in `sections` maps property to shape. A shape says what is at one position. Where the schema declares an array,
it says what each element is. Every shape works at any depth.

| Shape | Meaning | Use it |
| --- | --- | --- |
| *(nothing)*, as in `{id, type}` | What the schema says, by the default policy ([generate.md](generate.md)) | Almost always |
| `{a, b: <shape>}` | An inline object; only these properties | To narrow a general class to what you emit (a projection) |
| `->gene`, `->a\|b` | A pointer (`#/gene/<id>`) into those collections: a foreign key | A string that is always a bundle-local pointer, where the schema has no `pattern` saying so |
| `iri` | The generic IRI rule: the bare id of a `#/<collection>/<rest>` value, and its section; another string as written | A string that may be a bundle-local pointer, a CURIE or a URL |
| `string`, `integer`, `number`, `boolean` | That scalar | The schema allows several types, or leaves the position open; `integer` also narrows an integer-or-Range |
| `range` | An integer-or-Range (a two-element array of integers or nulls) | A position the schema leaves open that holds one |
| `<string shape>+json`, as in `->a\|b+json`, `iri+json`, `string+json` | A string (read as the left side says) or an object, kept as JSON text | A position that is a pointer in one record and an inline object in another |
| `json` | The value as compact JSON text | A position with no typed form: a map whose keys are data, an open object |
| `[<shape>]` | A list | The schema leaves the position open (where it says array, give the element's shape directly) |
| `extensions: [<shape>, ...]` | An extensions list: one shape per kind of value | To narrow the slots an extensions list has. List every kind: `[string, boolean, [string], {a: string}]` |

Inside a position the schema leaves open, every property needs a shape: `{code: string, count: integer}`; one with no
shape is JSON text.

**Renaming.** `start as loc_start` names the column differently from the property, for a property of the record (useful
where the name is awkward in SQL, although the tool quotes every name).

## Derived parts

Some positions are stored as more than one part. At the top level the parts are columns, inside a STRUCT they are fields
beside the property, and in a list each element is one STRUCT of its parts.

| Position | Top level, or inside a STRUCT | In a list: each element |
| --- | --- | --- |
| A pointer with one target | `<prop>` (the bare id) | VARCHAR (the bare id) |
| A pointer with several targets | `<prop>`, `<prop>_section` | `STRUCT(id, section)` |
| An IRI (`iri`) | `<prop>` (the bare id or the string), `<prop>_section` (the collection, or NULL) | `STRUCT(id, section)` |
| A string or an object (`+json`) | `<prop>` (the string, read as its shape says), `[<prop>_section]`, `<prop>_json` | `STRUCT(string or id, [section], json)` |
| An integer-or-Range | `<prop>` (the integer), `<prop>_min`, `<prop>_max` | `STRUCT(integer, min, max)` |

A derived name that equals a property name at the same level stops the compile (ADR 0007 decision 1.13): rename one.

**Extensions.** An array whose elements are closed objects with a string `name` and a `value` is read as an extensions
list, whatever the property is called: a list of `STRUCT(name, <the element's other fields>, value_<kind>...)`, with one
slot per JSON kind the value may take (`value_string`, `value_boolean`, `value_number`, `value_object`,
`value_string_array`, `value_object_array`, ...). A value that may be an open object, or whose slot would need derived
parts, has the single slot `value_json` (its JSON text). Each element fills only the slot of its value's kind.

## Foreign keys

A pointer with a declared target, from a schema `pattern` (`^#/<section>/` or `^#/(a|b)/`) or a `->` shape, is a
foreign key. The compiled spec lists each table's foreign keys (`foreign_keys`: the position, the target sections and,
with several targets, where the section is), in `table-spec.json` and in each file's key-value metadata
(`gkm_bundle_parquet.foreign_keys`). An IRI (`iri`, or `format: iri-reference` with no pattern) is not a foreign key.

## The rules the compile enforces

The compile stops, naming the section and position, on:

- a `schema` other than the schema's `$id`;
- a collection with no entry, or an entry for something that is not a collection;
- a property the schema doesn't have, unless the object is open;
- a shape contradicting the schema: `integer` on a string, an object on a scalar, a pointer where no string is allowed;
- a schema `pattern` beginning `^#/` that isn't `^#/<section>/` or `^#/(a|b)/`, or names something that isn't a
  collection; a `->` shape naming something that isn't a collection;
- a derived name equal to a property name at the same level; a key written twice;
- `key_order`, which tool 1.x read and 2.0.0 refuses: delete it.

Everything else the schema can't settle (an open object, a union of scalar types, a keyword the rules can't read, a
class that reaches itself, a map) is JSON text, and `generate` lists each such position.

A property you list that a bundle doesn't hold is fine: the column is NULL. A property a bundle holds that no column
covers stops the **build**, naming the record: nothing is dropped.

## Key order doesn't count

From tool 2.0.0 (planned; on `main`) the round trip is record-identical ([round-trip.md](round-trip.md)): a rebuilt
record has the same canonical form as its source record, so key order, array order and layout don't count, and a column
set says nothing about them. Columns come in schema order, then any positions the schema doesn't name (in an open
object) in the order this file lists them. Tool 1.x rebuilt a line-layout bundle byte for byte and needed a `key_order`
block naming the collections whose keys weren't sorted; 2.0.0 refuses a column set that has one, so delete the block.

## What a build checks

Every row is written back as JSON from its columns and its canonical form compared with the record's
([round-trip.md](round-trip.md)); any difference stops the build. This is what makes a wrong shape loud rather than a
wrong table. A JSON `null` stops it too, since a NULL column reads as absent.

The general reader holds a whole bundle in memory. For a bundle too large for that, write it in the line layout, which is
streamed.

## The schema

The bundle schema is a file, a URL, or a directory holding a schema set (`scripts/vendor-schemas.py` writes one from a
published schema and `scripts/vendor-git-schemas.py` from a commit of a local clone, so a build needs no network). Its
`$id` can have any form, such as a URN, and need not be the URL it was read from: a column set's `schema` pairs with the
`$id` the root declares. `$ref`s followed from it must be `https://w3id.org/` URLs, or JSON pointers within a file. The
README's [Schema sources](../README.md#schema-sources) says when a producer's w3id URL resolves, and why ClinVar-GKM's
1.1.0 URL serves a root whose `$id` differs from its fixture's.
