"""VoiceCapture — transcribes every voice note in every chat. STUB: built in the `voice` front."""
from __future__ import annotations


class VoiceCapture:
    def __init__(self, dsn: str, transcription, settings) -> None:
        self.dsn = dsn
        self.transcription = transcription
        self.settings = settings

    async def open(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
