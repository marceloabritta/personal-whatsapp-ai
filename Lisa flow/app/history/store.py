"""PgHistoryStore — the HistoryStore (types.py) over the lisa_search index. STUB: built in the
`store` front."""
from __future__ import annotations


class PgHistoryStore:
    def __init__(self, dsn: str, *, schema: str = "lisa_search") -> None:
        self.dsn = dsn
        self.schema = schema

    async def open(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
