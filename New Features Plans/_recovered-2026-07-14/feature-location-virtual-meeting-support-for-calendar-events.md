# Location & Virtual Meeting Support for Calendar Events

## Summary
Users can optionally attach a physical address, a resolved place name, or a virtual meeting link (via Google Meet) to calendar events, either at creation time or when editing an existing event.

## Problem / motivation
Today the assistant creates calendar events without any way to specify where they take place — no physical address, no video call link. Users end up having to open Google Calendar manually afterward to add this information, which defeats the purpose of managing everything through the assistant.

## User flow (from the user's point of view)
1. Marcelo creates a new event as usual, e.g. by chatting with the assistant on WhatsApp.
2. The assistant offers the option to add a location — a physical address, an informal place name/description, or "virtual."
3. If Marcelo gives a place name or description (e.g. "Santo Grão at Oscar Freire"), the assistant looks it up, resolves it to a real address, and confirms it with him before saving.
4. If the lookup returns multiple matches (e.g. several branches of the same place), the assistant lists them and asks Marcelo to pick the right one.
5. If Marcelo skips the location step, the event is created without one — it's not mandatory.
6. If Marcelo wants to add or change the location later, he refers to the event the same way he would for any other edit (by title, date, recency, etc.), using the assistant's existing event-matching logic.
7. If Marcelo marks the location as "virtual" on an event that already has a physical address, the Google Meet link replaces the address (and vice versa if he switches back to physical).
8. For virtual meetings, the assistant automatically generates a Google Meet link and attaches it to the calendar invite.
9. The assistant confirms the change, showing the resolved address or the Meet link in the summary.
10. If the location is added or changed after invites were already sent, guests are **not** re-notified by default — the assistant only sends an update notification if Marcelo explicitly asks for it.

## Actors
- Marcelo (user)
- Assistant (AI scheduling agent)
- Google Calendar
- Places/Maps lookup service

## Data & services touched
- Calendar event object: `location` field and conferencing/Google Meet field
- Places/Maps lookup service for resolving informal place names into real addresses

## Edge cases & open questions
- Switching a location from virtual to physical (removing the Meet link) or physical to virtual — handled by the same replace logic.
- Ambiguous or unresolvable location text (no matches, or multiple matches) — the assistant asks for clarification instead of guessing.
- A place name resolves to multiple locations (e.g. multiple branches of "Santo Grão") — assistant lists options for the user to choose from.
- **Open:** Should the event-creation confirmation always show a "location: none" placeholder to remind the user they can add one?
- **Open:** For virtual meetings, is Google Meet the only supported option, or should Zoom/Teams be supported eventually?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*