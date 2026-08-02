# Flight search via chat with AI Brain

## Summary
The user asks to search for flights inside the conversation and AI Brain queries Skyscanner, returning the 3 best options in a standard format.

## Problem / motivation
Today the user has to leave the chat and search for flights manually. They want to delegate that to AI Brain directly in the conversation.

## User flow (from the user's point of view)
1. The user asks for a flight search, providing origin, destination, and departure date (return date is optional).
2. AI Brain assembles a summary of the parameters (origin/destination/date/passengers/class) and asks for confirmation before searching, expecting an explicit "yes".
3. The user confirms (or asks to change class, number of passengers, or time, departing from the defaults).
4. If the user doesn't respond to the requested data or to the confirmation, there is a timeout and the flow simply dies, with no additional action.
5. AI Brain searches for flights via the Skyscanner API.
6. AI Brain returns the 3 best options in the chat, in the format: "encontrei as seguintes opções: Aerolinha / ida: data - horário / valor: $ / volta: data - horário / valor: $", prioritizing direct + cheapest + night flights.

## Actors
- User
- AI Brain

## Data & services touched
Search parameters (origin, destination, dates, passengers, class) and results returned by Skyscanner (airline, departure/return dates and times, prices).

## Edge cases & open questions
- Default is 1 passenger, economy class, if the user doesn't specify.
- Priority: direct + cheapest + night flight; if no direct/night flight is found, the tie-breaker is price.
- The user can explicitly request another class, more passengers, or a different time than the default.
- The flow only searches and shows options — it does not make a purchase.
- On timeout (no response to the requested data or to the confirmation), AI Brain does nothing and lets the flow die.

---
*Drafted by @brain on WhatsApp. Save to the repo and refine.*
