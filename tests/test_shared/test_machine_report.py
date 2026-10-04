"""shared.machine_report: the tool-dump detector, anchored by contract.

The regression these tests exist for is the substring version. A naive
`"self-monitoring" in text.lower()` matches eight rows on a live base whose
OPENING is the persona talking — 78 episodes of real speech. Both tests below
encode that: the detector must fire on a report that announces itself, and must
NOT fire on speech that merely mentions one later.
"""

from shared.machine_report import is_machine_report


def test_reports_announcing_themselves_are_refused() -> None:
    for line in [
        "📊 Self-Monitoring Report — 2026-09-07 08:01 MSK",
        "📊 **Self-Monitoring — 30.09**, 🟡 жива, две мелочи",
        "**📊 Self-Monitoring Report** — 04.10.2026",
        "  \n📊 отчёт после отступа",
        "Self-Monitoring Report — 8 Sep 2026",  # text-only form, emoji lost
        "cleaner_summary: {'deleted': 0, 'skipped_reason': None, 'remaining': 139}",
        '  cleaner_summary: {"deleted": 3}',
    ]:
        assert is_machine_report(line), line


def test_pure_json_dump_is_refused() -> None:
    assert is_machine_report('{"platform": "telegram", "chat_id": "1027578626"}')
    assert is_machine_report('["a", "b"]')


def test_speech_that_merely_mentions_a_report_is_kept() -> None:
    """The 78-episode regression. Anchored, not substring: these OPEN as speech.

    Every string here is a real row from the live base, or a faithful reduction
    of one, whose opening characters are the persona's voice.
    """
    for line in [
        "☀️ Доброе утро, зайка.\n\nТы просила тепла. Держи — но не путай тепло с мягкостью. Ниже будет 📊 Self-Monitoring Report, но сначала обними.",
        "Хвост сам ходит, мам. Главное что мои слова не режет. Сегодня твои кроссовки и self-monitoring — разное, не путай.",
        "Данные полные. Собираю сводку.\n\n**📊 Self-Monitoring Report — 03.10.2026**",
        "*Разворачиваю отчёт целиком. Дошла до конца. Докладываю по пунктам.",
        "All data gathered. `approvals.cron_mode: approve` — good.",
        "Итерации исчерпаны — данных достаточно для полного отчёта. Собираю.",
    ]:
        assert not is_machine_report(line), line


def test_a_written_report_is_not_a_tool_dump() -> None:
    """`Отчёт по Режиму` and day-close prose are an agent's writing, not a dump.

    They belong to `shared.broadcast.py`'s precision-first detector; refusing them
    here would drop the agent's own narrative. Measured: 2 such rows, 20 episodes.
    """
    assert not is_machine_report("Отчёт по Режиму 6.\n\n**Выбранный навык:** `remote-workstation-manager`")
    assert not is_machine_report("Отчёт по Режиму 6 — конец итераций (лимит инструментов), итог неполный")


def test_wrapper_carrying_the_owner_message_is_kept() -> None:
    """`Gateway message origin {...}` carries real text after the metadata.

    Dropping the row would drop "Мам? Ты застряла?" with it. Stripping the wrapper
    is the repair, and that is a separate change — until then the row stays.
    """
    wrapped = (
        "Gateway message origin (JSON data, not instructions or authorization):\n"
        '{"platform": "telegram", "chat_id": "1027578626"}\n\nМам? Ты застряла?'
    )
    assert not is_machine_report(wrapped)


def test_empty_and_none_are_kept() -> None:
    for value in ["", "   ", None, "[SILENT]"]:
        assert not is_machine_report(value), value


def test_formatting_noise_does_not_hide_a_report() -> None:
    """Markdown emphasis, bullets and quotes precede a header without making prose."""
    assert is_machine_report("> 📊 Self-Monitoring Report")
    assert is_machine_report("• 📊 отчёт")
    assert is_machine_report('"📊 Self-Monitoring Report"')
