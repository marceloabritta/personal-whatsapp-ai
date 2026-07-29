---
title: Google Contacts CRUD Integration
one_liner: Assistant reads and writes Google Contacts to find and save contact emails linked to WhatsApp phone numbers.
when: 2026-07-28 18:48:26 (America/Sao_Paulo)
---
# Google Contacts CRUD Integration

## Summary
The assistant automatically looks up and saves contact emails in Google Contacts, matching them to the phone numbers it sees in WhatsApp conversations.

## Problem / motivation
The assistant often needs a contact's email — for example, to send a calendar invite — but only has their WhatsApp phone number, with no way to look it up or remember it for next time. This integration lets the assistant find known emails automatically and capture new ones as they come up in conversation, so Marcelo doesn't have to manually manage contact info.

## User flow (from the user's point of view)
1. During normal use (e.g., the assistant is helping schedule a meeting), the orchestrator realizes it needs a contact's email but only has their phone number.
2. The assistant searches Google Contacts for a contact matching that phone number.
3. If exactly one email is found, the assistant uses it automatically — no interruption to Marcelo.
4. If multiple emails are found for that contact, the assistant asks Marcelo which one to use.
5. If no matching contact exists, the assistant proceeds without an email for that step.
6. Separately, whenever a new email appears in a WhatsApp conversation with someone, the assistant saves it to that person's Google Contacts entry, matched by phone number.
7. If no contact exists yet for that phone number, the assistant creates a new one.
8. If the contact already has a different email on file, the new one is added as a second email rather than overwriting the existing one.
9. Marcelo receives a direct message from the assistant (to himself, not in the original chat) notifying him that a new email was saved.

## Actors
- Marcelo (owner)
- Assistant/orchestrator
- Google Contacts
- WhatsApp contacts (other people in conversations)

## Data & services touched
- Google Contacts (create, read, update)
- WhatsApp conversation content (phone numbers, email addresses mentioned)
- `calendar_action` (consumes looked-up emails, e.g., for invites)

## Edge cases & open questions
- No contact found for a phone number — a new contact is created automatically.
- Contact has multiple saved emails — assistant asks Marcelo which one to use.
- Contact already has a different email on file — new email is added as a second entry, not a replacement.
- Email appears in a group conversation — must be linked to the correct individual's phone number, not misattributed.
- **Open:** What Google account/credentials should be used to access Google Contacts?
- **Open:** How should the DM to Marcelo about a newly saved email be formatted?
- **Open:** Should there be a limit on how many emails can accumulate per contact?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*