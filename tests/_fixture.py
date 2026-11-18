"""The bundle fixture the suite reads: `tests/fixtures/real-1.1.0/clinvar-gkm_excerpt.json.gz`, a real ClinVar-GKM
1.1.0 bundle excerpt (design plan §8.1; made by scripts/extract-fixtures.py, which 2.0.0 retired with ClinVar-GKM's
specs), and its pairing: ClinVar-GKM's 1.1.0 schema set and column set in tests/fixtures/clinvar-gkm/1.1.0/ (the
tool ships no producer's, ADR 0009).

The environment variable CLINVAR_GKM_FIXTURE, set to the path of a folder holding another excerpt of the same
schema version, runs the suite on that excerpt instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gkm_bundle_parquet.bundle import BundleShape, RootRules
from gkm_bundle_parquet.compiled import compile_pairing
from gkm_bundle_parquet.compiled import reader_rules as _reader_rules
from gkm_bundle_parquet.errors import SchemaVersionError
from gkm_bundle_parquet.spec import Spec, SpecSource

FIXTURES = Path(__file__).parent / "fixtures"
# ClinVar-GKM's 1.1.0 schema set and column set, as the 1.x tool shipped them (tests/fixtures/clinvar-gkm/).
CLINVAR = FIXTURES / "clinvar-gkm" / "1.1.0"
CLINVAR_SCHEMA = CLINVAR / "schema-set"
CLINVAR_COLUMNS = CLINVAR / "columns.yaml"
PAIRING = ["--bundle-schema", str(CLINVAR_SCHEMA), "--column-set", str(CLINVAR_COLUMNS)]
REAL = FIXTURES / "real-1.1.0" / "clinvar-gkm_excerpt.json.gz"
_ENV = "CLINVAR_GKM_FIXTURE"


def _choose() -> Path:
    folder = os.environ.get(_ENV)
    if folder is None:
        return REAL
    path = Path(folder) / REAL.name
    if not path.is_file():
        raise RuntimeError(f"{_ENV}={folder}: no fixture at {path}; it must hold an excerpt named {REAL.name}")
    return path


FIXTURE = _choose()
PROVENANCE_PATH = FIXTURE.parent / "PROVENANCE.json"
PROVENANCE = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
COUNTS: dict[str, int] = PROVENANCE["counts"]
# The release the fixture's records come from, as the extractor recorded it.
SOURCE_RELEASE: str = PROVENANCE["source"]["release"]
SCHEMA_VERSION: str = PROVENANCE["schema_version"]


def clinvar_spec() -> tuple[Spec, SpecSource]:
    """The sections spec compiled from the ClinVar-GKM 1.1.0 pairing."""
    return compile_pairing(str(CLINVAR_SCHEMA), str(CLINVAR_COLUMNS))


def reader_rules() -> RootRules:
    """How the fixture's root is read: by the ClinVar-GKM 1.1.0 pairing's rules."""
    return _reader_rules(clinvar_spec()[0])


SPEC, SPEC_SOURCE = clinvar_spec()
# The sections of the fixture's bundle schema, in schema order.
SECTIONS = tuple(t.sections[0] for t in SPEC.tables)
GOLDEN_SECTIONS_SPEC = FIXTURES / "sections-golden" / "tables.yaml"  # what the compiler must reproduce on 1.1.0
STAGING_NAME = FIXTURE.name.removesuffix(".json.gz")


def strict_rules() -> RootRules:
    """The strict reading 1.x gave the excerpt's producer, for the tests of the line grammar: the line layout
    only, and a `schema_version` line that must name the excerpt's version."""
    shape = BundleShape(SECTIONS)

    def shape_of(version: str) -> BundleShape:
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"bundle schema_version {version!r} is not the fixture's {SCHEMA_VERSION}")
        return shape

    return RootRules("schema_version", shape_of, version_required=True, general_reader=False)
