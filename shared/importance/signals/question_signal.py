from .base_signal import IImportanceSignal

class QuestionSignal(IImportanceSignal):
    """
    Signal based on question marks count.
    """
    def calculate(self, text: str, context: dict) -> float:
        qcount = text.count("?")
        return min(1.0, qcount * 0.5)
