"""Errors the tool raises. Each one stops the build: the tool never skips or drops input silently."""


class ToolError(Exception):
    """Base class. The CLI prints the message and exits non-zero."""


class InputError(ToolError):
    """An input can't be read: missing file, HTTP failure, checksum mismatch."""


class BundleFormatError(ToolError):
    """A bundle line doesn't match the assembler's layout (design plan §4.2), or a section or key is unknown
    or duplicated. The message names the file, line number, section and key where known."""


class SchemaVersionError(ToolError):
    """A bundle's schema_version is missing, or the tool can't read it: its schema set doesn't load, its sections
    spec doesn't compile, or the spec asked for doesn't list it (design plan §4.4, ADR 0006 §6)."""


class SpecError(ToolError):
    """The table spec is malformed, or a built table or record doesn't match it (an unknown extension name,
    a non-pointer where a pointer is required, a DESCRIBE that differs from the spec)."""


class SchemaSetError(ToolError):
    """A bundle schema set can't be loaded: a missing or unfetchable file, a file whose `$id` differs from the
    ref that reached it, a `$ref` that names nothing, a snapshot that differs from its manifest (ADR 0006)."""


class CompileError(ToolError):
    """The schema set (and its overlay) doesn't determine a table of the `sections` spec: a pattern the rules don't
    cover, or an overlay entry that contradicts the schema or has no use. The message names the section and
    position, and what the schema must state (ADR 0006)."""


class RoundTripError(ToolError):
    """The round trip fails (ADR 0005, amended by ADR 0008): a built row doesn't write back as its bundle record,
    or a rebuild can't reproduce the source bundle's records: the directory is not a full lossless build (a
    selection, a set built from deltas by 1.x, a missing table or ordinal, a spec that is not lossless), a
    pointer's section is unknown, or a rebuilt records sha256 differs from the recorded one."""
