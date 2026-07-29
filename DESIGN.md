---
name: manager-kanban
description: A dense, dark, terminal-adjacent control surface for an agent-orchestration kanban board.
mode: operate
colors:
  bg: "#0f1115"
  panel: "#171a21"
  panel-2: "#1e222b"
  panel-3: "#252a35"
  line: "#2a2f3a"
  text: "#e6e9ef"
  muted: "#8b93a3"
  accent: "#5b8cff"          # identity blue — safe as TEXT/borders on dark
  accent-solid: "#4069d8"    # darker blue for SOLID button fills under white text (~5:1)
  accent-2: "#7c5bff"
  ok: "#3fb950"
  warn: "#d29922"
  gate: "#f0883e"            # orange — a card waiting on the human at a gate
  danger: "#f85149"
  maint: "#e0a03a"           # amber — a maintenance/fix card, kept far from the blues
  maint-bg: "#2a2318"
  maint-line: "#5c4620"
  card-open: "#2d3d63"       # the card whose chat is open (paired with card-open-seen)
  untyped: "#3a4050"         # a backlog card nobody has typed yet
typography:
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
    fontSize: "14px"
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, monospace"
  scale:
    label: "11px"    # uppercase tracked micro-labels, meta, timestamps
    small: "12px"    # secondary body / descriptions
    body: "12.5px"   # chips, toasts, controls
    heading: "15px"  # app title (h1) and dialog titles (h2)
  weights: [400, 500, 600, 650]
rounded:
  sm: "6px"    # badges, code
  md: "8px"    # buttons, icon buttons
  card: "7px"
  lg: "14px"   # pipelines, modals
  pill: "20px" # chips, mode/repo tags
spacing:
  scale: ["2px", "4px", "6px", "8px", "10px", "12px", "14px", "16px", "18px", "24px"]
---

# manager-kanban — design language

The board is an **Operate** surface: a human supervises many AI agents through a kanban, and
success is reading state and acting fast. Density, scanability, and colour-as-meaning outrank
expression. Brand lives in precise, consistent details, not decoration.

## Color

A near-black slate (`bg` → `panel-3`) stack builds depth by surface, not by shadow. One family
of hairline borders (`line`) defines every edge. Colour is **reserved for meaning**, never
mood:

- **`accent` (blue)** — features, selection, links, focus. Use it as text or a border on dark.
  For a **solid fill under white text**, use **`accent-solid`** — the identity blue is only
  3.2:1 filled, `accent-solid` is ~5:1.
- **`maint` (amber)** — maintenance/fix cards, deliberately far from every blue so a fix and a
  feature read apart at a glance, even side by side in Build.
- **`gate` / `danger` (orange / red)** — a card waiting on the human; the corner gate-dot.
- **`ok` / `warn`** — connection and run state.
- **`untyped` (grey)** — the backlog is not a pipeline, so it is deliberately colourless.

Secondary text on a coloured surface tints from that hue; it is never flattened to grey.

## Typography

One system sans (`body`) carries the whole UI; hierarchy comes from **weight (400→650) and
size**, not from a second display face. `mono` is used only for genuine machine text — file
paths, ids, code, worker output. The floor is **11px** for any functional text and **12px**
for body copy; nothing legible sits below it.

Short structural labels — pipeline names, column titles, section headers — are **uppercase
with 0.6–1px tracking**. This is the board's chosen label grammar, not an eyebrow habit; it is
what makes the dense grid scannable. Reserve it for short labels; sentence-case anything long.

## Elevation & motion

Panels commit to a **defined edge** (1px `line` border) with only a **tight, low-blur drop
shadow** for lift — never a hairline border paired with a wide diffuse glow, and never a
zero-offset coloured halo. Motion is scarce and honest: a status indicator pulses **only while
its data is genuinely live** (e.g. a run winding down); nothing pulses for decoration.

## Conventions to preserve

- The `--card-open` / `--card-open-seen` pair is computed (seen = 0.6 × raw under the scrim);
  keep them in sync (`scripts/card-highlight-selftest.mjs` enforces it).
- Maintenance amber and gate orange are semantic — never repaint them to match a pipeline.
- The board never scrolls sideways; it is a thing you look at, not drive.
