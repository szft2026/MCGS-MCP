"""Entry point that works from any working directory.

Point the MCP client at this file -- no cwd juggling, no PYTHONPATH, no ports:

    python D:/mcgs-max/run.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcgs_max.server import main  # noqa: E402

if __name__ == "__main__":
    main()
