"""Optional language-model assistance: news extraction, trade reviews, briefs.

Nothing in this package can open, enlarge or keep open a position. See
``providers.py`` for the security model and ``service.py`` for budgets.
"""

from .providers import CATALOG, AIResult, ProviderClient, ProviderConfig, ProviderError
from .service import PURPOSES, AIService, AISettings, AIStore

__all__ = ["CATALOG", "AIResult", "AIService", "AISettings", "AIStore", "PURPOSES",
           "ProviderClient", "ProviderConfig", "ProviderError"]
