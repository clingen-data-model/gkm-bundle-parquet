"""Compile a table spec from a bundle schema set and a column set, and generate a schema's default column set, for
any producer's bundles (ADR 0006, ADR 0007).

`compile_sections(schema_set, column_set)` returns a spec mapping, the format of a tables.yaml (spec.py).
`default_column_set(schema_set, version_property)` is a schema's default column set (ADR 0007): every collection
with every property its record schema allows, each left to the schema; `generate_default` also compiles it and
reports the positions the policy settled as JSON text or by the generic IRI rule. The schema says what a position
may hold; a column set says what is built from it. Its format is in docs/column-set-format.md. No producer,
section, class or property is named here, except the structural names the rules read (`extensions`, `id`, `name`,
`value`).

The rules (the default policy, ADR 0007 decision 1; its version is RULES_VERSION):

- A root property of the schema is a collection when the schema gives it one record class (`patternProperties`, or
  `additionalProperties` holding a schema), or when the column set says so; the version property is neither;
  anything else is a root value, which the tool keeps as JSON text.
- A collection is one table, in schema order: the key column `id` (the bundle key), then one column per property
  the column set lists. `id` listed means the record may carry its own, equal to the key: where the record schema
  requires it, the key stands for it; where it only allows it, an `id_present` BOOLEAN says whether the record had
  one. A property any `allOf` member or the class declares `false` is no column.
- An object is a STRUCT and an array a list, at every depth. A union of objects is one merged STRUCT: the first
  branch's fields, then each further branch's new fields.
- A string whose schema has a `pattern` `^#/<section>/` or `^#/(<a>|<b>)/` is a pointer: the bare id, plus a
  `<prop>_section` when it has several targets (a foreign key); `->a|b` in the column set says the same. A string
  with `format: iri-reference` and no such pattern takes the generic IRI rule: `<prop>` holds the bare id of a
  `#/<collection>/<rest>` value, or the string as written, and `<prop>_section` the collection, or NULL.
- A position that may hold a string or an object is `<prop>` (the string, by the rules above) and `<prop>_json`
  (the object as compact JSON text). An integer-or-Range (a Range is known by its shape, a two-element array of
  integers or nulls) is `<prop>`, `<prop>_min`, `<prop>_max`. These parts are columns at the top level, fields
  beside the property inside a STRUCT, and, in a list, one STRUCT per element whose fields are named by part (`id`,
  `string`, `json`, `section`, `integer`, `min`, `max`).
- An `extensions` list whose elements are closed objects with `name` and `value` is a list of STRUCTs: `name`, the
  element's other fields, then one `value_<kind>` slot per JSON kind the value can take, or a `value_json`
  slot when the value is open.
- Anything else the schema can't settle is the value as compact JSON text (VARCHAR): a map whose keys are data, a
  keyword the rules can't read, an open object, a position with no shape keyword, a union of scalar types, a class
  that reaches itself (where it re-enters). Generation stops only on a pointer pattern of another form or naming
  something that isn't a collection, and on a derived name equal to a property name at the same level.

A column set may also state a position's shape (docs/column-set-format.md): a property list narrows an object, and
`->a|b`, `iri`, `json`, `range`, `<string shape>+json` and the scalar words say how a position reads. A shape that
contradicts the schema stops the compile.

How a schema position is read: `$ref` is followed. `allOf` members all hold, so a property two of them declare
holds both declarations (a profile narrows its base). `oneOf` and `anyOf` branches are a union. A member or
declaration with no shape keyword (`contains`, `minItems`) says nothing of the columns; `required`, `pattern` and
`format` are read. A keyword the rules can't read (`if`, `patternProperties`, ...) makes the position opaque.

Order: a table has its columns and fields in schema order, then the open positions in column-set order. The round
trip doesn't depend on it: it is record-identical, so key order doesn't count (ADR 0008 decision 1.2).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from functools import reduce

import duckdb

from .bundle import ROOT_VALUES_TABLE
from .errors import CompileError, SchemaSetError
from .schemas import SchemaSet, split_ref

SPEC_NAME = "sections"
RULES_VERSION = "2.0"  # the version of these rules: the default policy's (ADR 0007 decision 1.1)
EXTENSIONS, ID, ID_PRESENT = "extensions", "id", "id_present"
WRITER = {"compression": "snappy", "row_group_size": 122880}
JSON_TEXT = "json"  # the column set's shape for a value kept as JSON text
IRI = "iri"  # the column set's shape for the generic IRI rule
RANGE = "range"  # the column set's shape for an integer-or-Range
EXTENSION = "extension"  # a `parts` entry: the position is an extension STRUCT (spec.py)

_DUCK = {"string": "VARCHAR", "integer": "BIGINT", "boolean": "BOOLEAN", "number": "DOUBLE"}
_SLOT_ORDER = (
    "string",
    "boolean",
    "integer",
    "number",
    "object",
    "string_array",
    "boolean_array",
    "integer_array",
    "number_array",
    "object_array",
    "json",
)
_POINTER_SHAPE = re.compile(r"->(\w+(?:\|\w+)*)")
_SPLIT_SHAPE = re.compile(r"(string|iri|->\w+(?:\|\w+)*)\+json")
_RENAME = re.compile(r"(\w+) as (\w+)")
_NAME = r"[A-Za-z0-9_-]+"
_POINTER_PATTERN = re.compile(rf"\^#/(?:({_NAME})|\(({_NAME}(?:\|{_NAME})*)\))/")
# Keywords that say what a value is. Anything else a node holds is validation or annotation, except _OPAQUE and
# _META (read, but saying nothing about the columns on their own).
_SHAPE_KEYS = frozenset({"$ref", "type", "allOf", "oneOf", "anyOf", "properties", "items", "const", "enum"})
_META_KEYS = frozenset({"required", "pattern", "format"})
_OPAQUE = (
    "if",
    "then",
    "else",
    "patternProperties",
    "dependentSchemas",
    "propertyNames",
    "prefixItems",
    "$dynamicRef",
)
_STRIP = "strip ^#/[^/]+/"
_SLOT_TRANSFORM = "{name, value} -> {name, value_<kind>}: value goes to the slot of its JSON kind, at every depth"
_NESTED_SLOT_TRANSFORM = "nested extensions: {name, value} -> {name, value_<kind>} by the JSON kind of value"


def encoding_parts(enc: dict) -> list[tuple[str, str]]:
    """The parts a position's encoding (spec.py `parts`, `encoding`) stores, in order, with its DuckDB type: the
    first is the main part (`<prop>`), the others are `<prop>_<part>` beside it, or a list element's fields."""
    form = enc["form"]
    if form == JSON_TEXT:
        return [("json", "VARCHAR")]
    if form == RANGE:
        return [("integer", "BIGINT"), ("min", "BIGINT"), ("max", "BIGINT")]
    if form in ("pointer", IRI):
        string, has_object = form, False
    elif form == "string-or-object":
        string, has_object = enc["string"], True
    else:
        raise CompileError(f"unknown encoding {enc!r}")
    out = [("id" if string in ("pointer", IRI) else "string", "VARCHAR")]
    if string == IRI or (string == "pointer" and len(enc.get("targets") or ()) > 1):
        out.append(("section", "VARCHAR"))
    if has_object:
        out.append(("json", "VARCHAR"))
    return out


def object_fields(name: str, enc: dict) -> list[tuple[str, str, str]]:
    """(field name, part, type) of a property `name` with encoding `enc` inside an object: `<name>` for the main
    part, `<name>_<part>` for the others (ADR 0007 decisions 1.6-1.9)."""
    parts = encoding_parts(enc)
    return [(name if i == 0 else f"{name}_{p}", p, t) for i, (p, t) in enumerate(parts)]


@dataclass(frozen=True)
class _Both:
    """A position two `allOf` members declare: both hold, the later narrowing the earlier."""

    earlier: tuple
    later: tuple


_Decl = tuple[dict, str] | _Both  # a schema node and the $id of the file it sits in; or two declarations in one


@dataclass
class _Alts:
    """What a schema position allows: at most one merged object, one array, and any scalars."""

    scalars: list[str] = field(default_factory=list)  # JSON types
    items: list[_Decl] | None = None  # an array's element declarations; []: no `items`, so open
    fields: dict[str, list[_Decl]] | None = None  # an object's properties, each with its declarations
    closed: bool = False  # the object allows no other property
    strict: bool = False  # closed by its own `additionalProperties: false`, which sees only its own properties
    map: bool = False  # an object whose keys are data: `additionalProperties` holds a schema
    range: bool = False  # a two-element array of integers or nulls: a VRS Range, by its shape
    opaque: str | None = None  # a keyword the rules can't read
    refs: set[str] = field(default_factory=set)  # the object classes this position went through
    required: frozenset[str] = frozenset()  # the object's required properties
    banned: frozenset[str] = frozenset()  # properties declared `false`
    string: tuple | None = None  # how a string here reads: ("pointer", targets), ("iri",), or None (as written)

    def says_something(self) -> bool:
        return bool(self.scalars or self.range) or self.items is not None or self.fields is not None

    def split(self) -> list[_Alts]:
        """One _Alts per alternative: each scalar, the array, the object."""
        out = [
            _Alts(scalars=[t], refs=self.refs, string=self.string if t == "string" else None) for t in self.scalars
        ]
        if self.items is not None:
            out.append(_Alts(items=self.items, refs=self.refs))
        if self.fields is not None:
            out.append(
                _Alts(fields=self.fields, closed=self.closed, map=self.map, refs=self.refs, required=self.required)
            )
        return out


def _has_shape(node: dict) -> bool:
    return any(k in node for k in _SHAPE_KEYS) or any(k in node for k in _OPAQUE)


def _has_meta(node: dict) -> bool:
    return any(k in node for k in _META_KEYS)


def _says(decl: _Decl) -> bool:
    return isinstance(decl, _Both) or _has_shape(decl[0]) or _has_meta(decl[0])


def _both(earlier: list[_Decl], later: list[_Decl]) -> list[_Decl]:
    """A position declared twice in an `allOf`. A declaration with no shape keyword (`contains`) says nothing."""
    later = [d for d in later if _says(d)]
    if not later:
        return earlier
    return [_Both(tuple(earlier), tuple(later))] if earlier else later


def _meet(a: tuple | None, b: tuple | None) -> tuple | None:
    """Both string readings hold: a pointer pattern beats the IRI format; two pointer patterns hold together."""
    if a is None or b is None:
        return a or b
    if a[0] == "pointer" and b[0] == "pointer":
        return ("pointer", tuple(t for t in a[1] if t in b[1]))
    return a if a[0] == "pointer" else b


def _join(a: tuple | None, b: tuple | None) -> tuple | None:
    """Either string reading holds: a string read as written in either branch is read as written."""
    if a is None or b is None:
        return None
    if a[0] == "pointer" and b[0] == "pointer":
        return ("pointer", a[1] + tuple(t for t in b[1] if t not in a[1]))
    return ("iri",)


def _filtered(fields: dict | None, banned: frozenset[str]) -> dict | None:
    return None if fields is None else {k: v for k, v in fields.items() if k not in banned}


def _refine(a: _Alts, b: _Alts) -> _Alts:
    """Both hold (`allOf`): what both allow, and a property both declare holds both declarations. Two members that
    each close themselves with `additionalProperties: false` allow only what both name. A property either declares
    `false` is gone."""
    meta = {
        "refs": a.refs | b.refs,
        "opaque": a.opaque or b.opaque,
        "required": a.required | b.required,
        "banned": a.banned | b.banned,
        "string": _meet(a.string, b.string),
    }
    if not a.says_something():
        out = replace(b, **meta)
    elif not b.says_something():
        out = replace(a, **meta)
    else:
        out = _Alts(range=a.range and b.range, map=a.map or b.map, **meta)
        out.scalars = [t for t in a.scalars if t in b.scalars]
        if a.items is not None and b.items is not None:
            out.items = _both(a.items, b.items) if a.items else b.items
        if a.fields is not None and b.fields is not None:
            out.fields = dict(a.fields)
            for name, decls in b.fields.items():
                out.fields[name] = _both(out.fields.get(name, []), decls)
            if a.strict and b.strict and a.fields and b.fields:
                out.fields = {k: v for k, v in out.fields.items() if k in a.fields and k in b.fields}
            out.closed = a.closed or b.closed
            out.strict = a.strict or b.strict
    out.fields = _filtered(out.fields, out.banned)
    return out


def _union(a: _Alts, b: _Alts) -> _Alts:
    """Either holds (`oneOf`, `anyOf`): the union rule. The first branch's fields, then each further one's new. A
    property one branch declares `false` is allowed when the other declares it."""
    strings = [x.string for x in (a, b) if "string" in x.scalars]
    a_names, b_names = set(a.fields or ()), set(b.fields or ())
    out = _Alts(
        opaque=a.opaque or b.opaque,
        refs=a.refs | b.refs,
        range=a.range or b.range,
        map=a.map or b.map,
        banned=(a.banned & b.banned) | (a.banned - b_names) | (b.banned - a_names),
        string=reduce(_join, strings) if strings else None,
    )
    if a.fields is None:
        out.required = b.required
    elif b.fields is None:
        out.required = a.required
    else:
        out.required = a.required & b.required
    out.scalars = a.scalars + [t for t in b.scalars if t not in a.scalars]
    if a.items is not None or b.items is not None:
        # An open array in any branch leaves the union open.
        open_items = a.items == [] or b.items == []
        out.items = [] if open_items else (a.items or []) + (b.items or [])
    if a.fields is not None or b.fields is not None:
        out.fields = {k: list(v) for k, v in (a.fields or {}).items()}
        for name, decls in (b.fields or {}).items():
            out.fields.setdefault(name, []).extend(decls)
        out.closed = (a.closed or a.fields is None) and (b.closed or b.fields is None)
    return out


def _unsettled(a: _Alts) -> str | None:
    """Why the schema can't settle a position's form (ADR 0007 decision 1.12), or None."""
    if a.map:
        return "a map whose keys are data"
    if a.opaque:  # a keyword, or a sentence saying why
        return a.opaque if " " in a.opaque else f"uses {a.opaque}, which the rules can't read"
    if not a.says_something():
        return "the schema says nothing about its value"
    return None


@dataclass
class _Value:
    """A compiled position: its DuckDB type (of its main part, when it has an encoding), and what the column, a
    STRUCT or a list holding it needs to know of it."""

    type: str
    slot: str  # the JSON type's name in an extension slot: string, boolean, integer, number, object, <k>_array
    pointers: dict[str, list[str]] = field(
        default_factory=dict
    )  # one-target pointers inside: position -> [target]
    parts: dict[str, dict] = field(default_factory=dict)  # encoded positions inside: position -> encoding
    extension: bool = False  # holds an extension struct
    enc: dict | None = None  # this value's own encoding: JSON text, a Range, a pointer's or IRI's parts, a split


@dataclass(frozen=True)
class RootParts:
    """What the root of a bundle may hold, by its schema."""

    collections: tuple[str, ...]  # in schema order
    other_values: bool  # the root may hold values beyond the collections and the version property


@dataclass(frozen=True)
class Note:
    """A position the default policy settled in a way a producer may want to narrow (`generate` reports it)."""

    section: str
    position: str
    kind: str  # "json": kept as JSON text; "iri": the generic IRI rule, no pointer pattern; "open": open record
    reason: str


def _shift(prefix: str, positions: dict) -> dict:
    return {f"{prefix}{p}": v for p, v in positions.items()}


class _Compiler:
    def __init__(self, schema_set: SchemaSet, column_set: dict):
        self.schema_set = schema_set
        self.column_set = column_set
        self.version_property = column_set.get("version_property")
        self.parts = root_parts(schema_set, self.version_property, column_set.get("collections") or ())
        self.section_names = list(self.parts.collections)
        self.section = ""
        self.path: frozenset[str] = frozenset()  # the classes open on the way to the current position
        self.notes: list[Note] = []

    # --- errors and notes ----------------------------------------------------------------------------------------

    def stop(self, pos: str, message: str, must: str | None = None, *, fix: str | None = None) -> CompileError:
        """`must`: what a column set must state instead; `fix`: what must change when no column set can help."""
        where = f"schema {self.schema_set.bundle_id}: section {self.section}: position {pos}"
        tail = f"; the column set must {must}" if must else f"; {fix}" if fix else ""
        return CompileError(f"{where}: {message}{tail}")

    def bad_entry(self, pos: str, message: str) -> CompileError:
        return CompileError(
            f"column set for {self.schema_set.bundle_id}: section {self.section}: position {pos}: {message}"
        )

    def note(self, pos: str, kind: str, reason: str) -> None:
        self.notes.append(Note(self.section, pos, kind, reason))

    # --- reading the schema --------------------------------------------------------------------------------------

    def read(self, decls: list[_Decl], pos: str) -> _Alts:
        """What the declarations of one position allow, as a union."""
        alts = [self._decl(d, pos) for d in decls if _says(d)]
        return reduce(_union, alts) if alts else _Alts()

    def _decl(self, decl: _Decl, pos: str) -> _Alts:
        if isinstance(decl, _Both):
            return _refine(self.read(list(decl.earlier), pos), self.read(list(decl.later), pos))
        return self._node(decl[0], decl[1], (), pos)

    def _node(self, node: dict, base: str, stack: tuple[str, ...], pos: str) -> _Alts:
        out = _Alts()
        ref = node.get("$ref")
        if ref is not None:
            doc_id, pointer = split_ref(ref, base)
            key = f"{doc_id}#{pointer}"
            if key in stack:  # a $ref cycle with no property between: no shape to read (ADR 0007 1.12)
                return _Alts(opaque=f"class {key.removesuffix('#')} reaches itself through $ref alone")
            try:
                target, file_id = self.schema_set.resolve(ref, base)
            except SchemaSetError as e:
                raise self.stop(pos, f"unknown $ref: {e}") from e
            out = self._node(target, file_id, (*stack, key), pos)
            if out.fields is not None:  # only an object can reach itself through a property
                out.refs = out.refs | {key}
        for member in node.get("allOf", ()):
            if _has_shape(member) or _has_meta(member):
                out = _refine(out, self._node(member, base, stack, pos))
        out = _refine(out, self._own(node, base, stack, pos))
        for keyword in ("oneOf", "anyOf"):
            branches = [self._node(b, base, stack, pos) for b in node.get(keyword, ()) if _has_shape(b)]
            if branches:
                out = _refine(out, reduce(_union, branches))
        return out

    def _own(self, node: dict, base: str, stack: tuple[str, ...], pos: str) -> _Alts:
        """What the node itself states, without following $ref, allOf, oneOf or anyOf."""
        required = node.get("required")
        out = _Alts(
            opaque=next((k for k in _OPAQUE if k in node), None),
            required=frozenset(r for r in required if isinstance(r, str))
            if isinstance(required, list)
            else frozenset(),
            string=self._string_reading(node, pos),
        )
        extra = node.get("additionalProperties")
        closes = extra is False or node.get("unevaluatedProperties") is False
        types = node.get("type")
        if types is None and "const" in node:
            types = _json_type(node["const"])
        if types is None and node.get("enum"):
            types = sorted({_json_type(v) for v in node["enum"]})
        if types is None and ("properties" in node or closes or isinstance(extra, dict)):
            types = "object"
        if types is None and "items" in node:
            types = "array"
        properties = node.get("properties", {})
        if isinstance(properties, dict):
            out.banned = frozenset(name for name, decl in properties.items() if decl is False)
        for t in [types] if isinstance(types, str) else (types or ()):
            if t == "object":
                out.fields = {
                    name: [(decl if isinstance(decl, dict) else {}, base)]
                    for name, decl in properties.items()
                    if isinstance(decl, dict) or decl is True
                }
                out.closed = closes
                out.strict = extra is False
                out.map = isinstance(extra, dict)
            elif t == "array":
                self._own_array(out, node, base, stack, pos)
            elif t != "null":
                out.scalars.append(t)
        return out

    def _string_reading(self, node: dict, pos: str) -> tuple | None:
        """A node's `pattern` declaring a pointer (ADR 0007 decision 1.6), or its `format: iri-reference` (1.7)."""
        pattern = node.get("pattern")
        if isinstance(pattern, str) and pattern.startswith("^#/"):
            m = _POINTER_PATTERN.match(pattern)
            if not m:
                raise self.stop(
                    pos,
                    f"pattern {pattern!r} begins ^#/ but is not ^#/<section>/ or ^#/(<section>|<section>...)/",
                    fix="the schema must declare the pointer's sections that way (ADR 0007 decision 1.6)",
                )
            targets = tuple(m.group(1).split("|") if m.group(1) else m.group(2).split("|"))
            unknown = [t for t in targets if t not in self.section_names]
            if unknown:
                raise self.stop(
                    pos,
                    f"pattern {pattern!r} names {unknown}, not collections of the bundle schema "
                    f"({', '.join(self.section_names)})",
                )
            return ("pointer", targets)
        if node.get("format") == "iri-reference":
            return ("iri",)
        return None

    def _own_array(self, out: _Alts, node: dict, base: str, stack: tuple[str, ...], pos: str) -> None:
        items = node.get("items")
        if not isinstance(items, dict):
            out.items = []
            return
        if node.get("minItems") == 2 and node.get("maxItems") == 2:
            inner = self._node(items, base, stack, pos)
            if inner.scalars == ["integer"] and not (inner.fields or inner.items):
                out.range = True
                return
        out.items = [(items, base)]

    # --- a position, as the schema states it: the default policy -------------------------------------------------

    def json_value(self, pos: str, reason: str) -> _Value:
        self.note(pos, "json", reason)
        return _Value("VARCHAR", "json", enc={"form": JSON_TEXT})

    def from_schema(self, a: _Alts, pos: str, top: bool) -> _Value:
        """The default policy (ADR 0007 decision 1): a position the column set leaves to the schema."""
        why = _unsettled(a)
        if why:
            return self.json_value(pos, why)
        if a.range and a.items is None and a.fields is None and set(a.scalars) <= {"integer"}:
            return _Value("BIGINT", "integer", enc={"form": RANGE})
        if a.fields is not None and a.scalars == ["string"] and a.items is None and not a.range:
            return self.split_value(a.string, pos)
        kinds = len(a.scalars) + (a.items is not None) + (a.fields is not None) + a.range
        if kinds > 1:
            return self.json_value(pos, f"a union of {_describe(a)}")
        if a.scalars:
            return self.string_value(a.string, pos) if a.scalars == ["string"] else self.scalar(a.scalars[0])
        if a.items is not None:
            if not a.items:
                element = self.json_value(f"{pos}[*]", "an array with no `items`")
            else:
                element = self.value(a.items, None, f"{pos}[*]")
            return self.list_of(element, pos)
        if not a.closed:
            return self.json_value(pos, "an open object")
        if not a.fields:
            return self.json_value(pos, "an object that allows no property")
        reenters = a.refs & self.path
        if reenters:
            return self.json_value(pos, f"class {sorted(reenters)[0].removesuffix('#')} reaches itself")
        names = list(a.fields)
        return self.struct(a, {n: None for n in names}, names, pos)

    def string_value(self, reading: tuple | None, pos: str) -> _Value:
        """A string: as written, a pointer (one target: the bare id; several: with its section), or an IRI."""
        if reading is None:
            return self.scalar("string")
        if reading[0] == "pointer":
            targets = list(reading[1])
            if not targets:
                raise self.stop(pos, "its pointer patterns name no section in common")
            if len(targets) == 1:
                return _Value("VARCHAR", "string", pointers={"": targets})
            return _Value("VARCHAR", "string", enc={"form": "pointer", "targets": targets})
        self.note(pos, IRI, "format iri-reference, no pointer pattern")
        return _Value("VARCHAR", "string", enc={"form": IRI, "targets": list(self.section_names)})

    def split_value(self, reading: tuple | None, pos: str) -> _Value:
        """A string or an object (ADR 0007 decision 1.8): the string by its reading, the object as JSON text."""
        enc: dict = {"form": "string-or-object", "string": "plain"}
        if reading is not None and reading[0] == "pointer":
            if not reading[1]:
                raise self.stop(pos, "its pointer patterns name no section in common")
            enc.update(string="pointer", targets=list(reading[1]))
        elif reading is not None:
            self.note(pos, IRI, "format iri-reference, no pointer pattern")
            enc.update(string=IRI, targets=list(self.section_names))
        self.note(pos, "json", "a string or an object: the object is kept as JSON text")
        return _Value("VARCHAR", "string", enc=enc)

    def scalar(self, json_type: str) -> _Value:
        return _Value(_DUCK[json_type], json_type)

    def list_of(self, v: _Value, pos: str) -> _Value:
        """A list of v. An element with derived parts is one STRUCT of its parts (ADR 0007 decision 1.10)."""
        pointers, parts = _shift("[*]", v.pointers), _shift("[*]", v.parts)
        element = v.type
        if v.enc is not None:
            parts["[*]"] = v.enc
            named = encoding_parts(v.enc)
            if len(named) > 1:
                element = f"STRUCT({', '.join(f'{_q(p)} {t}' for p, t in named)})"
        return _Value(f"{element}[]", f"{v.slot}_array", pointers, parts, v.extension)

    def struct(self, a: _Alts, shape: dict, names: list[str], pos: str) -> _Value:
        """The object at `pos` with the properties `names`, each by its shape."""
        reenters = a.refs & self.path
        if reenters:
            raise self.stop(pos, f"class {sorted(reenters)[0]} reaches itself", "narrow around it")
        outer, self.path = self.path, self.path | a.refs
        fields, pointers, parts, extension = [], {}, {}, False
        laid: dict[str, str] = {}
        for name in names:
            v = self.field(name, (a.fields or {}).get(name, []), shape[name], f"{pos}.{name}")
            for fname, ftype in self.object_layout(name, v, names, laid, pos):
                fields.append(f"{_q(fname)} {ftype}")
            extension = extension or v.extension
            pointers.update(_shift(f".{name}", v.pointers))
            parts.update(_shift(f".{name}", v.parts))
            if v.enc is not None:
                parts[f".{name}"] = v.enc
        self.path = outer
        return _Value(f"STRUCT({', '.join(fields)})", "object", pointers, parts, extension)

    def object_layout(
        self, name: str, v: _Value, names: list[str], laid: dict[str, str], pos: str
    ) -> list[tuple[str, str]]:
        """The fields (or columns) a property takes inside an object: itself, and its derived parts beside it. A
        derived name equal to a property name at the same level stops the compile (ADR 0007 decision 1.13)."""
        out = [(n, t) for n, _, t in object_fields(name, v.enc)] if v.enc is not None else [(name, v.type)]
        for n, _ in out:
            if n != name and n in names:
                raise self.stop(
                    pos,
                    f"the derived name {n!r} of property {name!r} is also the name of a property here",
                    fix="rename one in the schema (ADR 0007 decision 1.13)",
                )
            if n in laid:
                raise self.stop(pos, f"the name {n!r} is taken twice: by {laid[n]!r} and by {name!r}")
            laid[n] = name
        return out

    def field(self, name: str, decls: list[_Decl], shape: object, pos: str, top: bool = False) -> _Value:
        if name == EXTENSIONS and shape != JSON_TEXT:
            return self.extensions(decls, shape, pos, top)
        return self.value(decls, shape, pos, top)

    # --- a position, with its column-set shape -------------------------------------------------------------------

    def value(self, decls: list[_Decl], shape: object, pos: str, top: bool = False) -> _Value:
        return self.from_shape(self.read(decls, pos), shape, pos, top)

    def from_shape(self, a: _Alts, shape: object, pos: str, top: bool = False) -> _Value:
        if shape is None:
            return self.from_schema(a, pos, top)
        if shape == JSON_TEXT:
            return _Value("VARCHAR", "json", enc={"form": JSON_TEXT})
        known = a.says_something()
        must_unwrap = a.items is not None and a.fields is None and not a.scalars and not a.range
        if isinstance(shape, list):
            if len(shape) != 1:
                raise self.bad_entry(pos, f"a list shape holds one element shape, not {shape!r}")
            if known and a.items is None:
                raise self.bad_entry(
                    pos, f"contradicts the schema: it says a list, the schema allows {_describe(a)}"
                )
            return self.list_of(self.value(a.items or [], shape[0], pos), pos)
        if must_unwrap and a.items:  # the schema says the list; the shape says what each element is
            return self.list_of(self.value(a.items, shape, pos), pos)
        if isinstance(shape, str):
            return self.from_word(a, shape, pos, known)
        if not isinstance(shape, dict) or not shape:
            raise self.bad_entry(pos, f"unknown shape {shape!r}")
        if a.map:
            raise self.bad_entry(
                pos, f"the schema makes this a map whose keys are data: state it as `{JSON_TEXT}`"
            )
        if known and a.fields is None:
            raise self.bad_entry(
                pos, f"contradicts the schema: it says an object, the schema allows {_describe(a)}"
            )
        return self.struct(a, shape, self.emitted(a, shape, pos), pos)

    def from_word(self, a: _Alts, shape: str, pos: str, known: bool) -> _Value:
        if shape == RANGE:
            if known and not (a.range or "integer" in a.scalars):
                raise self.bad_entry(
                    pos, f"contradicts the schema: it says a Range, the schema allows {_describe(a)}"
                )
            return _Value("BIGINT", "integer", enc={"form": RANGE})
        split = _SPLIT_SHAPE.fullmatch(shape)
        word = split.group(1) if split else shape
        if split and known and a.fields is None:
            raise self.bad_entry(
                pos, f"contradicts the schema: it says an object, the schema allows {_describe(a)}"
            )
        m = _POINTER_SHAPE.fullmatch(word)
        if m or word == IRI or (split and word == "string"):
            if known and "string" not in a.scalars:
                what = "a pointer" if m else "an IRI" if word == IRI else "a string"
                raise self.bad_entry(
                    pos, f"contradicts the schema: it says {what}, the schema allows {_describe(a)}"
                )
            if m:
                targets = m.group(1).split("|")
                unknown = [t for t in targets if t not in self.section_names]
                if unknown:
                    raise self.bad_entry(pos, f"a pointer names {unknown}, not collections of the bundle schema")
                reading = ("pointer", tuple(targets))
            else:
                reading = ("iri",) if word == IRI else None
            return self.split_value(reading, pos) if split else self.string_value(reading, pos)
        if shape not in _DUCK:
            raise self.bad_entry(
                pos,
                f"unknown shape {shape!r} (string, integer, number, boolean, json, iri, range, ->section, "
                "<string|iri|->section>+json)",
            )
        if known and shape not in a.scalars and not (shape == "integer" and ("number" in a.scalars or a.range)):
            raise self.bad_entry(pos, f"contradicts the schema: it says {shape}, the schema allows {_describe(a)}")
        return self.scalar(shape)

    def emitted(self, a: _Alts, shape: dict, pos: str) -> list[str]:
        """The properties a mapping shape emits, in table order; refuses one the schema doesn't have."""
        fields = a.fields or {}
        banned = [k for k in shape if k in a.banned]
        if banned:
            raise self.bad_entry(pos, f"names {banned}, which the schema declares false here")
        unknown = [k for k in shape if k not in fields] if a.closed else []
        if unknown:
            raise self.bad_entry(
                pos, f"names {unknown}, which the schema doesn't have here (it has: {', '.join(fields)})"
            )
        return self.order(fields, shape)

    def order(self, fields: dict, shape: dict) -> list[str]:
        """Schema order, then the open positions in column-set order."""
        return [k for k in fields if k in shape] + [k for k in shape if k not in fields]

    # --- extensions ----------------------------------------------------------------------------------------------

    def extension_element(self, a: _Alts) -> _Alts | None:
        """The element of an `extensions` position, when it has the shape rule 1.11 reads: a list of closed objects
        with `name` (a string) and `value`, whose class is not open on the way here. None: other rules apply."""
        if _unsettled(a) or a.scalars or a.range or a.fields is not None or not a.items:
            return None
        element = self.read(a.items, EXTENSIONS)
        if _unsettled(element) or element.scalars or element.items is not None or element.range:
            return None
        fields = element.fields or {}
        if not element.closed or "name" not in fields or "value" not in fields or element.refs & self.path:
            return None
        if self.read(fields["name"], "name").scalars != ["string"]:
            return None
        return element

    def extensions(self, decls: list[_Decl], shape: object, pos: str, top: bool) -> _Value:
        a = self.read(decls, pos)
        value_pos = f"{pos}[*].value"
        if shape is None:
            element = self.extension_element(a)
            if element is None:
                return self.from_schema(a, pos, top)
            values = self.read(element.fields["value"], value_pos)
            outer, self.path = self.path, self.path | element.refs
            try:
                slots = self.default_slots(values, value_pos)
                others = [n for n in element.fields if n not in ("name", "value")]
                return self.extension_struct(element, others, slots, pos)
            finally:
                self.path = outer
        if a.says_something() and a.items is None:
            raise self.stop(
                pos, f"an {EXTENSIONS} property is {_describe(a)}, not a list", f"keep it as `{JSON_TEXT}`"
            )
        if not (isinstance(shape, list) and shape):
            raise self.bad_entry(pos, f"an {EXTENSIONS} shape is the list of its value shapes, not {shape!r}")
        element = self.read(a.items or [], pos)
        values = self.read((element.fields or {}).get("value", []), value_pos)
        if element.says_something():
            if element.fields is None or (element.closed and "value" not in element.fields):
                raise self.bad_entry(pos, f"has value shapes, but the schema's elements have no value: {shape!r}")
        if JSON_TEXT in shape and len(shape) > 1:
            raise self.bad_entry(pos, f"`{JSON_TEXT}` takes every extension value; it can't share with {shape!r}")
        slots = []
        for s in shape:
            v = self.from_shape(values, s, value_pos)
            slot = JSON_TEXT if s == JSON_TEXT else v.slot
            if v.enc is not None and slot != JSON_TEXT:
                raise self.bad_entry(
                    value_pos, f"a value shape {s!r} has parts, which an extension slot can't hold"
                )
            slots.append((slot, v))
        return self.extension_struct(element, [], slots, pos)

    def default_slots(self, values: _Alts, pos: str) -> list[tuple[str, _Value]]:
        """The slots of an extension value the column set leaves to the schema: one per JSON kind the value can
        take, or one JSON-text slot when the value is open, or any of its kinds would need parts beside it."""

        def as_json(reason: str) -> list[tuple[str, _Value]]:
            return [(JSON_TEXT, self.json_value(pos, reason))]

        why = _unsettled(values)
        if why:
            return as_json(why)
        if values.range:
            return as_json("the value may be a Range, which has no slot")
        if values.fields is not None and not values.closed:
            return as_json("the value may be an open object")
        slots = []
        for alt in values.split():
            if alt.items is None:
                kind = alt.scalars[0] if alt.scalars else "object"
                v = self.from_schema(alt, pos, False)
                if v.enc is not None:
                    return as_json(f"its {kind} value has parts, which an extension slot can't hold")
                slots.append((kind, v))
                continue
            elements = self.read(alt.items, f"{pos}[*]") if alt.items else _Alts()
            why = _unsettled(elements)
            if why or elements.range or elements.items is not None:
                return as_json(f"its array value's elements: {why or 'arrays or Ranges'}")
            for element in elements.split():
                kind = element.scalars[0] if element.scalars else "object"
                v = self.from_schema(element, f"{pos}[*]", False)
                if v.enc is not None:
                    return as_json(f"its array value's {kind} elements have parts")
                slots.append((f"{kind}_array", self.list_of(v, pos)))
        return slots

    def extension_struct(
        self, element: _Alts, others: list[str], slots: list[tuple[str, _Value]], pos: str
    ) -> _Value:
        """`STRUCT(name, <the element's other fields>, value_<kind>...)[]` (ADR 0007 decision 1.11)."""
        by_slot: dict[str, _Value] = {}
        for slot, v in slots:
            if slot in by_slot:
                raise self.stop(f"{pos}[*].value", f"two value shapes fill the slot value_{slot}")
            by_slot[slot] = v
        names = ["name", *others]
        fields, pointers, parts = ['"name" VARCHAR'], {}, {"[*]": {"form": EXTENSION}}
        laid: dict[str, str] = {"name": "name"}
        for name in others:
            v = self.field(name, element.fields.get(name, []), None, f"{pos}[*].{name}")
            for fname, ftype in self.object_layout(name, v, names, laid, f"{pos}[*]"):
                fields.append(f"{_q(fname)} {ftype}")
            pointers.update(_shift(f"[*].{name}", v.pointers))
            parts.update(_shift(f"[*].{name}", v.parts))
            if v.enc is not None:
                parts[f"[*].{name}"] = v.enc
        for slot in sorted(by_slot, key=_SLOT_ORDER.index):
            v, fname = by_slot[slot], f"value_{slot}"
            if fname in laid:
                raise self.stop(
                    f"{pos}[*]",
                    f"the derived name {fname!r} of the extension value is also the name of a field here",
                    fix="rename one in the schema (ADR 0007 decision 1.13)",
                )
            laid[fname] = "value"
            fields.append(f"{_q(fname)} {v.type}")
            for targets in v.pointers.values():
                if len(targets) > 1:
                    raise self.stop(f"{pos}[*].value", "a pointer with several targets in an extension value")
            pointers.update(_shift(f"[*].{fname}", v.pointers))
            parts.update(_shift(f"[*].{fname}", v.parts))
            if slot == JSON_TEXT:
                parts[f"[*].{fname}"] = {"form": JSON_TEXT}
        return _Value(f"STRUCT({', '.join(fields)})[]", "object_array", pointers, parts, True)

    # --- a table -------------------------------------------------------------------------------------------------

    def record_alts(self, name: str) -> _Alts:
        """The section's record schema: its class, narrowed by any bundle-level `allOf` that names that section."""
        pos = "(the record)"
        bundle, bundle_id = self.schema_set.bundle, self.schema_set.bundle_id
        record = self.read([_record_decl(self.schema_set, bundle["properties"][name], bundle_id)], pos)
        for i, member in enumerate(bundle.get("allOf", ())):
            narrowing = member.get("patternProperties")
            if not isinstance(narrowing, dict) or set(member) - {"description", "patternProperties"}:
                raise CompileError(
                    f"schema {bundle_id}: allOf[{i}]: the rules read only patternProperties, from a "
                    "collection-name pattern to {additionalProperties: <record schema>}"
                )
            for regex, inner in narrowing.items():
                try:
                    applies = re.search(regex, name)
                except re.error as e:
                    raise CompileError(f"schema {bundle_id}: allOf[{i}]: bad pattern {regex!r}: {e}") from e
                if applies:
                    narrow = inner.get("additionalProperties")
                    if set(inner) != {"additionalProperties"} or not isinstance(narrow, dict):
                        raise CompileError(
                            f"schema {bundle_id}: allOf[{i}].{regex}: the rules read only "
                            "{additionalProperties: <record schema>} here"
                        )
                    record = _refine(record, self.read([(narrow, bundle_id)], pos))
        if record.fields is None:
            record = replace(record, fields={}, closed=False)  # a collection the column set declares, classless
        return record

    def record_shape(self, name: str, record: _Alts) -> tuple[dict, dict[str, str]]:
        """(the section's entry as {property: shape}, {property: column name} for the renamed ones)."""
        pos = "(the record)"
        entry = self.column_set["sections"].get(name)
        if entry is None:
            raise self.stop(pos, "the collection has no entry in the column set", "list its properties")
        if not isinstance(entry, dict):
            raise self.bad_entry(pos, f"a section's entry is a mapping of properties, not {entry!r}")
        shape, renames = {}, {}
        for key, value in entry.items():
            m = _RENAME.fullmatch(key)
            prop = m.group(1) if m else key
            if prop in shape:
                raise self.bad_entry(pos, f"lists {prop!r} twice")
            shape[prop] = value
            if m:
                if prop == ID:
                    raise self.bad_entry(pos, f"the record's {ID} is the key column; it can't be renamed")
                renames[prop] = m.group(2)
        return shape, renames

    def table(self, name: str) -> dict:
        self.section, self.path = name, frozenset()
        record = self.record_alts(name)
        shape, renames = self.record_shape(name, record)
        names = self.emitted(record, shape, "(the record)")
        if not record.closed:
            self.note(
                "(the record)", "open", "the record class is open: a property it doesn't declare stops the build"
            )
        self.path = frozenset(record.refs)
        columns = [_column("id", "VARCHAR", "never", _derive("key", None, None), "The bundle key.")]
        record_id = False
        if ID in shape:
            self.check_id(record.fields.get(ID, []), shape[ID])
            record_id = ID in record.required
            if not record_id:
                where = "TRUE when the record carries its own id (which equals the key), FALSE when it has none."
                columns.append(_column(ID_PRESENT, "BOOLEAN", "never", _derive("id-present", "$.id", None), where))
        slots: dict[str, str] = {}
        laid: dict[str, str] = {"id": ID, **({ID_PRESENT: ID} if not record_id and ID in shape else {})}
        fks: list[dict] = []
        props = [p for p in names if p != ID]
        column_names = {renames.get(p, p) for p in props}
        for prop in props:
            column_name = renames.get(prop, prop)
            for c in self.columns(prop, record.fields.get(prop, []), shape[prop], slots, column_name, fks):
                derived_clash = c["name"] != column_name and c["name"] in column_names
                if derived_clash or c["name"] in laid:
                    other = c["name"] if derived_clash else laid[c["name"]]
                    raise self.stop(
                        "(the record)",
                        f"the column name {c['name']!r} of property {prop!r} is also the name of {other!r}",
                        fix="rename one in the schema, or in a column set (ADR 0007 decision 1.13)",
                    )
                laid[c["name"]] = prop
                columns.append(c)
        described = self.schema_set.bundle["properties"][name].get("description")
        table = {
            "name": name,
            "sections": [name],
            "row_filter": None,
            "sort_key": "id",
            "known_fields": True,
            "description": _clean(described) or f"Section {name}.",
            "columns": columns,
        }
        if slots:
            table["extension_slots"] = slots
        table["json"] = {"record_id": record_id}
        table["foreign_keys"] = fks
        return table

    def check_id(self, decls: list[_Decl], shape: object) -> None:
        if shape is not None or "string" not in self.read(decls, ID).scalars:
            raise self.stop(ID, "the record's id must be a string, the bundle key", "list it with no shape")

    def columns(
        self, prop: str, decls: list[_Decl], shape: object, slots: dict[str, str], name: str, fks: list[dict]
    ) -> list[dict]:
        v = self.field(prop, decls, shape, prop, top=True)
        path = f"$.{prop}"
        description = _description(decls) or f"The {prop} property."
        when_absent = "when-absent"
        fks.extend(_foreign_keys(name, v))
        enc = v.enc
        if enc is not None:
            form = enc["form"]
            if form == JSON_TEXT:
                transform = "the value as compact JSON text"
                return [_column(name, "VARCHAR", when_absent, _derive("json-text", path, transform), description)]
            if form == RANGE:
                return self.range_columns(prop, name, path, description)
            if form == "pointer":
                targets = enc["targets"]
                return [
                    _column(
                        name,
                        "VARCHAR",
                        when_absent,
                        _derive("bare-id", path, _STRIP, targets=targets),
                        description,
                    ),
                    _column(
                        f"{name}_section",
                        "VARCHAR",
                        when_absent,
                        _derive("pointer-section", path, "the <section> of the pointer", targets=targets),
                        f"The section {prop} names.",
                    ),
                ]
            out = []
            for cname, part, ctype in object_fields(name, enc):
                said = description if cname == name else _PART_WORDS[part].format(prop=prop)
                d = _derive("part", path, _PART_TRANSFORMS[part], part=part, encoding=enc)
                out.append(_column(cname, ctype, when_absent, d, said))
            return out
        bare = v.pointers.get("") or (v.pointers.get("[*]") if v.type == "VARCHAR[]" else None)
        if bare:
            many = "[*]" in v.pointers
            strip = f"{_STRIP} from each pointer, in order" if many else _STRIP
            return [_column(name, v.type, when_absent, _derive("bare-id", path, strip, targets=bare), description)]
        if v.extension:
            slots[name] = "json-type"
        if v.type in _DUCK.values():
            return [_column(name, v.type, when_absent, _derive("scalar-path", path, None), description)]
        kind = "extension-slot" if prop == EXTENSIONS and v.extension else "typed-copy"
        transform = (
            _SLOT_TRANSFORM if kind == "extension-slot" else _NESTED_SLOT_TRANSFORM if v.extension else None
        )
        if v.pointers and kind != "extension-slot":
            transform = "pointers stripped"
        d = _derive(kind, path, transform, pointers=v.pointers, parts=v.parts)
        return [_column(name, v.type, when_absent, d, description)]

    def range_columns(self, prop: str, base: str, path: str, description: str) -> list[dict]:
        parts = (
            (
                base,
                "range-exact",
                f"the integer {prop}; NULL when {prop} is a Range ([min, max] list)",
                description,
            ),
            (
                f"{base}_min",
                "range-min",
                f"COALESCE(integer {prop}, {prop}[0])",
                f"Lower bound of {prop}: the integer, or the Range's first bound (NULL when open).",
            ),
            (
                f"{base}_max",
                "range-max",
                f"COALESCE(integer {prop}, {prop}[1])",
                f"Upper bound of {prop}: the integer, or the Range's second bound (NULL when open).",
            ),
        )
        return [_column(n, "BIGINT", "when-absent", _derive(k, path, t), d) for n, k, t, d in parts]

    # --- the spec ------------------------------------------------------------------------------------------------

    def check_column_set(self) -> None:
        doc, schema_id = self.column_set, self.schema_set.bundle_id
        where = f"column set for {schema_id}"
        if doc.get("schema") != schema_id:
            raise CompileError(
                f"{where}: it says it pairs with schema {doc.get('schema')!r}, but the schema's $id is "
                f"{schema_id!r}"
            )
        stray = [s for s in doc.get("sections") or {} if s not in self.section_names]
        if stray:
            raise CompileError(
                f"{where}: sections names {stray}, not collections of the schema ({', '.join(self.section_names)})"
            )
        if ROOT_VALUES_TABLE in self.section_names:
            raise CompileError(f"{where}: a collection can't be named {ROOT_VALUES_TABLE}, the root values' table")

    def spec(self) -> dict:
        schema_id = self.schema_set.bundle_id
        if not self.section_names:
            raise CompileError(f"schema {schema_id}: it has no collections")
        self.check_column_set()
        tables = []
        for name in self.section_names:
            try:
                tables.append(self.table(name))
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                # A schema or column set of the wrong JSON shape (a list where a mapping belongs, say).
                raise CompileError(f"schema {schema_id}: section {name}: malformed: {e!r}") from e
        const = self.schema_set.bundle["properties"].get(self.version_property, {}).get("const")
        return {
            "name": SPEC_NAME,
            "spec_version": RULES_VERSION,
            "status": "released",
            "bundle_schema_versions": [const] if isinstance(const, str) else [],
            "root": {"version_property": self.version_property, "other_values": self.parts.other_values},
            "lossless": True,
            "sources": [
                f"Compiled by the rules {RULES_VERSION} from bundle schema {schema_id} (schema set sha256 "
                f"{self.schema_set.sha256}) and its column set",
            ],
            "writer": dict(WRITER),
            "tables": tables,
        }

    def default_sections(self) -> dict:
        """Every collection with every property its record schema allows, each left to the schema."""
        sections = {}
        for name in self.section_names:
            self.section, self.path = name, frozenset()
            sections[name] = {p: None for p in self.record_alts(name).fields}
        return sections


_PART_WORDS = {
    "id": "The bare id of {prop}, or the string as written when it is no #/<collection>/ pointer.",
    "string": "{prop} when it is a string.",
    "section": "The collection {prop} points into (NULL: the string is kept as written).",
    "json": "{prop} as compact JSON text, when it is an object.",
}
_PART_TRANSFORMS = {
    "id": "a #/<collection>/<rest> string -> <rest>; any other string as written",
    "string": "the string, when the value is one",
    "section": "a #/<collection>/<rest> string -> <collection>; NULL otherwise",
    "json": "an object -> compact JSON text",
}


def _foreign_keys(column: str, v: _Value) -> list[dict]:
    """The declared pointer positions of a column (ADR 0007 decision 2.3): the position of the bare id, its target
    sections, and the position of its section, if any. A generic IRI is not one."""
    out = []
    for p, targets in v.pointers.items():
        out.append({"position": f"{column}{p}", "targets": list(targets), "section": None})
    encs = dict(v.parts)
    if v.enc is not None:
        encs[""] = v.enc
    for p, enc in encs.items():
        string = enc.get("string") if enc["form"] == "string-or-object" else enc["form"]
        if string != "pointer":
            continue
        fields = encoding_parts(enc)
        listed = p.endswith("[*]") and len(fields) > 1
        main = f"{column}{p}.id" if listed else f"{column}{p}"
        section = None
        if any(part == "section" for part, _ in fields):
            section = f"{column}{p}.section" if listed else f"{column}{p}_section"
        out.append({"position": main, "targets": list(enc["targets"]), "section": section})
    return sorted(out, key=lambda f: f["position"])


def _record_decl(schema_set: SchemaSet, node: dict, base: str) -> _Decl:
    """The record schema of a collection's root property: its `patternProperties` (a union of them), or the schema
    in its `additionalProperties`, following a `$ref` to the collection's definition. {} when it states none."""
    if "$ref" in node and "patternProperties" not in node:
        target, file_id = schema_set.resolve(node["$ref"], base)
        return _record_decl(schema_set, target, file_id)
    patterns = node.get("patternProperties")
    if isinstance(patterns, dict) and patterns:
        values = list(patterns.values())
        return (values[0] if len(values) == 1 else {"anyOf": values}), base
    extra = node.get("additionalProperties")
    return (extra if isinstance(extra, dict) else {}), base


def _root_kind(schema_set: SchemaSet, node: dict, base: str, seen: tuple[str, ...] = ()) -> str:
    """Whether a root property is a `collection` or a `value`; `unknown` when the schema can't say."""
    if "patternProperties" in node or (
        isinstance(node.get("additionalProperties"), dict) and "properties" not in node
    ):
        return "collection"
    ref = node.get("$ref")
    if isinstance(ref, str) and ref not in seen:
        target, file_id = schema_set.resolve(ref, base)
        return _root_kind(schema_set, target, file_id, (*seen, ref))
    if node.get("properties") or (node.get("type") not in ("object", None)) or "items" in node or "const" in node:
        return "value"
    return "unknown"


def root_parts(schema_set: SchemaSet, version_property: str | None = None, declared: object = ()) -> RootParts:
    """The root of a bundle, by its schema and the column set's `collections` (`declared`). A property the schema
    can't place and the column set doesn't declare is a value, kept as JSON text. Entries with no use stop it."""
    schema_id = schema_set.bundle_id
    bundle = schema_set.bundle
    properties = bundle.get("properties", {})
    if version_property is not None and version_property not in properties:
        raise CompileError(
            f"schema {schema_id}: version_property {version_property!r} is not a root property of it"
        )
    if not isinstance(declared, (list, tuple)):
        raise CompileError(f"column set for {schema_id}: collections must be a list of root properties")
    for name in declared:
        if name not in properties:
            raise CompileError(f"column set for {schema_id}: collections names {name!r}, not a root property")
    collections, values = [], False
    if version_property in declared:
        raise CompileError(f"column set for {schema_id}: {version_property!r} is the version, not a collection")
    for name, node in properties.items():
        if name == version_property:
            continue
        kind = _root_kind(schema_set, node, schema_id) if isinstance(node, dict) else "unknown"
        if name in declared and kind != "unknown":
            word = "already says it is a collection" if kind == "collection" else "says it is not a collection"
            raise CompileError(f"column set for {schema_id}: collections names {name!r}, but the schema {word}")
        if kind == "collection" or name in declared:
            collections.append(name)
        else:
            values = True
    return RootParts(tuple(collections), values or bundle.get("additionalProperties") is not False)


def _description(decls: list[_Decl]) -> str:
    """The schema's words for a position: the first declaration that has any, the narrowest first."""
    for decl in decls:
        if isinstance(decl, _Both):
            found = _description(list(decl.later)) or _description(list(decl.earlier))
        else:
            found = _clean(decl[0].get("description"))
        if found:
            return found
    return ""


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "null"


def _describe(a: _Alts) -> str:
    kinds = list(a.scalars)
    if a.range:
        kinds.append("a Range")
    if a.items is not None:
        kinds.append("an array")
    if a.fields is not None:
        kinds.append("an object")
    return ", ".join(kinds) or "nothing stated"


def _clean(text: object) -> str:
    return " ".join(str(text).split()) if text else ""


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _derive(
    kind: str,
    path: str | None,
    transform: str | None,
    *,
    targets=None,
    pointers=None,
    parts=None,
    part=None,
    encoding=None,
) -> dict:
    d: dict = {"kind": kind, "path": path, "transform": transform}
    if targets:
        d["targets"] = list(targets)
    if pointers:
        d["pointers"] = {p: list(ts) for p, ts in pointers.items()}
    if kind in ("typed-copy", "extension-slot"):
        d["parts"] = {p: dict(e) for p, e in sorted((parts or {}).items())}
    if part is not None:
        d["part"] = part
        d["encoding"] = dict(encoding)
    return d


def _column(name: str, type_: str, nulls: str, derivation: dict, description: str) -> dict:
    return {
        "name": name,
        "type": str(duckdb.sqltype(type_)),  # as DESCRIBE prints it: the database's own quoting
        "nulls": nulls,
        "derivation": derivation,
        "description": description,
    }


def compile_sections(schema_set: SchemaSet, column_set: dict) -> dict:
    """The spec of `schema_set` and its column set, as a mapping (module docstring). Raises CompileError naming the
    section and position when the two don't determine a table."""
    return _Compiler(schema_set, column_set).spec()


def default_column_set(schema_set: SchemaSet, version_property: str | None = None) -> dict:
    """The default column set of a bundle schema (ADR 0007): every collection, every property its record schema
    allows, each left to the schema. `version_property` is the root property holding the version, if any; it is
    given, never inferred (decision 2.4)."""
    doc: dict = {"schema": schema_set.bundle_id}
    if version_property is not None:
        doc["version_property"] = version_property
    doc["sections"] = _Compiler(schema_set, doc | {"sections": {}}).default_sections()
    return doc


@dataclass(frozen=True)
class Generated:
    """A schema's default: its column set (as a mapping and as the text `generate` writes), its compiled spec, and
    what the policy settled there (ADR 0007 decision 2.1)."""

    doc: dict
    text: str
    raw: dict
    notes: tuple[Note, ...]


def generate_default(schema_set: SchemaSet, version_property: str | None = None) -> Generated:
    doc = default_column_set(schema_set, version_property)
    compiler = _Compiler(schema_set, doc)
    raw = compiler.spec()
    return Generated(doc, column_set_text(doc, schema_set), raw, tuple(compiler.notes))


def column_set_text(doc: dict, schema_set: SchemaSet) -> str:
    """A default column set as YAML: the same inputs always give the same text."""
    lines = [
        f"# The default column set of bundle schema {doc['schema']}",
        f"# (schema set sha256 {schema_set.sha256}), generated by the default policy {RULES_VERSION} (ADR 0007).",
        "# Every property the schema allows is a column; the schema says how each is read.",
        f"schema: {json.dumps(doc['schema'])}",
    ]
    if doc.get("version_property") is not None:
        lines.append(f"version_property: {json.dumps(doc['version_property'])}")
    lines.append("sections:")
    for section, entry in doc["sections"].items():
        names = [_yaml_name(n) for n in entry]
        head = f"  {_yaml_name(section)}: {{"
        line, out = head, []
        for i, n in enumerate(names):
            piece = n + ("," if i < len(names) - 1 else "")
            if len(line) + len(piece) + 1 > 116 and line != head:
                out.append(line.rstrip())
                line = "      "
            line += piece + " "
        out.append(line.rstrip() + "}")
        lines.extend(out)
    return "\n".join(lines) + "\n"


def _yaml_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_]\w*", name) else json.dumps(name)


def table_spec_sha256(raw: dict) -> str:
    """The sha256 of what a compiled spec makes of the tables: its mapping as canonical JSON, without what doesn't
    affect them (`description`, `transform`, `sources`, `status`, `column_set`)."""
    kept = {k: v for k, v in raw.items() if k not in ("sources", "status", "tables", "column_set")}
    kept["tables"] = [
        {
            **{k: v for k, v in t.items() if k not in ("description", "columns")},
            "columns": [
                {
                    **{k: v for k, v in c.items() if k not in ("description", "derivation")},
                    "derivation": {k: v for k, v in c["derivation"].items() if k != "transform"},
                }
                for c in t["columns"]
            ],
        }
        for t in raw["tables"]
    ]
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_differences(default: dict, other: dict) -> list[str]:
    """What differs between two compiled specs, by table and column, both sides given (`generate --check`)."""
    out = []
    for key in ("root", "bundle_schema_versions", "lossless", "writer", "spec_version"):
        if default.get(key) != other.get(key):
            out.append(f"{key}: default {default.get(key)!r}, column set {other.get(key)!r}")
    dt = {t["name"]: t for t in default["tables"]}
    ot = {t["name"]: t for t in other["tables"]}
    for name in [*dt, *(n for n in ot if n not in dt)]:
        a, b = dt.get(name), ot.get(name)
        if a is None or b is None:
            out.append(f"table {name}: {'only in the column set' if a is None else 'missing from the column set'}")
            continue
        for key in sorted(set(a) | set(b) - {"columns", "description"}):
            if a.get(key) != b.get(key):
                out.append(f"table {name}: {key}: default {a.get(key)!r}, column set {b.get(key)!r}")
        ac = {c["name"]: c for c in a["columns"]}
        bc = {c["name"]: c for c in b["columns"]}
        if [c for c in ac if c in bc] != [c for c in bc if c in ac]:
            out.append(f"table {name}: columns in another order")
        for cname in [*ac, *(n for n in bc if n not in ac)]:
            x, y = ac.get(cname), bc.get(cname)
            if x is None or y is None:
                have = x or y
                side = "default" if y is None else "column set"
                out.append(
                    f"table {name}: column {cname}: only in the {side} "
                    f"({have['type']}, {have['derivation']['kind']})"
                )
                continue
            for field_ in ("type", "nulls"):
                if x[field_] != y[field_]:
                    out.append(
                        f"table {name}: column {cname}: {field_}: default {x[field_]}, column set {y[field_]}"
                    )
            dx = {k: v for k, v in x["derivation"].items() if k != "transform"}
            dy = {k: v for k, v in y["derivation"].items() if k != "transform"}
            if dx != dy:
                out.append(f"table {name}: column {cname}: derivation: default {dx}, column set {dy}")
    return out
