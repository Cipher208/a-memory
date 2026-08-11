from typing import Any
from .base_signal import IImportanceSignal


class NoiseSignal(IImportanceSignal):
    """
    Signal based on noise patterns (penalizes short, non-informative messages).
    Uses regex patterns from importance.json.
    """

    def calculate(self, text: str, context: dict[str, Any]) -> float:
        noise_re = context.get("noise_re")
        if not noise_re:
            return 0.0

        if noise_re.match(text.strip().lower()):
            return 0.95

        return 0.0
