"""Reusable typed columns (SQLAlchemy 2.0 typed style, design.md §2).

All timestamps are `timezone=True` (`timestamptz`) — the app is UTC end-to-end.
"""

import datetime as dt
import uuid
from typing import Annotated

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import mapped_column

UUIDpk = Annotated[
    uuid.UUID,
    mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
]

TZDateTime = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]

CreatedAt = Annotated[
    dt.datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now()),
]

JSONBDict = Annotated[dict, mapped_column(JSONB)]
