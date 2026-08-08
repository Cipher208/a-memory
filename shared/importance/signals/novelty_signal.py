from .base_signal import IImportanceSignal

class NoveltySignal(IImportanceSignal):
    """
    Signal based on whether the content was seen before.
    """
    def calculate(self, text: str, context: dict) -> float:
        seen_before = context.get("seen_before", False)
        return 0.0 if seen_before else 0.7
