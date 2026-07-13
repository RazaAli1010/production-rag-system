"""F12 persistence layer.

Re-exports the pieces other features import: `Base` (all 12 models registered via
`app.db.models`), and the `get_session` FastAPI dependency.
"""

from app.db.base import Base
from app.db.session import get_session

__all__ = ["Base", "get_session"]
