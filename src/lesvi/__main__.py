"""Entry point for ``python -m lesvi``."""

from __future__ import annotations

import sys

from lesvi.cli import main

if __name__ == "__main__":
    sys.exit(main())
