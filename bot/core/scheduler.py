"""Multi-feature scheduler with independent intervals and error isolation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bot.core.feature import BaseFeature

logger = logging.getLogger(__name__)


@dataclass
class _ScheduledTask:
    feature: BaseFeature
    next_run_at: float = 0.0


class BotScheduler:
    """Runs multiple BaseFeature tasks according to each feature's own interval."""

    def __init__(self, tick_seconds: float = 1.0) -> None:
        self._tasks: list[_ScheduledTask] = []
        self._tick_seconds = tick_seconds

    def register(self, feature: BaseFeature) -> None:
        """Register a feature to be managed by the scheduler."""
        self._tasks.append(_ScheduledTask(feature=feature, next_run_at=0.0))
        logger.info(
            "Đã đăng ký tính năng '%s' (chu kỳ: %ss).",
            feature.name,
            feature.interval_seconds,
        )

    def run_once(self) -> bool:
        """Execute all registered features immediately once.

        Returns ``True`` if every feature completed without raising an exception.
        """
        all_ok = True
        for task in self._tasks:
            if not self._safe_execute(task.feature):
                all_ok = False
        return all_ok

    def run_forever(self) -> None:
        """Main event loop running features whenever their schedule is due."""
        if not self._tasks:
            logger.warning("Không có tính năng nào được kích hoạt. Thoát chương trình.")
            return

        logger.info("Khởi động bot với %d tính năng.", len(self._tasks))
        try:
            while True:
                now = time.monotonic()
                for task in self._tasks:
                    if now >= task.next_run_at:
                        self._safe_execute(task.feature)
                        task.next_run_at = time.monotonic() + max(1, task.feature.interval_seconds)

                sleep_for = self._compute_sleep(time.monotonic())
                time.sleep(sleep_for)
        except KeyboardInterrupt:
            logger.info("Nhận tín hiệu dừng (Ctrl+C). Đang tắt bot...")

    def _compute_sleep(self, now: float) -> float:
        next_due = min(task.next_run_at for task in self._tasks)
        remaining = max(0.0, next_due - now)
        return min(self._tick_seconds, remaining) if remaining > 0 else 0.0

    @staticmethod
    def _safe_execute(feature: BaseFeature) -> bool:
        try:
            feature.execute()
            return True
        except Exception:
            logger.exception("Lỗi không mong muốn khi chạy tính năng '%s'.", feature.name)
            return False
