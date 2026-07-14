"""Pipeline configuration: the user-defined column list for each pipeline.

Persisted to `data/pipelines.json`. Seeded on first run with a sensible default
flow, but every column can be added, renamed, reordered, gated or deleted from
the UI — the folder tree and the worker files follow.

This module is pure config: it never touches card folders. The Board owns the
on-disk migration that a column change implies.
"""
from __future__ import annotations

import json
import os
import tempfile

import re

from .models import (
    BUILD,
    DEFAULT_PIPELINE_COLORS,
    EXPED,
    MAINT,
    PLAN,
    PIPELINES,
    Column,
    slugify,
)

# `#rgb` or `#rrggbb`. Anything else is not a colour and does not go in the file — this
# value is interpolated straight into the page's CSS.
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def valid_color(value: str) -> str | None:
    v = (value or "").strip()
    return v if _HEX.match(v) else None

# ---------------------------------------------------------------------------
# The default flow. (title, slug, gate)
#
# These are DEFAULTS, not the system. The moment a working folder is seeded they become its
# state — rename them, gate them, reorder them, delete them. Nothing here is load-bearing.
# ---------------------------------------------------------------------------
DEFAULT_COLUMNS: dict[str, list[tuple[str, str, bool]]] = {
    PLAN: [
        ("Ideas", "ideas", False),
        ("Scoping", "scoping", False),
        ("Scope Review", "scope-review", False),
        ("Planning", "planning", False),
        ("Plan Review", "plan-review", False),
        ("Plan Ready", "plan-ready", True),  # GATE: human approves the hand-off to build
    ],
    # Maintenance answers a different question from Plan: not "what should this do?" but
    # "why is it not doing it?". Hence the shape — you may not diagnose a bug you have not
    # reproduced, and you may not fix one you have not diagnosed.
    MAINT: [
        ("Report", "report", False),
        ("Replication", "replication", False),  # reproduce it, or there is nothing to fix
        ("Exploring", "exploring", False),  # ROOT CAUSE, not a symptom
        ("Plan Fix", "plan-fix", False),
        ("Plan Ready to Build", "plan-ready-to-build", True),  # GATE: human approves the fix
    ],
    # The fast lane. One pipeline end to end — scope, plan, build, ship — for work that does
    # not need the full ceremony. It takes BOTH kinds: a small feature and a small fix.
    #
    # The two gates are the whole reason it is allowed to be fast. Speed is bought by cutting
    # STEPS, never by cutting the human out: you approve the plan before any code is written,
    # and you approve the build before anything is committed, pushed or deployed.
    EXPED: [
        ("Scope", "scope", False),
        ("Plan", "plan", True),  # GATE: the human approves the plan before a line is written
        ("Build", "build", True),  # GATE: the human approves the build before it ships
        ("Shipped", "shipped", False),
    ],
    BUILD: [
        ("Preflight", "preflight", False),
        ("Tests", "tests", False),
        ("Coding", "coding", False),
        ("Build Review", "build-review", True),  # GATE: human approves the ship
        ("Shipped", "shipped", False),
    ],
}


class PipelineConfig:
    """The ordered columns of both pipelines, persisted as JSON."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "pipelines.json")
        self.columns: dict[str, list[Column]] = {p: [] for p in PIPELINES}
        self.colors: dict[str, str] = dict(DEFAULT_PIPELINE_COLORS)
        os.makedirs(data_dir, exist_ok=True)
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        raw = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (json.JSONDecodeError, OSError):
                raw = None
        if not raw:
            self._seed()
            self.save()
            return
        for p in PIPELINES:
            self.columns[p] = [Column.from_dict(c) for c in raw.get(p, [])]
        # A colour the human has set wins; anything missing or malformed falls back to the
        # default rather than reaching the page as a broken CSS value.
        stored = raw.get("colors") or {}
        for p in PIPELINES:
            self.colors[p] = valid_color(stored.get(p, "")) or DEFAULT_PIPELINE_COLORS[p]
        # never allow a pipeline to end up with zero columns
        for p in PIPELINES:
            if not self.columns[p]:
                self.columns[p] = self._default_for(p)
                self.save()

    def _seed(self) -> None:
        for p in PIPELINES:
            self.columns[p] = self._default_for(p)
        self.colors = dict(DEFAULT_PIPELINE_COLORS)

    def set_color(self, pipeline: str, color: str) -> str | None:
        """Repaint a pipeline. Returns the colour, or None if it was not a colour."""
        if pipeline not in PIPELINES:
            return None
        c = valid_color(color)
        if not c:
            return None
        self.colors[pipeline] = c
        self.save()
        return c

    @staticmethod
    def _default_for(pipeline: str) -> list[Column]:
        return [
            Column.new(pipeline, title, slug=slug, gate=gate)
            for title, slug, gate in DEFAULT_COLUMNS[pipeline]
        ]

    def save(self) -> None:
        payload: dict = {p: [c.to_dict() for c in self.columns[p]] for p in PIPELINES}
        payload["colors"] = {p: self.colors[p] for p in PIPELINES}
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ---- lookups -----------------------------------------------------
    def all_columns(self) -> list[Column]:
        return [c for p in PIPELINES for c in self.columns[p]]

    def get(self, column_id: str) -> Column | None:
        for c in self.all_columns():
            if c.id == column_id:
                return c
        return None

    def by_slug(self, pipeline: str, slug: str) -> Column | None:
        for c in self.columns.get(pipeline, []):
            if c.slug == slug:
                return c
        return None

    def resolve(self, pipeline: str, ref: str) -> Column | None:
        """Resolve a column by id, slug, or (case-insensitive) title — for the manager's tools."""
        for c in self.columns.get(pipeline, []):
            if ref in (c.id, c.slug) or c.title.lower() == (ref or "").lower():
                return c
        return None

    def index_of(self, column_id: str) -> int:
        c = self.get(column_id)
        if not c:
            return -1
        return [x.id for x in self.columns[c.pipeline]].index(column_id)

    def first(self, pipeline: str) -> Column:
        return self.columns[pipeline][0]

    def last(self, pipeline: str) -> Column:
        return self.columns[pipeline][-1]

    def next_column(self, column_id: str) -> Column | None:
        """The next column in flow order. Crossing plan -> build is NOT implicit; the
        end of the plan pipeline returns None (the manager must promote explicitly)."""
        c = self.get(column_id)
        if not c:
            return None
        cols = self.columns[c.pipeline]
        i = self.index_of(column_id)
        if i < 0 or i + 1 >= len(cols):
            return None
        return cols[i + 1]

    def unique_slug(self, pipeline: str, title: str, exclude_id: str | None = None) -> str:
        base = slugify(title)
        taken = {c.slug for c in self.columns[pipeline] if c.id != exclude_id}
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    # ---- mutations (config only; Board handles the disk side) ---------
    def add_column(self, pipeline: str, title: str, index: int | None = None, gate: bool = False) -> Column:
        if pipeline not in PIPELINES:
            raise ValueError(f"unknown pipeline: {pipeline}")
        title = (title or "").strip() or "Untitled"
        col = Column.new(pipeline, title, slug=self.unique_slug(pipeline, title), gate=gate)
        cols = self.columns[pipeline]
        if index is None or index < 0 or index > len(cols):
            index = len(cols)
        cols.insert(index, col)
        self.save()
        return col

    def rename_column(self, column_id: str, title: str) -> tuple[Column, str] | None:
        """Returns (column, old_slug). The slug follows the title."""
        col = self.get(column_id)
        if not col:
            return None
        title = (title or "").strip()
        if not title:
            return None
        old_slug = col.slug
        col.title = title
        col.slug = self.unique_slug(col.pipeline, title, exclude_id=col.id)
        self.save()
        return col, old_slug

    def set_gate(self, column_id: str, gate: bool) -> Column | None:
        col = self.get(column_id)
        if not col:
            return None
        col.gate = bool(gate)
        self.save()
        return col

    def reorder_column(self, column_id: str, index: int) -> Column | None:
        col = self.get(column_id)
        if not col:
            return None
        cols = self.columns[col.pipeline]
        cols.remove(col)
        index = max(0, min(index, len(cols)))
        cols.insert(index, col)
        self.save()
        return col

    def remove_column(self, column_id: str) -> Column | None:
        """Drops the column from the config. Refuses to empty a pipeline."""
        col = self.get(column_id)
        if not col:
            return None
        cols = self.columns[col.pipeline]
        if len(cols) <= 1:
            raise ValueError("a pipeline must keep at least one column")
        cols.remove(col)
        self.save()
        return col

    # ---- views -------------------------------------------------------
    def snapshot(self) -> list[dict]:
        from .models import PIPELINE_TITLES

        return [
            {
                "id": p,
                "title": PIPELINE_TITLES[p],
                "color": self.colors[p],
                "columns": [c.to_dict() for c in self.columns[p]],
            }
            for p in PIPELINES
        ]
