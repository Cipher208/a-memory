from .base_signal import IImportanceSignal

class TechKeywordSignal(IImportanceSignal):
    """
    Signal based on technical keywords.
    Uses regex compiled from importance.json (provided via context).
    """
    def calculate(self, text: str, context: dict) -> float:
        tech_re = context.get("tech_re")
        if not tech_re:
            return 0.0

        # ImportanceScorer logic: tokens = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9_-]+", text.lower())
        # total_hits = ... signals.tech_keyword = min(1.0, total_hits * 0.25)

        # Following task description: Use compiled regex
        hits = len(tech_re.findall(text.lower()))
        score = min(1.0, hits * 0.25)

        if context.get("is_technical_context"):
            score = min(1.0, score + 0.3)

        return score
