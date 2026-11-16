"""EvolutionHistoryMedia — the HistoryMedia seam (types.py) over Evolution + the transcription
service. STUB: built in the `agent` front."""
from __future__ import annotations


class EvolutionHistoryMedia:
    def __init__(self, evolution, transcription, settings) -> None:
        self.evolution = evolution
        self.transcription = transcription
        self.settings = settings
