from __future__ import annotations

"""
Backup Cron — automatic scheduled backups with jitter + wiki sync.
"""

import asyncio
import contextlib
import fcntl  # Unix-only by design: single-writer backup lock (Linux hosts)
import json
import logging
import os
import random
from pathlib import Path
import threading
import time
from pathlib import Path
from typing import Any

from config import config
from features.backup import snapshot_sqlite
from shared.path_safety import safe_resolve

logger = logging.getLogger(__name__)

# Per-layer budget for the nightly maintenance pass. Raised from 120 s after
# measuring the real cost on snapshots of the live bases: hermes spends 138 s in
# the `user` layer alone with every content-hash cache warm, so at 120 s the pass
# was cut mid-layer and the `agent` layer was skipped entirely.
# NOT raised for the other maintenance steps, which keep the 120 s default: they
# are separate jobs with their own cost, and widening them would change their
# behaviour too.
NIGHTLY_LAYER_BUDGET_S = 180

# Retry policy for the nightly pass, deliberately decoupled from the 24 h backup
# window. The pass used to be reachable only from inside `_check_backup`, which
# returns early until a full day has passed since the last backup, while
# `_do_backup` stamps `_last_backup` BEFORE the hooks run. A pass that died — on
# the layer budget, or because the process was restarted mid-pass — therefore
# waited a whole day for its next attempt, leaving no trace that it was ever
# tried. Measured on the live hermes base: the 04.10 and 05.10 passes both died,
# `cycles_state.last_nightly` stayed at 04.10 08:54, and both days still wrote a
# backup with no nightly recorded next to it. Restarts are routine here (hermes
# had 10 on 06.10 alone), so a retry must not wait for the next backup at all.
# It backs off instead: 15 min -> 1 h -> 4 h, then 4 h for every further failure.
NIGHTLY_BACKOFF_S = (900.0, 3600.0, 14400.0)

# A pass whose `_await_on_main_loop(...).result(timeout=)` expired leaves its
# coroutine RUNNING on the main loop — the timeout abandons, it does not cancel
# (see `hooks/shared.timed_step`). The nightly must not be started again on top
# of that: two passes over the same graph at once would occupy the event loop the
# whole agent shares, which is the one thing this scheduler must never cause. The
# unfinished-job guard therefore refuses a retry while work is still pending, but
# not forever — a pass still unfinished after this long is treated as lost, so
# the nightly can never be blocked permanently by a coroutine that never returns.
NIGHTLY_INFLIGHT_GRACE_S = 1800.0


class BackupCron:
    def __init__(self, base_dir: str | None = None):
        if base_dir is None:
            # Same resolution as AsyncConnectionManager: the per-instance
            # MCP_MEMORY_DATA_DIR. The old hard-coded ~/.mcp-ariel-memory made
            # every agent process (cowagent/hermes/mimocode) back up and
            # cycle-gate the shared default DB instead of its own live one.
            from shared.connection import connection_manager

            base_dir = str(connection_manager.base_dir)
        self.base_dir = Path(base_dir)
        self.backup_dir = self.base_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.interval_hours = config.get("backup", "backup_interval_hours") or 24
        self.retention_days = config.get("backup", "backup_retention_days") or 30
        self.keep_count = config.get("backup", "backup_keep_count") or 10
        self.jitter_seconds = config.get("backup", "jitter_seconds") or 3600
        self.wiki_sync_interval = config.get("backup", "wiki_sync_interval_minutes") or 30
        self._running = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_backup = 0.0
        self._last_wiki_sync = 0.0
        # Nightly retry bookkeeping. Persisted through `_save_state` because
        # restarts are routine here: in-memory backoff would reset to 15 min on
        # every restart, turning a restart-heavy day into an attempt-heavy one.
        self._nightly_failures = 0
        self._nightly_retry_at = 0.0
        self._nightly_started_at = 0.0
        # Coroutines handed to the main loop that have not finished yet.
        self._pending_futures: list[Any] = []
        self._state_file = self.base_dir / ".backup_cron_state.json"
        self._lock_path = self.base_dir / ".backup_cron.lock"
        self._load_state()

    def _acquire_backup_lock(self) -> int | None:
        """Non-blocking leader lock for this base dir. fd or None.

        fcntl locks die with the process — no stale-lock class by design.
        """
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    def _release_backup_lock(self, fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _load_state(self) -> None:
        # Two plain `time.time()` values, so the file is deliberately left as
        # plain JSON (see `_save_state`) — it then survives a master-key change.
        # `rotate=False` is required: this runs at import time via the module
        # singleton, and rotating would rewrite the file under WHOEVER imported
        # us. A warm-cache tool with a different key did exactly that to a live
        # base on 06.10, stranding the server's own state behind a foreign key.
        if self._state_file.exists():
            with contextlib.suppress(Exception):
                from shared.saga import read_state_legacy_or_encrypted

                state = read_state_legacy_or_encrypted(self._state_file, rotate=False)
                self._last_backup = state.get("last_backup", 0.0)
                self._last_wiki_sync = state.get("last_wiki_sync", 0.0)
                self._nightly_failures = int(state.get("nightly_failures", 0) or 0)
                self._nightly_retry_at = float(state.get("nightly_retry_at", 0.0) or 0.0)

    def _save_state(self) -> None:
        self._state_file.write_text(
            json.dumps(
                {
                    "last_backup": self._last_backup,
                    "last_wiki_sync": self._last_wiki_sync,
                    "nightly_failures": self._nightly_failures,
                    "nightly_retry_at": self._nightly_retry_at,
                }
            ),
            encoding="utf-8",
        )

    def start(self) -> None:
        if self._running:
            return
        from config import config

        if not config.is_feature_enabled("backup_cron"):
            return
        if os.environ.get("BACKUP_CRON_DISABLED"):
            return
        self._running = True
        self._stop_event.clear()
        # Named so a py-spy dump reads `Thread ... "backup-cron"` instead of an
        # anonymous "Thread-N". Note faulthandler (the no-root SIGUSR1 path in
        # mcp_server/server.py) prints only bare thread ids — names live in
        # py-spy, which needs root here.
        self._thread = threading.Thread(target=self._loop, daemon=True, name="backup-cron")
        self._thread.start()
        jitter_info = f" (+{self.jitter_seconds}s jitter)" if self.jitter_seconds else ""
        logger.info(f"Backup cron started (interval={self.interval_hours}h{jitter_info})")

    def capture_main_loop(self) -> None:
        """Remember the server's event loop for cron-thread jobs.

        aiosqlite connections are bound to that loop; scheduling coroutines
        onto it avoids the cross-loop deadlock class.
        """
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

    def _await_on_main_loop(self, coro: Any, timeout: float = 120) -> Any:
        if self._main_loop is not None and self._main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
            # Remember it before waiting. `.result(timeout=)` abandons the
            # coroutine on expiry instead of cancelling it, so an expired pass
            # leaves real work on the shared main loop; `_nightly_busy` reads
            # this list to refuse starting a second nightly on top of it.
            self._pending_futures = [f for f in self._pending_futures if not f.done()]
            self._pending_futures.append(future)
            return future.result(timeout=timeout)
        return asyncio.run(coro)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()  # interrupt jitter / tick sleeps immediately
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
                self._stop_event.wait(60)
            except Exception:
                logger.exception("Backup cron error")
                self._stop_event.wait(300)

    def _tick(self) -> None:
        now = time.time()
        # Each schedule is isolated: a failure in one (an unwritable state file, a
        # lock error) must not cost the others their tick. Before, the three ran
        # bare inside one try, so an exception in the first cancelled the backup
        # for that tick — and the wiki sync, which writes nothing, silently.
        #
        # Nightly first, and on its own schedule: it is the pass that used to be
        # lost for a whole day when it died, and the one that holds the
        # single-writer lock for minutes. A backup that waits one tick for that
        # lock is harmless; a nightly that waits a day is not.
        for step in (self._check_nightly, self._check_backup, self._check_wiki_sync):
            try:
                step(now)
            except Exception:
                logger.exception("Backup cron step %s failed", step.__name__)

    def _cycles_state_path(self) -> Path:
        # This base's own cycle state, not the connection manager's: they are the
        # same base in production (the singleton defaults to it), and keeping it
        # on `self.base_dir` means a BackupCron built for an explicit base reads
        # that base's cycles — not whatever base the process happens to have, and
        # not the live one while a test runs.
        return self.base_dir / "cycles_state.json"

    def _nightly_busy(self, now: float) -> bool:
        """Is work from an earlier nightly pass still running on the main loop."""
        self._pending_futures = [f for f in self._pending_futures if not f.done()]
        if not self._pending_futures:
            self._nightly_started_at = 0.0
            return False
        if self._nightly_started_at and now - self._nightly_started_at > NIGHTLY_INFLIGHT_GRACE_S:
            logger.warning(
                "Nightly pass still has %d unfinished job(s) on the main loop after %.0f min — "
                "treating it as lost and allowing a retry; a second pass may briefly overlap",
                len(self._pending_futures),
                (now - self._nightly_started_at) / 60.0,
            )
            self._pending_futures.clear()
            self._nightly_started_at = 0.0
            return False
        return True

    def _check_nightly(self, now: float) -> None:
        """Run the nightly pass when the cycle is due, independent of backups.

        See `NIGHTLY_BACKOFF_S` for why this is not called from `_check_backup`
        any more. The gate (`nightly_gate`) still decides *when* the cycle is
        due; what changed is how often we are allowed to ask and what happens
        when an attempt fails: the next attempt comes after a backoff, not after
        the next 24 h backup window.
        """
        if self._nightly_busy(now):
            logger.info("Nightly skipped: the previous pass is still running")
            return
        if now < self._nightly_retry_at:
            return

        state_path = self._cycles_state_path()
        try:
            from features.cycles import nightly_gate

            gate = nightly_gate(state_path)
        except Exception:
            logger.exception("Cycles gate error — running nightly unguarded")
            gate = {"action": "run", "budget": "unknown", "reason": "gate_error"}
        if gate.get("action") != "run":
            logger.info("Nightly skipped by cycles gate: %s", gate)
            return

        # Same single-writer lock as the backup, so gateway/dashboard MCP twins
        # cannot both run the pass. Not being the leader is NOT a failure: the
        # leader is doing the work, so no backoff is recorded.
        fd = self._acquire_backup_lock()
        if fd is None:
            logger.info("Nightly skipped: another writer holds the lock")
            return
        self._nightly_started_at = now
        try:
            verdict = self._fire_nightly_hooks(state_path, gate_checked=True)
        finally:
            self._release_backup_lock(fd)

        if verdict == "failed":
            self._nightly_failures += 1
            delay = NIGHTLY_BACKOFF_S[min(self._nightly_failures, len(NIGHTLY_BACKOFF_S)) - 1]
            self._nightly_retry_at = time.time() + delay
            self._save_state()
            logger.warning(
                "Nightly pass failed (%d in a row) — next attempt in %.0f min",
                self._nightly_failures,
                delay / 60.0,
            )
        else:
            if self._nightly_failures:
                logger.info("Nightly pass recovered after %d failed attempt(s)", self._nightly_failures)
            self._nightly_failures = 0
            self._nightly_retry_at = 0.0
            self._save_state()

    def _check_backup(self, now: float) -> None:
        next_backup = self._last_backup + self.interval_hours * 3600
        if now < next_backup:
            return

        jitter = random.randint(0, self.jitter_seconds) if self.jitter_seconds else 0
        if jitter:
            logger.info(f"Backup jitter: waiting {jitter}s")
            self._stop_event.wait(jitter)
            if not self._running:  # stopped inside the jitter window
                return

        # Single writer per base dir: gateway/dashboard MCP twins must not
        # each write a full backup. Lock AFTER jitter so we never squat it.
        fd = self._acquire_backup_lock()
        if fd is None:
            logger.info("Backup skipped: another writer holds the lock")
            return
        try:
            self._do_backup()
            self._cleanup_old()
        finally:
            self._release_backup_lock(fd)

    def _check_wiki_sync(self, now: float) -> None:
        if now - self._last_wiki_sync >= self.wiki_sync_interval * 60:
            self._sync_wiki()

    def _fire_nightly_hooks(self, state_path: Path | None = None, *, gate_checked: bool = False) -> str:
        """Run the nightly maintenance pass. Returns 'ok', 'failed' or 'skipped'.

        The verdict is new and is the point of the change: a pass that died used
        to return normally, so the caller could not tell it apart from a pass
        that finished, and the backup had already marked itself done by then.
        Callers scheduling a retry read 'failed'.

        `gate_checked=True` means the caller already consulted `nightly_gate`
        (that is `_check_nightly`, which needs the answer before taking the
        lock). The default re-checks it, so a direct call still refuses to run
        an unripe cycle.
        """
        if state_path is None:
            state_path = self._cycles_state_path()
        # C7 cycles-daemon gate: cycle_due (persistent last_run) + triple
        # cost-cap. The nightly pass runs only when the cycle is due and the
        # budget is not blocked — the scheduler from the S13 design doc.
        if not gate_checked:
            try:
                from features.cycles import nightly_gate

                gate = nightly_gate(state_path)
                if gate["action"] != "run":
                    logger.info("Nightly skipped by cycles gate: %s", gate)
                    return "skipped"
            except Exception:
                logger.exception("Cycles gate error — running nightly unguarded")
        verdict = "ok"
        try:
            from hooks.registry import hook_registry

            for layer in ["user", "agent"]:
                # Timing only. The `finally` re-raises, so a timeout still skips
                # the remaining layers exactly as it did before: this logs which
                # layer was in flight and what it really cost, without changing
                # what runs. The bare "Nightly hook error" that used to be the
                # only trace did not say which layer died or how close the other
                # came to the nightly layer budget.
                started = time.monotonic()
                logger.info("Nightly pass: layer=%s starting", layer)
                try:
                    self._await_on_main_loop(
                        hook_registry.fire("nightly", layer, {"trigger": "backup_cron"}),
                        timeout=NIGHTLY_LAYER_BUDGET_S,
                    )
                finally:
                    logger.info("Nightly pass: layer=%s finished in %.1f s", layer, time.monotonic() - started)
            with contextlib.suppress(Exception):
                from features.cycles import record_nightly_done

                record_nightly_done(state_path)  # record the successful pass
        except Exception:
            logger.exception("Nightly hook error")
            verdict = "failed"
        # Compact-to-budget after nightly builds (graph_build runs inside the
        # "nightly" hook above): evict lowest-activation L4 facts to archive.
        with contextlib.suppress(Exception):
            from lifecycle.compact import compact_under_budget

            for layer in ["user", "agent"]:
                self._await_on_main_loop(compact_under_budget("default", layer))
        # B5: TTL-expiry sweep with mass-delete guards (after compact).
        with contextlib.suppress(Exception):
            from lifecycle.l0_sweep import sweep_expired

            self._await_on_main_loop(sweep_expired())
        # L0 replay: wake rows that were never processed. This is the reader that
        # `received` never had — the status was written by every message and read by
        # nothing automatic, so a row could sit unprocessed indefinitely. Bounded by
        # `l0.replay_max_rows` and gated by the importance gate (see features/replay),
        # so a backlog drains over successive nights instead of landing in one burst.
        # Ordered BEFORE the expiry and the tiering: a row that qualifies for replay
        # should get its chance to be distilled before anything closes or compresses it.
        with contextlib.suppress(Exception):
            from features.replay import replay

            replayed = self._await_on_main_loop(replay())
            if any(replayed.get(k) for k in ("processed", "gated", "conflicts")):
                logger.info("L0 replay: %s", replayed)
        # L0 expiry: `received` older than `l0.received_ttl_days` is a strand, not a
        # wait forever. Closed as `gated_out` / `never_processed` — which is also the
        # status that makes it eligible for the tiers below, where the text is kept.
        # Parked rows (deliberate "wake later" imports) are untouched.
        with contextlib.suppress(Exception):
            from lifecycle.l0_tiers import close_overdue_received

            expired = self._await_on_main_loop(close_overdue_received())
            if expired.get("closed"):
                logger.info("L0 received expiry: %s", expired)
        # S6: L0 tiering — warm (preview+zlib) / cold (CLACK archive). After the
        # sweep so freshly processed rows age through tiers in a stable order.
        with contextlib.suppress(Exception):
            from lifecycle.l0_tiers import tier_l0

            tiers = self._await_on_main_loop(tier_l0())
            logger.info("L0 tiering: %s", tiers)
        # F-T7 (S3): rebuild session summaries from L0 texts (was dead code).
        with contextlib.suppress(Exception):
            from features.l2_enrich import enrich_sessions

            self._await_on_main_loop(enrich_sessions(days=1))
        # A8: MEMORY.md bridge — refresh top-facts file, then drain user notes.
        with contextlib.suppress(Exception):
            from features.bridge import ingest_drain, regenerate_bridge

            self._await_on_main_loop(regenerate_bridge("default", "agent"))
            self._await_on_main_loop(ingest_drain("default", "agent"))
        return verdict

    def _do_backup(self) -> str:
        import shutil
        import uuid

        timestamp = int(time.time())
        name = f"auto_{timestamp}_{uuid.uuid4().hex[:6]}"
        dest = self.backup_dir / name
        dest.mkdir(parents=True, exist_ok=True)

        db_files = ["memory.db"]
        backed_up = []
        for db_file in db_files:
            src = self.base_dir / db_file
            if src.exists():
                snapshot_sqlite(src, dest / db_file)
                backed_up.append(db_file)

        # Backup wiki .md files
        wiki_dir = self.base_dir / "wiki"
        if wiki_dir.exists():
            shutil.copytree(wiki_dir, dest / "wiki", dirs_exist_ok=True)
            backed_up.append("wiki/")

        manifest = {"name": name, "timestamp": timestamp, "files": backed_up}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        self._last_backup = time.time()
        self._save_state()
        logger.info("Auto-backup created: %s (%d files)", name, len(backed_up))
        return str(dest)

    def _cleanup_old(self) -> None:
        import shutil

        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0
        for d in self.backup_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed += 1
        # Count cap regardless of age: N writers × interval overflowed age-only
        # retention (2.4 backups/day × 30d ≈ 5G per base, incident 18.09).
        kept = sorted(
            (d for d in self.backup_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in kept[self.keep_count :]:
            shutil.rmtree(d)
            removed += 1
        if removed:
            logger.info("Cleaned up %d old backups", removed)
        self._cleanup_tmp()

    # Test artifacts in the temp dir do not clean themselves up: hermetic fixtures
    # leave ariel-test-global-*, the eval harness — ariel-eval-*, pytest — its own
    # pytest-of-<user>. A full /tmp on tmpfs stalled the local pre-push pytest gate
    # (2026-09-06), and on 2026-10-06 the same tmpfs hit its user quota: every write
    # started failing with EDQUOT, which took down every tool of the DSH sandbox
    # because the harness writes its own command output to /tmp too.
    #
    # The window is HOURS, not days. This sweep is reachable only from `_do_backup`,
    # so it runs about once a day: a 2-day threshold meant a leak had to survive two
    # daily passes before anything touched it, and a day's worth of artifacts sat in
    # the temp dir in the meantime — exactly what filled it in both incidents. Six
    # hours is far longer than anything that legitimately writes here (the full suite
    # takes ~90 s, pytest caps a single test at 60 s, and the eval and hermetic dirs
    # are per-session), yet short enough that a leak is collected the same day.
    _TMP_CLEANUP_HOURS = 6.0

    def _cleanup_tmp(self, tmp_root: Path | None = None) -> int:
        """Tear down stale test artifacts in the temp dir. Strict prefixes, best-effort."""
        import getpass
        import shutil as _shutil
        import tempfile

        # Resolve the real temp dir: TMPDIR can point elsewhere (/var/tmp on 2026-10-01),
        # and a hardcoded /tmp left 4.4G of pytest/eval artifacts uncleaned (2026-10-01).
        root = tmp_root or Path(tempfile.gettempdir())
        cutoff = time.time() - self._TMP_CLEANUP_HOURS * 3600
        candidates: list[Path] = []
        pytest_base = root / f"pytest-of-{getpass.getuser()}"
        if pytest_base.is_dir() and not pytest_base.is_symlink():
            candidates.extend(pytest_base.glob("pytest-*"))
        candidates.extend(root.glob("ariel-test-global-*"))
        candidates.extend(root.glob("ariel-eval-*"))
        # pytest points `pytest-current` at the run in progress, and an age test alone
        # cannot recognise it: a run that started one minute ago and one that finished
        # one minute ago both look fresh. Skipping that target is cheap and exact, and
        # it is what keeps a shortened window safe for a suite being run right now.
        current = pytest_base / "pytest-current"
        live = {os.path.realpath(current)} if current.is_symlink() else set()
        removed = 0
        for d in candidates:
            try:
                if d.is_dir() and not d.is_symlink() and os.path.realpath(d) not in live and d.stat().st_mtime < cutoff:
                    _shutil.rmtree(d)
                    removed += 1
            except OSError:
                continue  # someone else's / busy directory — not our concern
        if removed:
            logger.info("Tmp cleanup: removed %d stale test dirs (>%gh)", removed, self._TMP_CLEANUP_HOURS)
        return removed

    def _sync_wiki(self) -> None:
        """Synchronize wiki files with disk."""
        try:
            from wiki import WikiManager

            for layer in ["user", "agent"]:
                fw = WikiManager(layer=layer)
                raw = fw.reindex_all()
                result: dict[str, Any] = self._await_on_main_loop(raw) if asyncio.iscoroutine(raw) else raw
                if isinstance(result, dict) and result.get("indexed", 0) > 0:
                    logger.info("Wiki %s synced: %d files", layer, result["indexed"])
            self._last_wiki_sync = time.time()
            self._save_state()
        except Exception:
            logger.exception("Wiki sync error")

    def backup_now(self) -> str:
        return self._do_backup()

    def restore(self, backup_name: str) -> dict[str, Any]:
        src = self.backup_dir / backup_name
        if not src.exists() or src.is_symlink():
            return {"error": f"Backup not found or invalid: {backup_name}"}

        manifest_path = src / "manifest.json"
        if manifest_path.exists() and not manifest_path.is_symlink():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"files": [f.name for f in src.glob("*.db")]}

        restored: list[str] = []
        for db_file in manifest.get("files", []):
            dest_path = self.base_dir / db_file
            if dest_path.exists() and dest_path.is_symlink():
                logger.error("Refusing to restore over symlink: %s", dest_path)
                continue

            self._restore_entry(src, db_file, dest_path, restored)

        return {"restored": restored, "backup": backup_name}

    def _restore_entry(self, src: Path, db_file: str, dest_path: Path, restored: list[str]) -> None:
        """Restore one manifest entry — a wiki/ directory or a db file."""
        import shutil

        safe_resolve(self.base_dir, db_file)  # raises ValueError if traversal
        if db_file.endswith("/"):
            src_wiki = src / db_file
            if src_wiki.exists() and not src_wiki.is_symlink():
                shutil.copytree(src_wiki, dest_path, dirs_exist_ok=True)
                restored.append(db_file)
            return

        backup_file = src / db_file
        if backup_file.exists() and not backup_file.is_symlink():
            # skylos: ignore [SKY-D215, SKY-D325] - Safe via safe_resolve and symlink checks
            shutil.copy2(backup_file, dest_path)
            restored.append(db_file)

    def list_backups(self) -> list[dict[str, Any]]:
        backups: list[dict[str, Any]] = []
        for d in sorted(self.backup_dir.iterdir(), reverse=True):
            if d.is_dir():
                info = {"name": d.name}
                manifest_path = d / "manifest.json"
                if manifest_path.exists():
                    with contextlib.suppress(Exception):
                        info.update(json.loads(manifest_path.read_text(encoding="utf-8")))
                backups.append(info)
        return backups

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "interval_hours": self.interval_hours,
            "jitter_seconds": self.jitter_seconds,
            "retention_days": self.retention_days,
            "keep_count": self.keep_count,
            "wiki_sync_interval_minutes": self.wiki_sync_interval,
            "last_backup": self._last_backup,
            "next_backup": self._last_backup + self.interval_hours * 3600,
            "nightly_failures": self._nightly_failures,
            "next_nightly_retry": self._nightly_retry_at,
            "backup_count": len(list(self.backup_dir.iterdir())),
        }


backup_cron = BackupCron()
