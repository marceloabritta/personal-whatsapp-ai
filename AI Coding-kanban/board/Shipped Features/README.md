# Shipped Features

Archive of the plan documents for features that have **already shipped to production**.
The counterpart to `New Features Plans/` (which holds only pending, not-yet-built work).

## Convention (when a feature ships)

Whenever a feature is shipped to production:

1. **Move** its plan `.md` from `New Features Plans/` into this folder
   (`Shipped Features/`), using `git mv` to preserve history.
2. **Rename** it to include the ship date, in the format
   **`YYYY-MM-DD - original-name.md`** (date first, then the original plan name).

Example: `feature-requests.md`, shipped on 2026-07-11, became
`2026-07-11 - feature-requests.md`.

This keeps `New Features Plans/` as a pure pending backlog, and makes this folder a
dated, chronological record of what has been delivered.
