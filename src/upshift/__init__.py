"""upshift: make upgrading a production AI agent to a new model version safe."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

#: Stamped into every run record, capture `index.json` and adapt record as the provenance of
#: that evidence, so it must be the version that actually ran. Read from the installed
#: package rather than kept by hand: the literal that used to live here fell three releases
#: behind `pyproject.toml` and quietly labelled real evidence `0.1.0` while `upshift
#: --version` — which already read the metadata — reported the truth.
try:
    __version__ = _installed_version("upshift")
except PackageNotFoundError:  # source tree with no install; matches cli._version()
    __version__ = "0.0.0+src"
