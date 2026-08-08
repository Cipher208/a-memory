from .base_signal import IImportanceSignal

class LengthSignal(IImportanceSignal):
    """
    Signal based on text length.
    Logic: min(1.0, L / 800.0)
    """
    def calculate(self, text: str, context: dict) -> float:
        if not text:
            return 0.0
        L = len(text)
        return min(1.0, L / 800.0)
