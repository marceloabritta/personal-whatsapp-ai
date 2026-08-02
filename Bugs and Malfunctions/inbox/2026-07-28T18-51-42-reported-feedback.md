# Failure report — reported / feedback  (2026-07-28 18:51:42)

| Field       | Value |
|-------------|-------|
| When        | 2026-07-28 18:51:42 (America/Sao_Paulo) |
| Chat        | 5511976001033@s.whatsapp.net (?) |
| Trigger     | reported |
| Source      | **OWNER-REPORTED** (human-verified) |
| Failed task | feedback |
| Status      | needs-plan |

## What the user asked
anote erro: eu comecei o chat em ingles e vc migrou pra português.

vc deve sempre falar na língua que a conversa começa

_(On an owner-reported failure this is the NOTE itself, not the order that misbehaved — that one is in the transcript.)_

## What happened
The secretary replied in Portuguese even though the conversation was originally started in English, switching languages mid-conversation

```
n/a
```

## Owner's report
**What the owner says went wrong:** The secretary replied in Portuguese even though the conversation was originally started in English, switching languages mid-conversation
**What they expected instead:** The secretary should always reply in the language the conversation started in, and never switch languages mid-conversation
**His exact words:** The owner started this chat in English but the secretary switched to responding in Portuguese partway through the conversation

### The offending message (quoted)
_Not quoted — the owner reported this without replying to a specific message. The evidence is in the transcript and the logs below._

## Auto-analysis (best-effort, UNVERIFIED — a cheap model's guess, not evidence)
LIKELY CAUSE: LLM router's language detection logic is not persisting the initial conversation language across turns; it infers language per-turn from the most recent message(s) rather than maintaining session-level language state, causing it to detect Portuguese in the mid-conversation transcript and switch the router's "lang" parameter accordingly.

SUSPECTED FILE/AREA: Router's language inference/detection (likely in LLM prompt or preprocessing step that determines the "lang" field sent to skills) and session state management in Redis—check whether conversation language is stored as a session attribute that the router reads before each LLM call, or if the router recalculates it fresh each turn from recent transcript.

## Recent logs
```
2026-07-28T21:21:19.814Z [log] settings: Redis connected
2026-07-28T21:21:19.817Z [log] sessions: Redis connected
2026-07-28T21:21:23.314Z [log] skill loaded: "1. Calendar Actions" -> calendar_action
2026-07-28T21:21:23.319Z [log] skill loaded: "2. Audio transcriptions" -> transcribe_audio
2026-07-28T21:21:23.327Z [log] skill loaded: "3. Tasks" -> task_action
2026-07-28T21:21:23.332Z [log] skill loaded: "4. Feature Requests" -> feature_request
2026-07-28T21:21:23.337Z [log] skill loaded: "5. Feedback" -> feedback
2026-07-28T21:21:23.342Z [log] skill loaded: "6. Flight Search" -> flight_search
2026-07-28T21:21:23.346Z [log] skill loaded: "7. Assistant Settings" -> assistant_settings
2026-07-28T21:21:23.347Z [log] mary skills: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings
2026-07-28T21:21:23.361Z [log] new-tags: @mary (source: SECRETARY_TAG_NEW seed)
2026-07-28T21:21:23.371Z [log] Secretary v2.0 (orchestrator) listening on port 3000
2026-07-28T21:45:29.689Z [log] TRANSCRIPT>>>
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- _Gesso & Costa Flores_
- _(ninguém convidado)_
- _22 de jul. de 2026, 10:00 (45 min)_
- _📍 minha casa_

_Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto._
ME: sim
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Gesso & Costa Flores_
- _(ninguém convidado)_
- _22 de jul. de 2026, 10:00 (45 min)_
- _📍 minha casa_

_Aqui está o link do evento:_
https://www.google.com/calendar/event?eid=NGhyOTFpMmdycjcwMzVkM2FmN2RncmJoazQgbWFyY2Vsb2Ficml0dGFAbQ
ME: @mary qual o cnpj do emissor da nota fiscal
ME: *[Assistente IA do Marcelo]:*

_Não tenho acesso a notas fiscais ou dados de CNPJ aqui na conversa — não é algo que eu consiga consultar. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: esse pdf
ME: @mary qual o cnpj do pdf anexo?
ME: *[Assistente IA do Marcelo]:*

_Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: @assistente qual o cnpj do pdf acima?
ME: *[Assistente IA do Marcelo]:*

Não entendi o que você quer que eu faça. Habilidades disponíveis: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings.
ME: altura da estrutura lateral do muxarabi da frente 2.565mt
ME: da esquerda
ME: da direita
ME: @assistente agendar hoke 13:45 levar range rocer no glauco, 45 minutos so eu
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto._
ME: s
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Aqui está o link do evento:_
https://www.google.com/calendar/… «truncated 3129 chars»
2026-07-28T21:45:36.341Z [log] ROUTER RAW: {"say": "Anotando essa feature. Pra fechar a spec:\n\n- Trigger: essa ação de contatos roda automaticamente sempre que eu preciso de um e-mail (ex: pra convidar alguém numa reunião) e só tenho o telefone — é isso, ou você também quer poder pedir manualmente tipo 'busca o e-mail desse contato'?\n- Quando eu salvo um e-mail novo que apareceu na conversa do WhatsApp, devo sempre perguntar sua confirmação antes de gravar no Google Contacts, ou pode salvar direto?\n- Edge case: e se o número não tiver nenhum contato correspondente no Google Contacts — eu crio um contato novo ou só aviso que não achei?\n- Edge case: e se o contato já tiver um e-mail salvo diferente do que apareceu agora — eu substituo, pergunto, ou guardo como um segundo e-mail?", "next": "listen", "skills": [], "info": {}, "lang": "pt", "awaitFrom": "owner"}
2026-07-28T21:45:36.342Z [log] TURN -> {"next":"listen","skills":[],"hasSay":true}
2026-07-28T21:48:08.810Z [log] TRANSCRIPT>>>
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Gesso & Costa Flores_
- _(ninguém convidado)_
- _22 de jul. de 2026, 10:00 (45 min)_
- _📍 minha casa_

_Aqui está o link do evento:_
https://www.google.com/calendar/event?eid=NGhyOTFpMmdycjcwMzVkM2FmN2RncmJoazQgbWFyY2Vsb2Ficml0dGFAbQ
ME: @mary qual o cnpj do emissor da nota fiscal
ME: *[Assistente IA do Marcelo]:*

_Não tenho acesso a notas fiscais ou dados de CNPJ aqui na conversa — não é algo que eu consiga consultar. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: esse pdf
ME: @mary qual o cnpj do pdf anexo?
ME: *[Assistente IA do Marcelo]:*

_Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: @assistente qual o cnpj do pdf acima?
ME: *[Assistente IA do Marcelo]:*

Não entendi o que você quer que eu faça. Habilidades disponíveis: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings.
ME: altura da estrutura lateral do muxarabi da frente 2.565mt
ME: da esquerda
ME: da direita
ME: @assistente agendar hoke 13:45 levar range rocer no glauco, 45 minutos so eu
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto._
ME: s
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Aqui está o link do evento:_
https://www.google.com/calendar/event?eid=MmQ5aWlyYzI4MXF0a3RiNzN0cTFpaHJkdWcgbWFyY2Vsb2Ficml0dGFAbQ
ME: @assistante book 2 tasks for me for tomorrow. One: ask laura about Mr. White sale. Second: ask Laura about table removal
ME: @assistente book 2 tasks for me for tomorrow. One: ask laura about Mr.… «truncated 3988 chars»
2026-07-28T21:48:18.083Z [log] ROUTER RAW: {"say": null, "next": "execute", "skills": ["feature_request"], "info": {"title": "Google Contacts CRUD Integration", "one_liner": "Assistant reads and writes Google Contacts to find and save contact emails linked to WhatsApp phone numbers.", "problem": "The assistant often needs a contact's email (e.g., to send a calendar invite) but only has their WhatsApp phone number, and currently cannot look it up or save it for future use.", "trigger": "Runs automatically whenever the orchestrator determines it's needed (not a manual user command).", "actors": ["Marcelo (owner)", "Assistant/orchestrator", "Google Contacts", "WhatsApp contacts (other people in conversations)"], "steps": [
  "Orchestrator determines it needs a contact's email (e.g., to invite someone to a meeting) and only has their phone number.",
  "Assistant searches Google Contacts by phone number.",
  "If a matching contact is found with exactly one email, use it.",
  "If the matching contact has two or more emails saved, the assistant asks Marcelo which one to use.",
  "If no matching contact is found, the assistant proceeds without an email for that step.",
  "Separately, when a new email appears in a WhatsApp conversation with a contact, the assistant saves it to that contact's entry in Google Contacts, linked by phone number.",
  "If no contact exists for that phone number, the assistant creates a new contact.",
  "If the contact already has a different email saved, the new one is stored as a second email (not replacing the first).",
  "The assistant notifies Marcelo about the save action via a direct message to himself (not inline in the conversation where the email appeared)."
], "data_touched": "Google Contacts (CRUD), WhatsApp conversation content (phone numbers, emails mentioned), calendar_action (consumer of looked-up emails)", "edge_cases": [
  "No contact found for the phone number — a new contact is created.",
  "Contact has multiple emails saved — assistant asks Marcelo which one … «truncated 483 chars»
2026-07-28T21:48:18.084Z [log] TURN -> {"next":"execute","skills":["feature_request"],"hasSay":false}
2026-07-28T21:48:26.136Z [log] FEATURE DOC LEN: 2765
2026-07-28T21:48:28.946Z [log] ROUTER RAW: {"say":"_Spec salva! Feature 'Google Contacts CRUD Integration' documentada com trigger, fluxo, edge cases e as respostas que você deu._","next":"listen","skills":[],"info":{},"lang":"pt","awaitFrom":"owner"}
2026-07-28T21:48:28.947Z [log] TURN -> {"next":"listen","skills":[],"hasSay":true}
2026-07-28T21:51:39.535Z [log] TRANSCRIPT>>>
ME: *[Assistente IA do Marcelo]:*

_Não tenho acesso a notas fiscais ou dados de CNPJ aqui na conversa — não é algo que eu consiga consultar. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: esse pdf
ME: @mary qual o cnpj do pdf anexo?
ME: *[Assistente IA do Marcelo]:*

_Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: @assistente qual o cnpj do pdf acima?
ME: *[Assistente IA do Marcelo]:*

Não entendi o que você quer que eu faça. Habilidades disponíveis: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings.
ME: altura da estrutura lateral do muxarabi da frente 2.565mt
ME: da esquerda
ME: da direita
ME: @assistente agendar hoke 13:45 levar range rocer no glauco, 45 minutos so eu
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto._
ME: s
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Aqui está o link do evento:_
https://www.google.com/calendar/event?eid=MmQ5aWlyYzI4MXF0a3RiNzN0cTFpaHJkdWcgbWFyY2Vsb2Ficml0dGFAbQ
ME: @assistante book 2 tasks for me for tomorrow. One: ask laura about Mr. White sale. Second: ask Laura about table removal
ME: @assistente book 2 tasks for me for tomorrow. One: ask laura about Mr. White sale. Second: ask Laura about table removal
ME: *[Marcelo's AI Assistant]:*

_Added to your list:_
_24/jul - ask laura about Mr. White sale_
_24/jul - ask Laura about table removal_

_Tell me if you need to change anything, otherwise we're good._
ME: @mary I have a pdf form I have to fill. I will send it and you should read it and tell me what info … «truncated 3925 chars»
2026-07-28T21:51:42.719Z [log] ROUTER RAW: {"say": null, "next": "execute", "skills": ["feedback"], "info": {"note": "The owner started this chat in English but the secretary switched to responding in Portuguese partway through the conversation", "what_went_wrong": "The secretary replied in Portuguese even though the conversation was originally started in English, switching languages mid-conversation", "expected": "The secretary should always reply in the language the conversation started in, and never switch languages mid-conversation", "suspected_skill": null}, "lang": "en", "awaitFrom": "owner"}
2026-07-28T21:51:42.720Z [log] TURN -> {"next":"execute","skills":["feedback"],"hasSay":false}
```

## Conversation transcript
```
ME: *[Assistente IA do Marcelo]:*

_Não tenho acesso a notas fiscais ou dados de CNPJ aqui na conversa — não é algo que eu consiga consultar. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: esse pdf
ME: @mary qual o cnpj do pdf anexo?
ME: *[Assistente IA do Marcelo]:*

_Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Posso ajudar com agenda, tarefas, voos ou outra coisa?_
ME: @assistente qual o cnpj do pdf acima?
ME: *[Assistente IA do Marcelo]:*

Não entendi o que você quer que eu faça. Habilidades disponíveis: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings.
ME: altura da estrutura lateral do muxarabi da frente 2.565mt
ME: da esquerda
ME: da direita
ME: @assistente agendar hoke 13:45 levar range rocer no glauco, 45 minutos so eu
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto._
ME: s
ME: *[Assistente IA do Marcelo]:*

_Pronto! Convite criado e enviado:_

- _Levar Range Rover no Glauco_
- _(ninguém convidado)_
- _23 de jul. de 2026, 13:45 (45 min)_

_Aqui está o link do evento:_
https://www.google.com/calendar/event?eid=MmQ5aWlyYzI4MXF0a3RiNzN0cTFpaHJkdWcgbWFyY2Vsb2Ficml0dGFAbQ
ME: @assistante book 2 tasks for me for tomorrow. One: ask laura about Mr. White sale. Second: ask Laura about table removal
ME: @assistente book 2 tasks for me for tomorrow. One: ask laura about Mr. White sale. Second: ask Laura about table removal
ME: *[Marcelo's AI Assistant]:*

_Added to your list:_
_24/jul - ask laura about Mr. White sale_
_24/jul - ask Laura about table removal_

_Tell me if you need to change anything, otherwise we're good._
ME: @mary I have a pdf form I have to fill. I will send it and you should read it and tell me what info I need. ok?
ME: *[Assistente IA do Marcelo]:*

_Preciso te avisar: eu não consigo abrir/ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade hoje. Se quiser, você pode copiar o texto do formulário aqui que eu leio e te digo o que falta preencher. Quer fazer assim?_
ME: *[Assistente IA do Marcelo]:*

_Consegui abrir o PDF anexado agora! É o "2026 Guest Information & Authorization Form" da Powder South LLC (heli-ski). Os campos que você precisa preencher são:_

**Parte A — Informações do esquiador**
- _Nome do cliente, semana do passeio_
- _Tipo: esquiador/snowboarder/telemarker/mono-skier (snowboarder já marcado)_
- _Idade, altura, peso_
- _Nível: intermediário/avançado/expert_
- _Tamanho da bota (mm)_
- _Autoavaliação 1-5 em neve profunda e terreno íngreme_

**Parte B — Ficha médica**
- _Alergia a remédio, qualquer alergia, doenças_
- _Diabetes (sim/não), tipo sanguíneo_
- _Doença respiratória crônica (sim/não + detalhe)_
- _Doença cardíaca (sim/não + detalhe)_
- _Hipertensão (sim/não + detalhe)_
- _Mal de altitude no passado (sim/não + detalhe)_
- _Medicações atuais_
- _Informação de saúde adicional_
- _Seguro saúde: empresa, número, telefone de contato_

**Parte C — Contatos de emergência**
- _Contato primário: nome, telefone, email_
- _Contato reserva: nome, telefone, email_

**Parte D — Autorização de imagem**
- _Aceita ser marcado em posts? Se sim, handles de Instagram/Facebook/Twitter_
- _Interesse em colaborações? sim/não_

**Assinatura**
- _Assinatura, data, nome impresso_

_Quer que eu monte isso como uma lista de tarefas pra você preencher, ou só isso já resolve?_
ME: @mary criar tarefa dia 3 pagar banco inter
ME: *[Assistente IA do Marcelo]:*

_Adicionei à sua lista:_
_03/ago - pagar banco inter_
ME: @mary book me lunch tomorrow at Santo grão Oscar freire 12:00
ME: *[Assistente IA do Marcelo]:*

_Confirme este evento:_
- *Almoço*
- _29 de jul. de 2026, 12:00 (45 min)_
- _📍 Santo Grão, Rua Oscar Freire, 413 - Jardim Paulista, São Paulo - SP_

_Esse é o endereço que encontrei para o Santo Grão na Oscar Freire — confirma? Responda "sim" para eu criar o evento._
ME: cancelar
ME: *[Assistente IA do Marcelo]:*

_Ok, cancelando o agendamento do almoço proposto. Nada foi criado._
ME: @mary feature request: google contacts actions. CRUD scope. Then the assistant must use it to i. know e-mail of contacts by searching the phone and finding e-mail; ii. save e-mail to contact linked by the phone number talking on WhatsApp.
ME: *[Assistente IA do Marcelo]:*

_Anotando essa feature. Pra fechar a spec:_

- _Trigger: essa ação de contatos roda automaticamente sempre que eu preciso de um e-mail (ex: pra convidar alguém numa reunião) e só tenho o telefone — é isso, ou você também quer poder pedir manualmente tipo 'busca o e-mail desse contato'?_
- _Quando eu salvo um e-mail novo que apareceu na conversa do WhatsApp, devo sempre perguntar sua confirmação antes de gravar no Google Contacts, ou pode salvar direto?_
- _Edge case: e se o número não tiver nenhum contato correspondente no Google Contacts — eu crio um contato novo ou só aviso que não achei?_
- _Edge case: e se o contato já tiver um e-mail salvo diferente do que apareceu agora — eu substituo, pergunto, ou guardo como um segundo e-mail?_
ME: Trigger: a ação roda sempre que o orquestrador achar necessário

para salvar email no contato, vc deve enviar uma msg para mim mesmo, nao inline na conversa que ele apareceu.

edge case: se nao tiver, sim, crie.

guarda um segundo email. e se algum contato a ser convidado tiver 2 e-mails ou mais anotados, a agente deve perguntar qual usar.
ME: *[Assistente IA do Marcelo]:*

_Spec salva! Feature 'Google Contacts CRUD Integration' documentada com trigger, fluxo, edge cases e as respostas que você deu._
ME: @mary anote erro: eu comecei o chat em ingles e vc migrou pra português.

vc deve sempre falar na língua que a conversa começa
```
