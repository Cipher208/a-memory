from __future__ import annotations

"""Saga watchdog — detects stuck sagas, recovery and cleanup of old states."""

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

from shared.constants import (
    STATUS_STUCK,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_COMPENSATING,
    STATUS_COMPENSATED,
)
from shared.saga.impl import storage

logger = logging.getLogger(__name__)


class SagaWatchdog:
    """Detect stuck sagas and recover from crashes."""

    def __init__(self, check_interval: int = 60, max_age_seconds: int = 600):
        self.check_interval = check_interval
        self.max_age_seconds = max_age_seconds
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Saga watchdog started (interval=%ds)", self.check_interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._check_stuck_sagas()
                time.sleep(self.check_interval)
            except Exception:
                logger.exception("Saga watchdog error")
                time.sleep(30)

    def _check_stuck_sagas(self) -> None:
        """Find and mark stuck sagas."""
        storage.SAGA_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()

        for state_file in storage.SAGA_DIR.glob("*.json"):
            if not state_file.is_file() or state_file.is_symlink():
                continue
            self._process_stuck_candidate(state_file, now)

    def _process_stuck_candidate(self, state_file: Path, now: float) -> None:
        """Read and analyze a single saga state file for timeout."""
        try:
            state = self._read_state_safe(state_file)

            status = state.get("status", "")
            started_at = state.get("started_at", 0)
            saga_name = state.get("name", "unknown")

            if status in (STATUS_RUNNING, STATUS_COMPENSATING):
                age = now - started_at
                if age > self.max_age_seconds:
                    self._mark_as_stuck(state_file, state, saga_name, age)

        except Exception:
            logger.exception("Error checking saga %s", state_file.name)

    def _mark_as_stuck(self, state_file: Path, state: dict[str, Any], saga_name: str, age: float) -> None:
        state["status"] = STATUS_STUCK
        state["stuck_reason"] = f"timeout_after_{int(age)}s"
        storage.write_state_file(state_file, state)
        logger.warning("Saga '%s' marked as STUCK (age=%ds)", saga_name, int(age))

    def get_stuck_sagas(self) -> list[dict[str, Any]]:
        """Get list of stuck sagas."""
        stuck = []
        storage.SAGA_DIR.mkdir(parents=True, exist_ok=True)

        for state_file in storage.SAGA_DIR.glob("*.json"):
            if not state_file.is_file() or state_file.is_symlink():
                continue

            with contextlib.suppress(Exception):
                state = self._read_state_safe(state_file)
                if self._is_stuck_candidate(state):
                    stuck.append(self._format_stuck_info(state))

        return stuck

    def _read_state_safe(self, state_file: Path) -> dict[str, Any]:
        return storage.read_state_file(state_file)

    def _is_stuck_candidate(self, state: dict[str, Any]) -> bool:
        return state.get("status") in (STATUS_STUCK, STATUS_FAILED, STATUS_RUNNING)

    def _format_stuck_info(self, state: dict[str, Any]) -> dict[str, Any]:
        age = time.time() - state.get("started_at", 0)
        return {
            "saga_id": state.get("saga_id"),
            "name": state.get("name"),
            "status": state.get("status"),
            "current_step": state.get("current_step"),
            "age_seconds": int(age),
        }

    def recover_saga(self, saga_id: str) -> dict[str, Any] | None:
        """Attempt to recover a stuck saga."""
        if saga_id.startswith(".") or "/" in saga_id or "\\" in saga_id:
            return {"error": "Invalid saga_id"}

        state_file = storage.SAGA_DIR / (saga_id + ".json")
        if not state_file.exists() or state_file.is_symlink():
            return None

        try:
            state = self._read_state_safe(state_file)

            if state.get("status") != STATUS_STUCK:
                return {"error": "Saga is not stuck, status: {}".format(state.get("status"))}

            state["status"] = "manual_review_required"
            state["recovered_at"] = time.time()
            storage.write_state_file(state_file, state)

            return {"status": "manual_review_required", "state": state}
        except Exception as e:
            return {"error": str(e)}

    def cleanup_completed(self) -> int:
        """Delete completed, compensated, stuck, failed, and manual_review_required sagas older than 1 hour."""
        cutoff = time.time() - 3600
        removed = 0
        with contextlib.suppress(Exception):
            storage.SAGA_DIR.mkdir(parents=True, exist_ok=True)

            for state_file in storage.SAGA_DIR.glob("*.json"):
                if state_file.is_symlink():
                    continue
                with contextlib.suppress(Exception):
                    state = self._read_state_safe(state_file)

                    if (
                        state.get("status") in (STATUS_COMPLETED, STATUS_COMPENSATED, STATUS_STUCK, STATUS_FAILED, "manual_review_required")
                        and state.get("started_at", 0) < cutoff
                    ):
                        state_file.unlink()
                        removed += 1

            return removed
        return 0


# Singleton watchdog
saga_watchdog = SagaWatchdog()
