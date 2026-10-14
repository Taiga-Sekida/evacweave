"""Small fail-closed per-run attempt budgets.

This is an application guard, not a Google Cloud billing cap. A consumed attempt is
never refunded automatically because a transport error may occur after billable work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

ProviderKind = Literal["gemma", "gemini"]


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class AttemptBudget:
    gemma_limit: int = 30
    gemini_limit: int = 2
    _gemma_used: int = 0
    _gemini_used: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.gemma_limit <= 100:
            raise ValueError("gemma_limit must be between 0 and 100")
        if not 0 <= self.gemini_limit <= 10:
            raise ValueError("gemini_limit must be between 0 and 10")

    def consume(self, provider: ProviderKind, count: int = 1) -> None:
        if not 1 <= count <= 100:
            raise ValueError("count must be between 1 and 100")
        with self._lock:
            if provider == "gemma":
                if self._gemma_used + count > self.gemma_limit:
                    raise BudgetExceeded("per-run Gemma attempt budget exhausted")
                self._gemma_used += count
                return
            if provider == "gemini":
                if self._gemini_used + count > self.gemini_limit:
                    raise BudgetExceeded("per-run Gemini attempt budget exhausted")
                self._gemini_used += count
                return
            raise ValueError("unknown provider")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "gemma_limit": self.gemma_limit,
                "gemma_used": self._gemma_used,
                "gemini_limit": self.gemini_limit,
                "gemini_used": self._gemini_used,
            }
