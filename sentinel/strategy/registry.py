"""Strategy registry, plugin discovery and lifecycle control.

Lifecycle states, and what each permits:

``reference``     explanatory or benchmark only; never trades.
``hypothesis``    stated, not yet tested. Backtest only.
``experimental``  passed a first evaluation; paper and demo only.
``accepted``      passed the full acceptance protocol; may trade real money.
``suspended``     was accepted, then breached a failure condition. Its
                  positions are managed out; it opens nothing new.

Promotion to ``accepted`` is only possible through
``research.acceptance.promote``, which requires a verdict object carrying the
run id, the thresholds that were in force, and the evidence. Setting the field
by hand raises.

Discovery
---------

Three ways in, one gate:

* **Built-in families.** ``discover_builtin()`` walks
  ``sentinel/strategy/families/`` and registers every ``Strategy`` subclass it
  finds. A new file dropped into that directory needs no edit here.
* **A user directory.** ``load_user_strategies(path)`` imports every ``*.py``
  in a directory outside the package. Set ``ops.strategy_plugin_dir`` in the
  configuration or pass a path explicitly; a broken file is reported and
  skipped, never fatal.
* **Entry points.** A separately installed package can advertise strategies
  under the ``sentinel_fx.strategies`` group.

The gate all three pass through is ``register()``, and it enforces the property
that makes plugins safe: **a strategy class may not declare itself accepted.**
Without that check, dropping one file into a watched directory would be enough
to create a class claiming the lifecycle that authorises real money -- turning
the plugin mechanism into the shortest path around the entire acceptance
protocol. Promotion happens to an *allocation*, against a stored verdict, and
nowhere else.

A warning about the size of this library
----------------------------------------

Thirty strategies do not make this system thirty times more likely to find an
edge. They make it a better overfitting machine, because the best of thirty
searches over one history looks good whether or not any of them has an edge.
The only thing that makes a large library honest is counting the search, which
is what ``research/trials.py`` does and what ``scripts/run_acceptance.py``
enforces. If you add strategies here without the ledger in the loop, you have
made the system worse.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Type

from .base import Strategy
from .baselines import BuyAndHold, CoinFlip, NoTrade, RandomWalk

LIFECYCLE_ORDER = ["reference", "hypothesis", "experimental", "accepted", "suspended"]
TRADES_REAL_MONEY = {"accepted"}
TRADES_PAPER = {"hypothesis", "experimental", "accepted"}

#: Lifecycles a class definition is allowed to declare. ``accepted`` is absent
#: deliberately -- see the module docstring.
DECLARABLE_LIFECYCLES = {"reference", "hypothesis", "experimental"}

_REGISTRY: Dict[str, Type[Strategy]] = {}
_SOURCES: Dict[str, str] = {}
#: Non-fatal problems hit while loading plugins, in load order. Surfaced by
#: ``load_report()`` so a plugin that failed to import is visible rather than
#: mysteriously absent.
_LOAD_ERRORS: List[str] = []


def register(cls: Type[Strategy], *, source: str = "builtin") -> Type[Strategy]:
    """Register a strategy class. Usable directly or as a decorator.

    Rejects, in this order:

    * anything that is not a concrete ``Strategy`` subclass -- a plugin file
      full of helper classes should not fill the registry with them;
    * a class with no ``meta``, or with an empty name;
    * a name collision with a *different* class, because two strategies sharing
      a name makes every verdict, allocation and ledger entry ambiguous;
    * a class declaring ``accepted`` or ``suspended``. Those states are
      conclusions about a specific configuration, held in the verdict store and
      the allocation, and a class cannot assert either.
    """
    if not (inspect.isclass(cls) and issubclass(cls, Strategy)):
        raise TypeError(f"{cls!r} is not a Strategy subclass")
    if inspect.isabstract(cls):
        raise TypeError(f"{cls.__name__} is abstract and cannot be registered")
    meta = getattr(cls, "meta", None)
    name = getattr(meta, "name", "")
    if not name:
        raise ValueError(f"{cls.__name__} has no StrategyMeta.name")
    lifecycle = getattr(meta, "lifecycle", "hypothesis")
    if lifecycle not in DECLARABLE_LIFECYCLES:
        raise ValueError(
            f"strategy {name!r} declares lifecycle {lifecycle!r}. A class may only "
            f"declare one of {sorted(DECLARABLE_LIFECYCLES)}: 'accepted' is earned by "
            "a configuration through scripts/run_acceptance.py and recorded in the "
            "verdict store, never asserted in code.")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(
            f"strategy name collision: {name!r} is already registered from "
            f"{_SOURCES.get(name, 'unknown')}")
    _REGISTRY[name] = cls
    _SOURCES.setdefault(name, source)
    return cls


def strategy(cls: Type[Strategy]) -> Type[Strategy]:
    """Decorator form of :func:`register`, for plugin files that prefer it.

    Built-in families do not need it -- discovery finds every ``Strategy``
    subclass in the module either way -- but an external file may define
    helpers alongside its strategy, and the decorator says which is which.
    """
    return register(cls, source="decorator")


def unregister(name: str) -> None:
    """Remove a strategy. For tests and for reloading a user directory."""
    _REGISTRY.pop(name, None)
    _SOURCES.pop(name, None)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _register_module_members(module, source: str) -> List[str]:
    """Register every concrete Strategy subclass DEFINED in ``module``.

    The ``__module__`` check is what stops a module from re-registering
    everything it imported: ``families/trend.py`` imports ``Strategy`` itself,
    and a family module that imports a peer's class for comparison would
    otherwise claim ownership of it.
    """
    found: List[str] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if not issubclass(obj, Strategy) or obj is Strategy:
            continue
        if inspect.isabstract(obj) or getattr(obj, "meta", None) is None:
            continue
        if getattr(obj, "__module__", "") != module.__name__:
            continue
        try:
            register(obj, source=source)
            found.append(obj.meta.name)
        except (TypeError, ValueError) as exc:
            _LOAD_ERRORS.append(f"{module.__name__}.{obj.__name__}: {exc}")
    return found


def discover_builtin() -> List[str]:
    """Import every family module and register what it defines.

    Idempotent: re-registering the same class under the same name is a no-op,
    so calling this twice (import order, a test, a reload) is safe.
    """
    from . import families

    registered: List[str] = []
    for info in pkgutil.iter_modules(families.__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{families.__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001 - one bad family must not hide the rest
            _LOAD_ERRORS.append(f"family {info.name}: {exc.__class__.__name__}: {exc}")
            continue
        registered.extend(_register_module_members(module, source=f"family:{info.name}"))
    return registered


def load_user_strategies(directory: str | Path, *,
                         reload: bool = False) -> List[str]:
    """Import every ``*.py`` in ``directory`` and register what it defines.

    A missing directory returns an empty list rather than raising: "the
    operator has not configured one" is the normal case, not an error.

    A file that fails to import is recorded in ``load_report()`` and skipped.
    That choice is deliberate and worth stating: the alternative -- refusing to
    start -- turns a typo in an experimental strategy into an outage for a
    system that may be holding positions. The cost is that a broken plugin is
    silent unless someone reads the report, which is why the report is exposed
    through the API's strategy endpoint.

    Security note: importing a Python file executes it. The directory is
    therefore as trusted as the process itself, and it must not be writable by
    anything the operator does not control.
    """
    path = Path(directory).expanduser()
    if not path.is_dir():
        return []
    registered: List[str] = []
    for file in sorted(path.glob("*.py")):
        if file.name.startswith("_"):
            continue
        mod_name = f"sentinel_user_strategies.{file.stem}"
        if mod_name in sys.modules and not reload:
            registered.extend(_register_module_members(sys.modules[mod_name],
                                                       source=f"user:{file.name}"))
            continue
        try:
            spec = importlib.util.spec_from_file_location(mod_name, file)
            if spec is None or spec.loader is None:
                _LOAD_ERRORS.append(f"user strategy {file.name}: no import spec")
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - see docstring
            sys.modules.pop(mod_name, None)
            _LOAD_ERRORS.append(
                f"user strategy {file.name}: {exc.__class__.__name__}: {exc}")
            continue
        registered.extend(_register_module_members(module, source=f"user:{file.name}"))
    return registered


def load_entry_point_strategies(group: str = "sentinel_fx.strategies") -> List[str]:
    """Register strategies advertised by separately installed packages.

    Each entry point may resolve to a ``Strategy`` subclass or to a module; a
    module has its members scanned exactly as a family module would.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.10
        return []
    registered: List[str] = []
    try:
        points = entry_points(group=group)
    except TypeError:  # pragma: no cover - older selection API
        points = entry_points().get(group, [])  # type: ignore[union-attr]
    for point in points:
        try:
            obj = point.load()
        except Exception as exc:  # noqa: BLE001 - a third-party package must not
            _LOAD_ERRORS.append(                      # break startup
                f"entry point {point.name}: {exc.__class__.__name__}: {exc}")
            continue
        if inspect.isclass(obj):
            try:
                register(obj, source=f"entry_point:{point.name}")
                registered.append(obj.meta.name)
            except (TypeError, ValueError) as exc:
                _LOAD_ERRORS.append(f"entry point {point.name}: {exc}")
        else:
            registered.extend(_register_module_members(
                obj, source=f"entry_point:{point.name}"))
    return registered


def load_report() -> Dict[str, object]:
    """What is loaded, from where, and what failed to load."""
    return {
        "registered": len(_REGISTRY),
        "by_source": {name: _SOURCES.get(name, "unknown") for name in sorted(_REGISTRY)},
        "families": {f: len(available(family=f)) for f in sorted(families())},
        "errors": list(_LOAD_ERRORS),
    }


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


def available(family: Optional[str] = None) -> List[str]:
    if family is None:
        return sorted(_REGISTRY)
    return sorted(n for n, c in _REGISTRY.items()
                  if getattr(c.meta, "family", "unclassified") == family)


def families() -> List[str]:
    return sorted({getattr(c.meta, "family", "unclassified") for c in _REGISTRY.values()})


def family_of(name: str) -> str:
    """The family a strategy belongs to. Used by the trial ledger to decide
    which search a candidate is charged with."""
    return getattr(get(name).meta, "family", "unclassified")


def get(name: str) -> Type[Strategy]:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; available: {available()}")
    return _REGISTRY[name]


def build(name: str, **params) -> Strategy:
    return get(name)(**params)


def describe_all() -> List[dict]:
    out = []
    for name in available():
        cls = _REGISTRY[name]
        m = cls.meta
        out.append({
            "name": m.name, "version": m.version, "description": m.description,
            "family": getattr(m, "family", "unclassified"),
            "timeframe": m.timeframe, "horizon_bars": m.horizon_bars,
            "lifecycle": m.lifecycle, "hypothesis": m.hypothesis,
            "failure_conditions": m.failure_conditions,
            "required_history": m.required_history,
            "source": _SOURCES.get(name, "unknown"),
            "default_params": cls.default_params(),
        })
    return out


def can_trade(lifecycle: str, live_money: bool) -> bool:
    return lifecycle in (TRADES_REAL_MONEY if live_money else TRADES_PAPER)


# Baselines first: they are the yardstick every candidate is measured against
# and must exist even if a family module fails to import.
for _cls in (NoTrade, RandomWalk, CoinFlip, BuyAndHold):
    register(_cls, source="baseline")
discover_builtin()
