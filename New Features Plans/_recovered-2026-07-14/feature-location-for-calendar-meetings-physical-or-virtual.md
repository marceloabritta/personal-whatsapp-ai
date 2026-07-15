# Location for Calendar Meetings (Physical or Virtual)

## Summary
Users can optionally attach a physical address or a virtual meeting link to any calendar event, at creation or later, with the assistant automatically resolving addresses and generating Google Meet links as needed.

## Problem / motivation
Calendar events currently have no structured way to specify where a meeting happens. There's no support for distinguishing physical vs. virtual locations, and free-text addresses aren't validated, which can lead to vague or incorrect location info on invites.

## User flow (from the user's point of view)
1. User creates or edits a calendar event, optionally specifying a location — location is never mandatory.
2. User can add or change the location at creation time or at any point afterward.
3. If the user says the meeting is "virtual," the assistant automatically attaches a Google Meet link to the invite instead of an address.
4. If an event already has a physical address and the user changes it to virtual, the Meet link replaces the address (the event doesn't keep both).
5. User can describe a location informally (e.g., "Santo Grão at Oscar Freire"), and the assistant looks up and resolves it to an actual address.
6. Assistant confirms the resolved address with the user before saving it to the event.
7. If the lookup returns multiple possible matches, the assistant lists them and asks the user to pick the right one instead of guessing.
8. When editing an existing event, the assistant identifies which event to update using the standard event-matching approach (by title, date, "last created," etc.).
9. If the location is added or changed after invites have already gone out, guests are not notified automatically — the assistant only sends an update if the user explicitly asks.

## Actors
- Marcelo (user)
- Calendar assistant

## Data & services touched
- Calendar event object: location field, virtual/physical flag, Google Meet link
- Address lookup/validation service
- Guest invite notification system

## Edge cases & open questions
- Virtual location added to an event that already has a physical address → Meet link replaces the address.
- Ambiguous or incomplete address text → assistant validates and confirms with the user before saving.
- Multiple address matches found → assistant lists options for the user to choose from.
- Location changed after invites were sent → no automatic guest notification unless explicitly requested.
- **Open:** Should new-event confirmations always display a "location: none" placeholder, or only mention location when the user brings it up?
- **Open:** Is Google Meet sufficient for now, or should Zoom/Teams support be added eventually?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*