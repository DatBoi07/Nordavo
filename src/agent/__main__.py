# src/agent/__main__.py
"""Allow running as `python -m agent` or directly as `python src/agent/__main__.py`."""
import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.dirname(script_dir)

# Remove script directory from sys.path so 'agent' is imported as a package from 'src'
if sys.path and os.path.abspath(sys.path[0]) == script_dir:
    sys.path.pop(0)

if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from agent.cli import main

if __name__ == "__main__":
    main()
