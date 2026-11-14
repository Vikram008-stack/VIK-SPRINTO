"""Per-company budget guard: max cost (USD) and max wall-clock seconds.

Steps call `guard.check()` before any paid call. On breach the company stops and is marked
INSUFFICIENT_DATA with reason budget_exceeded (a designed handoff, not a crash).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, used: float, limit: float):
        self.kind, self.used, self.limit = kind, used, limit
        super().__init__(f"{kind} budget exceeded: {used:.4f} > {limit}")


@dataclass
class CompanyBudget:
    max_cost_usd: float
    max_seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(default=0.0)
    cost_usd: float = 0.0
    unknown_cost_calls: int = 0      # calls whose price is unknown (counted, never guessed)
    tokens: int = 0
    scrape_credits: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    def add(self, cost_usd: Optional[float] = None, tokens: int = 0, credits: int = 0) -> None:
        if cost_usd is None:
            self.unknown_cost_calls += 1
        else:
            self.cost_usd += cost_usd
        self.tokens += tokens
        self.scrape_credits += credits

    def check(self) -> None:
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost_usd", self.cost_usd, self.max_cost_usd)
        if self.elapsed > self.max_seconds:
            raise BudgetExceeded("seconds", self.elapsed, self.max_seconds)

    def snapshot(self) -> dict:
        return {"cost_usd": round(self.cost_usd, 6), "seconds": round(self.elapsed, 2),
                "tokens": self.tokens, "scrape_credits": self.scrape_credits,
                "unknown_cost_calls": self.unknown_cost_calls}
