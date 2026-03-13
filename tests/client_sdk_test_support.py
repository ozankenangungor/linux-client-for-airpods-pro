"""Repository path support for the independently packaged Python client."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON_CLIENT_ROOT = ROOT / "packages/airpods-client-python"
PYTHON_CLIENT_SRC = PYTHON_CLIENT_ROOT / "src"

client_source = str(PYTHON_CLIENT_SRC)
if client_source not in sys.path:
    sys.path.insert(0, client_source)
