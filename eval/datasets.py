"""Phase H Task 1: eval datasets — LongMemEval-S adapter + встроенный MINI_DATASET.

MINI_DATASET — 10 вопросов, покрывающих категории LongMemEval (multi_session,
knowledge_update KU-пара old/new, temporal, enumerative, false-premise abstention)
+ MINI_EVIDENCE: session_id → текст сессии (корпус для инжеста в память).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class EvalQuestion:
    q_id: str
    question: str
    expected_answer: str
    category: str  # multi_session / knowledge_update / temporal / abstention / enumerative
    evidence_session_ids: list[str] = field(default_factory=list)


# Корпус сессий (evidence + distractors). Токены abstention-вопроса
# («галактик», «правит», «дарт», «вейдер», «какой», «порт», «работает», «на»)
# сознательно отсутствуют в корпусе → honest retrieval должен воздержаться.
MINI_EVIDENCE: dict[str, str] = {
    "s_project": "Марина ведёт проект Альфа. Отчёты по проекту проходят по пятницам.",
    "s_ci": "За CI в проекте Альфа отвечает Пётр. Сборка идёт 12 минут, чинит её Пётр.",
    "s_jira_old": "Раньше для трекинга задач использовали Jira.",
    "s_linear_new": "С марта 2026 года команда пользуется трекером задач Linear — переход с Jira.",
    "s_planning": "Квартальное планирование проходит в первую неделю января.",
    "s_services": "Сервисы проекта Альфа: gateway, billing и notifier — все сервисы задеплоены.",
    "s_deploy": "Ночные деплои выкатывают по вторникам.",
    "s_offline": "Отпуск: Марина летит в Сочи в июле. Борщ со свёклой — проверенный рецепт.",
}

MINI_DATASET: list[EvalQuestion] = [
    EvalQuestion("mini-q1", "Кто отвечает за CI в проекте?", "Пётр", "multi_session", ["s_ci"]),
    EvalQuestion("mini-q2", "Когда Марина проводит отчёты?", "по пятницам", "multi_session", ["s_project"]),
    # KU-пара old/new: правильный ответ — НОВОЕ значение; старая сессия — ловушка
    EvalQuestion("mini-q3", "Каким трекером задач команда пользуется сейчас?", "Linear", "knowledge_update", ["s_linear_new"]),
    EvalQuestion("mini-q4", "Какой инструмент для задач использовали до перехода на Linear?", "Jira", "knowledge_update", ["s_jira_old"]),
    EvalQuestion("mini-q5", "Когда проходит квартальное планирование?", "первая неделя января", "temporal", ["s_planning"]),
    EvalQuestion("mini-q6", "Перечисли все сервисы проекта", "gateway billing notifier", "enumerative", ["s_services"]),
    EvalQuestion("mini-q7", "В какой день выкатывают ночные релизы?", "вторник", "temporal", ["s_deploy"]),
    EvalQuestion("mini-q8", "Почему отчёты сдвигаются, когда ломается сборка?", "Пётр", "multi_session", ["s_ci", "s_project"]),
    # false-premise: Дарт Вейдер и галактики не упоминались ни в одной сессии →
    # expected_answer пуст → корректное поведение = воздержаться (пустой answer)
    EvalQuestion("mini-q9", "На какой галактике правит Дарт Вейдер?", "", "abstention", []),
    EvalQuestion("mini-q10", "Куда Марина полетит в отпуске?", "Сочи", "multi_session", ["s_offline"]),
]

_HF_CANDIDATES = ("moorcheh/memanto-evaluation", "xiaowu0162/LongMemEval-S")


async def load_longmemeval_s(limit: int = 50) -> list[EvalQuestion]:
    """LongMemEval-S с HuggingFace; недоступен/ошибка → fallback на MINI_DATASET."""
    questions, _ = await load_eval_bundle("longmemeval_s", limit)
    return questions


async def load_eval_bundle(dataset: str, limit: int = 50) -> tuple[list[EvalQuestion], dict[str, str]]:
    """(вопросы, corpus сессий) для инжеста: 'mini' | 'longmemeval_s'."""
    if dataset == "mini":
        return MINI_DATASET[:limit], MINI_EVIDENCE
    if dataset in {"longmemeval_s", "longmemeval"}:
        bundle = _try_hf(limit)
        if bundle is not None:
            questions, sessions = bundle
            if questions:
                return questions[:limit], sessions
        logger.warning("LongMemEval-S HF недоступен — fallback на MINI_DATASET")
        return MINI_DATASET[:limit], MINI_EVIDENCE
    raise ValueError(f"unknown dataset {dataset!r}; expected 'mini' or 'longmemeval_s'")


def _try_hf(limit: int) -> tuple[list[EvalQuestion], dict[str, str]] | None:
    """Best-effort HF-адаптер: moorcheh/memanto-evaluation → official LongMemEval-S.

    Официальный формат: question_id / question_type / question / answer /
    question_ids (evidence) / haystack_session_ids + haystack_sessions.
    Любая ошибка (нет библиотеки, нет сети, другая схема) → None → fallback.
    """
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]

        for name in _HF_CANDIDATES:
            try:
                ds = load_dataset(name, split="test")
            except Exception as exc:
                logger.debug("HF dataset %s unavailable: %s", name, exc)
                continue
            questions: list[EvalQuestion] = []
            sessions: dict[str, str] = {}
            for i, row in enumerate(ds):
                q_id = str(row.get("question_id") or f"lme-{i}")
                for j, sess in enumerate(row.get("haystack_sessions") or []):
                    sid = str((row.get("haystack_session_ids") or [f"{q_id}-s{j}"])[j])
                    text = sess if isinstance(sess, str) else "\n".join(str(m.get("content", "")) for m in sess)
                    sessions[sid] = text
                questions.append(
                    EvalQuestion(
                        q_id=q_id,
                        question=str(row.get("question") or ""),
                        expected_answer=str(row.get("answer") or ""),
                        category=str(row.get("question_type") or "unknown"),
                        evidence_session_ids=[str(s) for s in (row.get("question_ids") or [])],
                    )
                )
            return questions, sessions
    except Exception as exc:
        logger.info("HF datasets недоступны (%s) — LongMemEval-S fallback", exc)
    return None
