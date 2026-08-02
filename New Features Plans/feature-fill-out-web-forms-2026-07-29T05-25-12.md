---
title: Fill out web forms
one_liner: The assistant should be able to fill out forms (e.g. web forms shared by the owner) on his behalf
when: 2026-07-29 05:25:12 (America/Sao_Paulo)
---
# Fill out web forms

## Summary
The assistant should be able to fill out forms — such as web forms, PDFs, or shared documents — on the owner's behalf.

## Problem / motivation
Marcelo asked the assistant to fill out a form, but it currently has no way to do so. This is a capability gap similar to browsing the web or reading files, and it limits how useful the assistant can be for everyday administrative tasks.

## User flow (from the user's point of view)
1. Marcelo sends the assistant a form — as a link, PDF, or attached document — and asks it to be filled out.
2. The assistant reviews the form and identifies what fields need to be completed.
3. If some required information is missing, the assistant asks Marcelo for it; otherwise, it infers the answers from known context or data (e.g. contacts, calendar, tasks).
4. The assistant fills out the form and returns the completed version to Marcelo, or submits it directly if that's appropriate.

## Actors
- Marcelo (owner)
- Assistant

## Data & services touched
- Form documents and links (PDF, web forms, Google Forms, etc.)
- Owner's personal data used to auto-fill fields (contacts, calendar, tasks, and other known context)

## Edge cases & open questions
- Form requires information the assistant doesn't have and Marcelo hasn't provided.
- Form is a scanned image and requires OCR to read.
- Form requires a signature.
- Form submission requires authentication or login credentials.
- **Open:** Should the assistant submit the form automatically, or always return it to Marcelo for review first?
- **Open:** What form types are in scope — PDF, web forms, Google Forms, others?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*