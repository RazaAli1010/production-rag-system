"""Import every model so `Base.metadata` (and Alembic autogenerate) sees the full schema.

T-10: this module is the single place that must import all 12 tables. Nothing else needs to
import individual model modules directly for metadata purposes.
"""

from app.db.models.auth import LoginAttempt, RefreshToken
from app.db.models.chat import Message, Session
from app.db.models.corpus import Chunk, Document
from app.db.models.evals import EvalResult, EvalRun
from app.db.models.ops import CacheEntry, RequestLog
from app.db.models.user import ApiKey, User

__all__ = [
    "User",
    "ApiKey",
    "RefreshToken",
    "LoginAttempt",
    "Document",
    "Chunk",
    "Session",
    "Message",
    "RequestLog",
    "CacheEntry",
    "EvalRun",
    "EvalResult",
]
