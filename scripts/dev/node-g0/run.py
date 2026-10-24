"""Run the disposable node G0 fixture from the repository root."""

# ruff: noqa: INP001 -- standalone acceptance entry point

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    runpy.run_module("tests.node_g0.run", run_name="__main__")
