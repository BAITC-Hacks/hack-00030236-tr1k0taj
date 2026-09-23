"""Starter-kit knowledge module: loader, catalog, mock-backend records, search.

Public API for the agent kernel — import only from here:

    from app.knowledge import Knowledge, open_knowledge

    kb = Knowledge(session)
    await kb.catalog.scenarios()                    # all 40, never narrowed
    await kb.records.find_client(phone="+77010000004")
    await kb.search.kb_lookup("claims.submission")  # -> Fact(source="kb_lookup", source_id=...)

See docs/specs/knowledge-module.md.
"""

from app.knowledge.api import router as api_router
from app.knowledge.catalog import Catalog
from app.knowledge.loader import build_records, ensure_loaded, load_kit
from app.knowledge.rag import (
    RAG_KINDS,
    reindex_knowledge,
    schedule_reindex_knowledge,
    stop_reindex_knowledge,
)
from app.knowledge.records import Records, normalize_phone
from app.knowledge.search import Search
from app.knowledge.service import Knowledge, open_knowledge
from app.knowledge.types import (
    Action,
    Claim,
    Client,
    Fact,
    Hit,
    Payment,
    Policy,
    Scenario,
    Slot,
    SystemIntent,
)

__all__ = [
    "RAG_KINDS",
    "Action",
    "Catalog",
    "Claim",
    "Client",
    "Fact",
    "Hit",
    "Knowledge",
    "Payment",
    "Policy",
    "Records",
    "Scenario",
    "Search",
    "Slot",
    "SystemIntent",
    "api_router",
    "build_records",
    "ensure_loaded",
    "load_kit",
    "normalize_phone",
    "open_knowledge",
    "reindex_knowledge",
    "schedule_reindex_knowledge",
    "stop_reindex_knowledge",
]
