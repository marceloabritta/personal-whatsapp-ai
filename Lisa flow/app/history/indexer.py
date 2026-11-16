"""HistoryIndexer — keeps the lisa_search index in step with Evolution's "Message" table. STUB:
built in the `store` front."""
from __future__ import annotations


class HistoryIndexer:
    def __init__(self, dsn: str, *, schema: str = "lisa_search", settings=None) -> None:
        self.dsn = dsn
        self.schema = schema
        self.settings = settings

    async def open(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
