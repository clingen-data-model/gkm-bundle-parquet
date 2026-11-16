"""Build Parquet tables from GKM JSON bundles, rebuild the bundle from them, and validate bundles."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("clinvar-gkm-parquet")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
