---
title: Suporte a local (endereço ou virtual) nos eventos de calendário
one_liner: Permitir que eventos criados pelo assistente tenham endereço físico ou sejam marcados como virtuais, gerando automaticamente um link do Google Meet nesse caso
when: 2026-07-16 08:23:44 (America/Sao_Paulo)
---
# Support for Location (Address or Virtual) in Calendar Events

## Summary
Calendar events created through the assistant can now include a physical address or be marked as virtual, with a Google Meet link generated automatically in the latter case.

## Problem / motivation
Today the assistant has no way to record where an event takes place — neither a physical address nor a virtual meeting option — so this information gets lost or is missing from the invite, forcing users to add it manually afterward.

## User flow (from the user's point of view)
1. Marcelo asks the assistant to create or update an event via WhatsApp, mentioning a physical address (e.g., "Faria Lima 201") or stating that it's virtual.
2. The assistant determines whether the event is in-person (with an address), virtual, or has no location indication at all.
3. If in-person: the assistant includes the address in the event's location field.
4. If virtual (or if the assistant is confident it should be digital): the assistant automatically generates a Google Meet link and adds it to the invite, without asking for confirmation first.
5. If there's no indication of location and no certainty it's virtual: the location field is left blank, and the assistant does not ask the user about it.
6. The assistant confirms the updated event with Marcelo before sending it to guests.

## Actors
- Marcelo (user)
- AI Assistant
- Event guests

## Data & services touched
- Calendar event (location/address field, video call link)
- Google Meet (link generation)

## Edge cases & open questions
- User switches an existing event from in-person to virtual, or vice versa.
- Address mentioned ambiguously or incompletely.
- Event with no location indication at all: field stays blank, assistant doesn't ask.
- Assistant is unsure whether the event is virtual: field stays blank, no Meet link is created as a precaution.

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*