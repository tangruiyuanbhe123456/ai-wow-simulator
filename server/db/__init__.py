"""server.db package — SQLite persistence."""
from .schema import SCHEMA_SQL, ensure_schema
from .store import (
    Store,
    connect,
    init_schema,
    row_to_dict,
    rows_to_dicts,
    jdump,
    jload,
)

__all__ = [
    "SCHEMA_SQL", "ensure_schema", "Store",
    "connect", "init_schema", "row_to_dict", "rows_to_dicts",
    "jdump", "jload",
]
