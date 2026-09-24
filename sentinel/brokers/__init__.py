"""Broker adapters."""

from .base import Broker, BrokerCapabilities, SubmitResult
from .paper import PaperBroker, SimProfile

from .profiles import BrokerProfile, get_profile, list_profiles, register_profile

__all__ = ["Broker", "BrokerCapabilities", "SubmitResult", "PaperBroker", "SimProfile",
           "BrokerProfile", "build_broker", "get_profile", "list_profiles",
           "register_profile"]


def build_broker(name: str, **kwargs):
    """Build an adapter for a venue, by adapter name OR by broker profile name.

    Both of these work, and they mean different things:

        build_broker("mt5")            # the adapter, with the generic profile
        build_broker("amarkets")       # the SAME adapter, with AMarkets' quirks

    The second form is the useful one. Every MetaTrader broker speaks the same
    protocol and none of them behaves the same way -- different symbol
    spellings, different minimum stop distances, different filling modes,
    different server clocks. A profile carries those differences, and the
    adapter reconciles it against the live terminal at startup.

    Real adapters are imported lazily so a missing optional dependency
    (MetaTrader5 is Windows-only, ccxt is not installed by default) never
    breaks the paper path.
    """
    from .profiles import resolve_profile
    from .profiles import venues as _venues  # noqa: F401 - registers profiles

    key = (name or "paper").lower().strip()
    profile = resolve_profile(key)
    adapter = profile.adapter if profile else key

    if adapter == "paper":
        return PaperBroker(**kwargs)
    if adapter == "oanda":
        from .oanda import OandaBroker
        return OandaBroker(**kwargs)
    if adapter in ("mt5", "mt4"):
        from .mt5 import MT5Broker
        if adapter == "mt4":
            raise ValueError(
                "MT4 needs an Expert Advisor bridge on the terminal; it has no "
                "Python API of its own. Use the broker's MT5 server if it "
                "offers one -- it is strictly better here, because MT5 at "
                "least reports order state coherently.")
        # On a host without the Windows-only package -- an Ubuntu server --
        # the terminal is reached through the bridge named in the environment.
        # An explicit `mt5_module` (tests, the probe) always wins.
        if "mt5_module" not in kwargs:
            from .mt5_bridge import bridge_from_env
            bridged = bridge_from_env()
            if bridged is not None:
                kwargs["mt5_module"] = bridged
        return MT5Broker(profile=profile, **kwargs)
    if adapter == "ccxt":
        from .ccxt_adapter import CCXTBroker
        return CCXTBroker(**kwargs)

    from .profiles import list_profiles
    raise ValueError(
        f"unknown broker {name!r}. Adapters: paper, oanda, mt5, ccxt. "
        f"Profiles: {', '.join(p.name for p in list_profiles())}. "
        "For an unlisted MetaTrader broker use 'generic_mt5', which declares "
        "nothing it cannot read from the terminal.")
