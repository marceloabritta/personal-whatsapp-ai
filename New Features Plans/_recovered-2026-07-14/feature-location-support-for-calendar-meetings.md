# Location Support for Calendar Meetings

## Summary
Users can add an optional physical or virtual location to any calendar event, with the assistant validating addresses and auto-generating Google Meet links for virtual meetings.

## Problem / motivation
Calendar events currently have no structured way to specify a location — whether a physical address or a virtual meeting link — so this information ends up living outside the event or being handled manually, creating friction and inconsistency.

## User flow (from the user's point of view)
1. Marcelo creates a calendar event without specifying a location, since it's optional.
2. At any point — during creation or later — Marcelo can add or edit the location, referring to the event by title, date, or as "the last one created."
3. Marcelo describes a place in natural language (e.g., "Santo Grão at Oscar Freire"), and the assistant looks up and resolves the full address.
4. If the lookup returns multiple possible matches, the assistant lists them so Marcelo can pick the right one — it never guesses.
5. The assistant confirms the resolved address with Marcelo before saving it to the event.
6. Alternatively, Marcelo can say the meeting is virtual, and the assistant automatically adds a Google Meet link to the invite.
7. If Marcelo changes an event with a physical address to virtual, the Meet link replaces the address rather than both being shown.
8. If Marcelo changes the location after invites have already gone out, guests are not automatically re-notified unless he explicitly asks for that.

## Actors
- Marcelo (user)
- Assistant

## Data & services touched
- Calendar event object (location field: physical address or virtual/Meet link)
- Guest invite records
- Address lookup/validation service
- Google Meet (link generation)

## Edge cases & open questions
- Address text is incomplete or unclear — assistant validates/looks it up rather than saving raw text as-is.
- Multiple location matches found during lookup — assistant presents options instead of guessing.
- Switching from a physical address to a virtual meeting replaces the address instead of displaying both.
- Location added or changed after invites are already sent — no automatic guest re-notification unless explicitly requested.
- **Open:** Should new-event confirmation messages always display a "location: none" placeholder, or only mention location when the user brings it up?
- **Open:** Is Google Meet sufficient for now, or should Zoom/Teams support be added later?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*