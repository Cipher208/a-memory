from __future__ import annotations

"""
Saga — pattern for multi-step operations with compensation (rollback).
Includes watchdog for detecting stuck sagas and persistence for recovery.
State files are encrypted at rest using envelope encryption.
Supports retry with exponential backoff and idempotent step execution (B7).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

SAGA_DIR = Path.home() / ".mcp-ariel-memory" / "sagas"

try:
    from shared.crypto import is_encrypted_blob as _is_crypto_encrypted_blob
    from features.secrets import decrypt_json, encrypt_json

    def is_encrypted_blob(path: Path) -> bool:
        """Check if file is encrypted (not plain JSON)."""
        if not path.exists():
            return False
        # noqa: SKY-D325
        with path.open("rb") as f:
            head = f.read(1)
        return _is_crypto_encrypted_blob(head)

    _HAS_ENCRYPTION = True
except ImportError:
    _HAS_ENCRYPTION = False


class SagaStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    STUCK = "stuck"


@dataclass
class SagaStep:
    name: str
    action: Callable[[dict], Coroutine[Any, Any, dict]]
    compensation: Callable[[dict], Coroutine[Any, Any, None]] | None = None
    timeout_seconds: int | None = None
    retry_attempts: int = 0
    retry_backoff: float = 0.5
    retry_on: tuple = (ConnectionError, TimeoutError)
    idempotency_key_fn: Callable[[dict], str] | None = None
    status: SagaStatus = SagaStatus.PENDING
    result: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)


class Saga:
    def __init__(self, name: str, timeout_seconds: int = 300, saga_id: str | None = None):
        self.name = name
        self._saga_id = saga_id or f"{name}_{uuid.uuid4().hex[:8]}"
        self.timeout_seconds = timeout_seconds
        self._steps: list[SagaStep] = []
        self._status = SagaStatus.PENDING
        self._data: dict = {}
        self._current_step = 0
        self._started_at: float = 0.0
        self._completed_steps: list[int] = []
        SAGA_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def status(self) -> SagaStatus:
        return self._status

    @property
    def data(self) -> dict:
        return self._data

    @property
    def saga_id(self) -> str:
        return self._saga_id

    def add_step(
        self,
        name: str,
        action: Callable[[dict], Coroutine[Any, Any, dict]],
        compensation: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
        timeout_seconds: int | None = None,
        retry_attempts: int = 0,
        retry_backoff: float = 0.5,
        retry_on: tuple = (ConnectionError, TimeoutError),
        idempotency_key_fn: Callable[[dict], str] | None = None,
    ) -> Saga:
        self._steps.append(
            SagaStep(
                name=name,
                action=action,
                compensation=compensation,
                timeout_seconds=timeout_seconds,
                retry_attempts=retry_attempts,
                retry_backoff=retry_backoff,
                retry_on=retry_on,
                idempotency_key_fn=idempotency_key_fn,
            )
        )
        return self

    def _save_state(self) -> None:
        """Save state to disk (encrypted if available)."""
        state_file = SAGA_DIR / (self._saga_id + ".json")
        state = {
            "name": self.name,
            "saga_id": self._saga_id,
            "status": self._status.value,
            "current_step": self._current_step,
            "started_at": self._started_at,
            "data": self._data,
            "completed_steps": self._completed_steps,
            "steps": [{"name": s.name, "status": s.status.value, "result": s.result} for s in self._steps],
        }
        try:
            SAGA_DIR.mkdir(parents=True, exist_ok=True)
            if _HAS_ENCRYPTION:
                blob = encrypt_json(state)
                # noqa: SKY-D324
                state_file.write_bytes(blob)
            else:
                # noqa: SKY-D324
                state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        except Exception:
            logger.exception("Failed to save saga state")

    def _load_state(self, saga_id: str) -> dict | None:
        """Load state from disk (supports encrypted and legacy plain JSON)."""
        from shared.saga import read_state_legacy_or_encrypted

        state_file = SAGA_DIR / (saga_id + ".json")
        if not state_file.exists():
            return None
        with contextlib.suppress(Exception):
            return read_state_legacy_or_encrypted(state_file)
        return None

    def _cleanup_state(self) -> None:
        """Delete state file after completion."""
        state_file = SAGA_DIR / (self._saga_id + ".json")
        if state_file.exists():
            with contextlib.suppress(Exception):
                state_file.unlink()

    # ─── Idempotency helpers ───

    def _compute_idempotency_key(self, step: SagaStep) -> str | None:
        """Compute SHA-256 hash for idempotent step replay."""
        if not step.idempotency_key_fn:
            return None
        try:
            seed = step.idempotency_key_fn(self._data)
        except Exception:
            return None
        raw = f"{step.name}|{seed}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _is_already_completed(self, key: str) -> bool:
        """Check idempotency log for completed step."""
        from shared.connection import connection_manager

        conn = await connection_manager.get("memory.db")
        try:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM saga_step_log WHERE saga_id=? AND params_hash=? LIMIT 1",
                    (self._saga_id, key),
                )
            ).fetchone()
            return row is not None
        except Exception:
            return False

    async def _get_cached_result(self, key: str) -> dict | None:
        """Get cached result from idempotency log."""
        from shared.connection import connection_manager

        conn = await connection_manager.get("memory.db")
        try:
            row = await (
                await conn.execute(
                    "SELECT result_json FROM saga_step_log WHERE saga_id=? AND params_hash=?",
                    (self._saga_id, key),
                )
            ).fetchone()
            if row is None:
                return None
            result_blob = row["result_json"]
            if isinstance(result_blob, (bytes, bytearray)):
                return decrypt_json(bytes(result_blob))
            return json.loads(result_blob) if result_blob else None
        except Exception:
            return None

    async def _record_completed(self, key: str, step_name: str, result: dict) -> None:
        """Record completed step in idempotency log."""
        from shared.connection import connection_manager

        conn = await connection_manager.get("memory.db")
        try:
            encrypted = encrypt_json(result) if _HAS_ENCRYPTION else json.dumps(result).encode()
            await conn.execute(
                """INSERT INTO saga_step_log (saga_id, step_name, params_hash, result_json, completed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(saga_id, step_name, params_hash) DO NOTHING""",
                (self._saga_id, step_name, key, encrypted, time.time()),
            )
            await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record idempotency log: {e}")

    # ─── Execute with retry + idempotency ───

    async def _check_idempotency(self, step: SagaStep) -> tuple[bool, str | None]:
        """Check if step was already executed via idempotency log.

        Returns (replayed, idempotency_key). If replayed, step state is restored
        from cache and the caller should skip execution.
        """
        idemp_key = self._compute_idempotency_key(step)
        if idemp_key and await self._is_already_completed(idemp_key):
            cached = await self._get_cached_result(idemp_key)
            if cached is not None:
                step.result = cached
                step.status = SagaStatus.COMPLETED
                step.data = self._data.copy()
                self._data.update(step.result)
                self._completed_steps.append(self._current_step)
                self._save_state()
                logger.info(f"Saga '{self.name}' step '{step.name}' replayed from cache")
                return True, idemp_key
        return False, idemp_key

    async def _execute_step_with_retry(self, step: SagaStep) -> None:
        """Execute a single step with exponential backoff retry."""
        attempt = 0
        step_exc: Exception | None = None
        while attempt <= step.retry_attempts:
            try:
                step.result = await self._run_step_action(step)
                return
            except step.retry_on as exc:
                step_exc = exc
                attempt += 1
                if attempt <= step.retry_attempts:
                    await self._handle_retry_pause(step, attempt, exc)
            except Exception as exc:
                step_exc = exc
                break

        await self._handle_step_failure(step, attempt, step_exc)

    async def _run_step_action(self, step: SagaStep) -> dict:
        step_timeout = step.timeout_seconds or self.timeout_seconds
        if isinstance(step.action, Saga):
            result = await asyncio.wait_for(step.action.execute(self._data), timeout=step_timeout)
        else:
            action_result = step.action(self._data)
            if asyncio.iscoroutine(action_result):
                result = await asyncio.wait_for(action_result, timeout=step_timeout)
            else:
                result = action_result  # type: ignore[assignment]
        return result if isinstance(result, dict) else {"value": result}

    async def _handle_retry_pause(self, step: SagaStep, attempt: int, exc: Exception):
        delay = step.retry_backoff * (2 ** (attempt - 1))
        logger.warning(f"Saga '{self.name}' step '{step.name}' retry {attempt}/{step.retry_attempts} in {delay:.1f}s: {exc}")
        await asyncio.sleep(delay)

    async def _handle_step_failure(self, step: SagaStep, attempt: int, step_exc: Exception | None):
        step.status = SagaStatus.FAILED
        if attempt > step.retry_attempts:
            logger.error(f"Saga '{self.name}' step '{step.name}' failed after {step.retry_attempts} retries: {step_exc}")
        else:
            logger.error(f"Saga '{self.name}' step '{step.name}' failed: {step_exc}")

        await self._compensate(self._current_step)
        self._save_state()
        if step_exc is not None:
            raise step_exc

    async def _record_step(self, step: SagaStep, idemp_key: str | None) -> None:
        """Record completed step in idempotency log and update saga state."""
        if idemp_key:
            await self._record_completed(idemp_key, step.name, step.result)
        step.data = self._data.copy()
        self._data.update(step.result)
        step.status = SagaStatus.COMPLETED
        self._completed_steps.append(self._current_step)
        self._save_state()
        logger.info(f"Saga '{self.name}' step '{step.name}' completed")

    async def execute(self, initial_data: dict | None = None) -> dict:
        if not self._saga_id:
            self._saga_id = self.name + "_" + uuid.uuid4().hex[:8]
        self._data = initial_data or {}
        self._status = SagaStatus.RUNNING
        self._current_step = 0
        self._started_at = time.time()
        self._completed_steps = []
        self._save_state()

        logger.info(f"Saga '{self.name}' started (id={self._saga_id})")

        try:
            for i, step in enumerate(self._steps):
                self._current_step = i
                step.status = SagaStatus.RUNNING
                self._save_state()

                replayed, idemp_key = await self._check_idempotency(step)
                if replayed:
                    continue

                await self._execute_step_with_retry(step)
                await self._record_step(step, idemp_key)

            self._status = SagaStatus.COMPLETED
            self._cleanup_state()
            logger.info(f"Saga '{self.name}' completed")
            return self._data

        except Exception:
            if self._status != SagaStatus.COMPENSATED:
                self._status = SagaStatus.FAILED
            self._save_state()
            logger.exception("Saga '%s' failed", self.name)
            raise

    async def _compensate(self, failed_step: int) -> None:
        self._status = SagaStatus.COMPENSATING
        self._save_state()
        logger.info(f"Saga '{self.name}' compensating from step {failed_step}")

        for i in range(failed_step - 1, -1, -1):
            step = self._steps[i]
            if step.status != SagaStatus.COMPLETED:
                continue

            if isinstance(step.action, Saga):
                await self._compensate_inner_saga(step.action)
            elif step.compensation:
                await self._compensate_step(step)

        self._status = SagaStatus.COMPENSATED

    async def _compensate_inner_saga(self, inner: Saga) -> None:
        """Compensate all completed steps of a nested saga in reverse order."""
        for j in range(len(inner._steps) - 1, -1, -1):
            inner_step = inner._steps[j]
            if inner_step.status == SagaStatus.COMPLETED and inner_step.compensation:
                try:
                    await inner_step.compensation(inner_step.data)
                    logger.info("Saga '%s' compensated inner step '%s'", self.name, inner_step.name)
                except Exception:
                    logger.exception("Saga '%s' inner compensation failed for '%s'", self.name, inner_step.name)

    async def _compensate_step(self, step: SagaStep) -> None:
        """Run compensation for a single step, logging success or failure."""
        if not step.compensation:
            return
        try:
            await step.compensation(step.data)
            logger.info("Saga '%s' compensated step '%s'", self.name, step.name)
        except Exception:
            logger.exception("Saga '%s' compensation failed for '%s'", self.name, step.name)

    def get_state(self) -> dict:
        return {
            "name": self.name,
            "saga_id": self._saga_id,
            "status": self._status.value,
            "current_step": self._current_step,
            "started_at": self._started_at,
            "data": self._data.copy(),
            "steps": [{"name": s.name, "status": s.status.value, "result": s.result} for s in self._steps],
        }


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
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Saga watchdog started (interval=%ds)", self.check_interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while self._running:
            try:
                self._check_stuck_sagas()
                time.sleep(self.check_interval)
            except Exception:
                logger.exception("Saga watchdog error")
                time.sleep(30)

    def _check_stuck_sagas(self) -> None:
        """Find and mark stuck sagas."""
        SAGA_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()

        for state_file in SAGA_DIR.glob("*.json"):
            try:
                blob = state_file.read_bytes()
                state = decrypt_json(blob) if _HAS_ENCRYPTION and is_encrypted_blob(state_file) else json.loads(blob.decode("utf-8"))

                status = state.get("status", "")
                started_at = state.get("started_at", 0)
                saga_name = state.get("name", "unknown")

                if status in ("running", "compensating"):
                    age = now - started_at
                    if age > self.max_age_seconds:
                        state["status"] = "stuck"
                        state["stuck_reason"] = f"timeout_after_{int(age)}s"
                        if _HAS_ENCRYPTION:
                            # noqa: SKY-D324
                            state_file.write_bytes(encrypt_json(state))
                        else:
                            # noqa: SKY-D324
                            state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
                        logger.warning("Saga '%s' marked as STUCK (age=%ds)", saga_name, int(age))

            except Exception:
                logger.exception("Error checking saga %s", state_file.name)

    def get_stuck_sagas(self) -> list[dict[str, Any]]:
        """Get list of stuck sagas."""
        stuck = []
        SAGA_DIR.mkdir(parents=True, exist_ok=True)

        for state_file in SAGA_DIR.glob("*.json"):
            try:
                blob = state_file.read_bytes()
                state = decrypt_json(blob) if _HAS_ENCRYPTION and is_encrypted_blob(state_file) else json.loads(blob.decode("utf-8"))

                if state.get("status") in ("stuck", "failed", "running"):
                    age = time.time() - state.get("started_at", 0)
                    stuck.append(
                        {
                            "saga_id": state.get("saga_id"),
                            "name": state.get("name"),
                            "status": state.get("status"),
                            "current_step": state.get("current_step"),
                            "age_seconds": int(age),
                        }
                    )
            except Exception:
                pass

        return stuck

    def recover_saga(self, saga_id: str) -> dict[str, Any] | None:
        """Attempt to recover a stuck saga."""
        state_file = SAGA_DIR / (saga_id + ".json")
        if not state_file.exists():
            return None

        try:
            blob = state_file.read_bytes()
            state = decrypt_json(blob) if _HAS_ENCRYPTION and is_encrypted_blob(state_file) else json.loads(blob.decode("utf-8"))

            if state.get("status") != "stuck":
                return {"error": "Saga is not stuck, status: {}".format(state.get("status"))}

            state["status"] = "manual_review_required"
            state["recovered_at"] = time.time()
            if _HAS_ENCRYPTION:
                # noqa: SKY-D324
                state_file.write_bytes(encrypt_json(state))
            else:
                # noqa: SKY-D324
                state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

            return {"status": "manual_review_required", "state": state}
        except Exception as e:
            return {"error": str(e)}

    def cleanup_completed(self) -> int:
        """Delete completed, compensated, stuck, failed, and manual_review_required sagas older than 1 hour."""
        cutoff = time.time() - 3600
        removed = 0
        SAGA_DIR.mkdir(parents=True, exist_ok=True)

        for state_file in SAGA_DIR.glob("*.json"):
            try:
                blob = state_file.read_bytes()
                state = decrypt_json(blob) if _HAS_ENCRYPTION and is_encrypted_blob(state_file) else json.loads(blob.decode("utf-8"))

                if state.get("status") in ("completed", "compensated", "stuck", "failed", "manual_review_required"):
                    if state.get("started_at", 0) < cutoff:
                        state_file.unlink()
                        removed += 1
            except Exception:
                pass

        return removed


# Singleton watchdog
saga_watchdog = SagaWatchdog()


# Ready-made sagas moved to backup.py and consolidation.py
