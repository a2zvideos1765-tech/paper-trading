"""Auto-discover strategies from this package.

Each strategy lives in its own file (e.g. `s1_user_pyramid.py`) and exports a
module-level constant `STRATEGY: StrategyV2`. To add a new strategy:

  1. Create a new file in src/strategies/ with `STRATEGY = StrategyV2(name=..., ...)`.
  2. Reference its name in config/portfolios.yaml.
  3. Restart paperaglo-trader.

That's it. No registration call, no decorator, no editing of this file.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from src.engine.v2_engine import StrategyV2


_REGISTRY: dict[str, StrategyV2] | None = None


def all_strategies() -> dict[str, StrategyV2]:
    """Lazy auto-discovery. Walks the package and pulls `STRATEGY` from each module."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    out: dict[str, StrategyV2] = {}
    import src.strategies as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in {"registry", "__init__"}:
            continue
        m = importlib.import_module(f"src.strategies.{mod.name}")
        if not hasattr(m, "STRATEGY"):
            continue
        s: StrategyV2 = m.STRATEGY
        if not isinstance(s, StrategyV2):
            raise TypeError(
                f"src/strategies/{mod.name}.py: STRATEGY must be a StrategyV2 instance, "
                f"got {type(s).__name__}"
            )
        if s.name in out:
            raise ValueError(
                f"Duplicate strategy name {s.name!r} in src/strategies/{mod.name}.py "
                f"(also defined elsewhere). Strategy names must be unique."
            )
        out[s.name] = s
    _REGISTRY = out
    return _REGISTRY


def get(name: str) -> StrategyV2:
    reg = all_strategies()
    if name not in reg:
        raise KeyError(
            f"Unknown strategy {name!r}. Known: {sorted(reg)}. "
            f"Add a file under src/strategies/ that exports STRATEGY = StrategyV2(name={name!r}, ...)"
        )
    return reg[name]


def names() -> Iterable[str]:
    return sorted(all_strategies())
