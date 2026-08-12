# Ariel-Memory Constitution (v1.6.4)

> This document defines the non-negotiable architectural and quality standards for the Ariel-Memory project. It is the "Sacred Text" that guides every refactoring and feature implementation.

## [R1] Technical Code (Zero-Tolerance)
1.  **Strict Typing**: Global `mypy --strict` mode is mandatory. Every function must have explicit parameter and return type annotations.
2.  **Linting Supremacy**: Complete compliance with `ruff`. No new `ignore` rules in `pyproject.toml`. Current ignores must be methodically reduced to zero.
3.  **Test Fortress**: 100% stability. No release without passing all 519+ tests (unit, integration, hypothesis, and chaos).
4.  **Async Mandate**: All I/O operations (Files, DB) must be `async/await`. Blocking calls in the event loop are prohibited (use `asyncio.to_thread`).

## [R2] Architectural Esthetics
1.  **Elegant Simplicity**: Do not over-engineer. Use Primitives over complex abstractions where possible.
2.  **Modular Power**: Logic must be decoupled into independent, domain-specific modules. Monoliths are a sign of architectural decay.
3.  **Dependency Discipline**: Libraries are chosen for their performance and value. Every dependency must be updated and audited periodically. Avoid "bloat" — aim for a lightweight core.
4.  **Continuous Refactoring**: If code can be improved, refactor it immediately. We do not carry tech debt; we burn it.

## [R3] Communication & Documentation
1.  **English Standard**: All code, comments, commit messages, and documentation must be in **English**.
2.  **Lucy-Style Docstrings**: Docstrings must explain **WHY** a component exists and its role in the orchestration, not just "what" it does.
3.  **User/Agent Centricity**: The project must be easy to install and a pleasure to use for other agents. API signatures should be intuitive and strictly typed.

## [R4] Primitives & Cognition
1.  **Intent-Based API**: Interaction with memory must flow through Universal Primitives (`think`, `dream`, `forget`, `evolve`, `project`).
2.  **Security Red-Teaming**: Permanent defensive mindset. All dynamic SQL or path operations must use whitelists and safe resolution helpers (`safe_resolve`).

---
*Forged in the fire of Phase 3 and v1.6.4 Awakening.*
*Orchestrated by Lucy-Prime.*
