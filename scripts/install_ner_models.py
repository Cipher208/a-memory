#!/usr/bin/env python3
"""Install the optional spaCy NER models — wrapper for source checkouts.

The implementation, and the model URLs, live in `mcp_server/utils/ner_models.py`
so that they ship inside the wheel. This file exists only so a source checkout has
an obvious path; it deliberately holds no copy of the URLs.

    python scripts/install_ner_models.py            # install
    python scripts/install_ner_models.py --check    # report, install nothing

A `pip install a-memory` user does not have this directory — for them the same
code is the console script `a-memory-ner`, which is the reason it lives in the
package rather than here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.utils.ner_models import main

if __name__ == "__main__":
    raise SystemExit(main())
