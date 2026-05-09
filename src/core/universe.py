"""Load the symbol universe from config/universe.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.config import REPO_ROOT


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    token: str
    exchange: str


def load_universe() -> tuple[list[SymbolSpec], list[SymbolSpec]]:
    """Returns (equities, indices)."""
    path = REPO_ROOT / "config" / "universe.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    eq = [SymbolSpec(**row) for row in (data.get("symbols") or [])]
    idx = [SymbolSpec(**row) for row in (data.get("indices") or [])]
    return eq, idx


def all_specs() -> list[SymbolSpec]:
    eq, idx = load_universe()
    return [*eq, *idx]
