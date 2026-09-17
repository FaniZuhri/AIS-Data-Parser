#!/usr/bin/env python3
"""Entry point for the AIS/GPS decoder.

Run it from the repo root with the pipenv environment active:

    pipenv install
    pipenv shell
    python main.py tests/fixtures/mixed_stream.txt --format table
    python main.py < capture.log
    python main.py --help

`python main.py --help` documents every option. `python -m aivdm` runs the same
thing through the package, which is useful once the package is pip-installed.
"""

from __future__ import annotations

import sys

from aivdm.cli import main

if __name__ == "__main__":
    sys.exit(main())
