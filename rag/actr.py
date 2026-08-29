"""ACT-R base-level activation for retrieval scoring.

B = ln(1 + n) - decay * ln(1 + age_days); normalized to [0, 1] via 1 - exp(-B).
decay = 0.5 (ACT-R default base-level learning constant). Frequency comes
from the recall_useful audit signal; recency from the fact's last touch.
"""

from __future__ import annotations

import math

SECONDS_PER_DAY = 86400.0


def actr_activation(now: float, last_access: float, access_count: int, decay: float = 0.5) -> float:
    """Normalized base-level activation in [0, 1]. Zero accesses -> 0.0."""
    if access_count <= 0:
        return 0.0
    age_days = max(0.0, (now - last_access) / SECONDS_PER_DAY)
    b = math.log1p(access_count) - decay * math.log1p(age_days)
    return max(0.0, min(1.0, 1.0 - math.exp(-b)))
