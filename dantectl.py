#!/usr/bin/env python3
"""Entry point so the tool runs as ./dantectl.py from a checkout.

`python3 -m dantectl` does the same thing; this exists so the repository is
runnable without knowing that.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dantectl.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
