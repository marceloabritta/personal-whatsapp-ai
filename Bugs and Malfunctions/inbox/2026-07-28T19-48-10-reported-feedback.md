# Failure report — reported / feedback  (2026-07-28 19:48:10)

| Field       | Value |
|-------------|-------|
| When        | 2026-07-28 19:48:10 (America/Sao_Paulo) |
| Chat        | 5511976001033@s.whatsapp.net (?) |
| Trigger     | reported |
| Source      | **OWNER-REPORTED** (human-verified) |
| Failed task | feedback |
| Status      | needs-plan |

## What the user asked
anote erro. vc é capaz de ler PDFs, mas por algum motivo vc nao ve o PDF que foi enviado antes de eu te chamar com a @ de início.

_(On an owner-reported failure this is the NOTE itself, not the order that misbehaved — that one is in the transcript.)_

## What happened
The secretary told the owner it cannot open/read attached PDFs at all, but later successfully summarized a PDF once asked again — meaning it can read PDFs but fails to see ones sent before the @mary trigger message.

```
n/a
```

## Owner's report
**What the owner says went wrong:** The secretary told the owner it cannot open/read attached PDFs at all, but later successfully summarized a PDF once asked again — meaning it can read PDFs but fails to see ones sent before the @mary trigger message.
**What they expected instead:** The secretary should be able to see and read a PDF that was sent earlier in the chat, even if it was sent before the message that tags @mary.
**His exact words:** The secretary claimed it could not read PDFs when a PDF was sent before the @mary trigger tag, but it can read PDFs sent after being tagged. It seems the secretary doesn't see/process attachments sent prior to being summoned with the trigger tag.

### The offending message (quoted)
_Not quoted — the owner reported this without replying to a specific message. The evidence is in the transcript and the logs below._

## Auto-analysis (best-effort, UNVERIFIED — a cheap model's guess, not evidence)
LIKELY CAUSE: Session/conversation history context is not being loaded or passed to the LLM router when a message arrives before the @mary trigger tag. The router likely only includes recent messages in the LLM prompt, excluding earlier attachments that weren't explicitly tagged or summoned. When the user re-sends the request after @mary, the PDF is now in the active context window.

SUSPECTED AREA: The webhook message handler or session context builder (likely in the Express route that processes incoming WhatsApp messages) — check how it constructs the message history and whether it filters/truncates based on tag presence rather than full conversation state. Uncertainty: unclear if this is message filtering, Redis session retrieval, or LLM prompt assembly, but the pattern points to context scoping around the @mary trigger boundary.

## Recent logs
```
2026-07-28T22:34:30.171Z [log] sessions: Redis connected
2026-07-28T22:34:30.176Z [log] settings: Redis connected
2026-07-28T22:34:33.846Z [log] skill loaded: "1. Calendar Actions" -> calendar_action
2026-07-28T22:34:33.851Z [log] skill loaded: "2. Audio transcriptions" -> transcribe_audio
2026-07-28T22:34:33.857Z [log] skill loaded: "3. Tasks" -> task_action
2026-07-28T22:34:33.863Z [log] skill loaded: "4. Feature Requests" -> feature_request
2026-07-28T22:34:33.867Z [log] skill loaded: "5. Feedback" -> feedback
2026-07-28T22:34:33.874Z [log] skill loaded: "6. Flight Search" -> flight_search
2026-07-28T22:34:33.882Z [log] skill loaded: "7. Assistant Settings" -> assistant_settings
2026-07-28T22:34:33.883Z [log] mary skills: calendar_action, transcribe_audio, task_action, feature_request, feedback, flight_search, assistant_settings
2026-07-28T22:34:33.889Z [log] new-tags: @mary (source: SECRETARY_TAG_NEW seed)
2026-07-28T22:34:33.899Z [log] Secretary v2.0 (orchestrator) listening on port 3000
2026-07-28T22:45:56.869Z [log] TRANSCRIPT>>>
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

_Preciso te avisar: eu não consigo abrir/ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade hoje. Se quiser, você pode copiar o … «truncated 4053 chars»
2026-07-28T22:45:59.744Z [log] ROUTER RAW: {"say": "Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Se puder colar o texto do PDF aqui, eu leio e resumo pra você.", "next": "done", "skills": [], "info": {}, "lang": "en", "awaitFrom": "owner"}
2026-07-28T22:45:59.746Z [log] TURN -> {"next":"done","skills":[],"hasSay":true}
2026-07-28T22:46:58.447Z [log] TRANSCRIPT>>>
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

_Consegui abrir o PDF anexado agora! É o "2026 Guest Information & Authorization Form" da Powder South LLC (heli-… «truncated 4051 chars»
2026-07-28T22:47:13.251Z [log] ROUTER RAW: {"say": "*[Marcelo's AI Assistant]:*\n\n_Aqui está o resumo do PDF — DENZA B5:_\n\n_SUV híbrido plug-in de luxo, chassi sobre longarinas (on-frame), com até 1.200 km de autonomia combinada (100 km no elétrico). Tração integral 4WD inteligente, suspensão hidráulica Disus-P, modos de terreno (lama/areia/neve/rocha) e raio de giro de 3,4m em modo U-Turn._\n\n_Performance: 505 kW e 760 Nm combinados, 0-100 km/h em 4,8s. Motor a combustão de 143 kW + motores elétricos dianteiro (200 kW) e traseiro (285 kW). Bateria LFP de 31,8 kWh, carrega até 100 kW em DC e suporta V2L (descarrega pra outros aparelhos)._\n\n_Dimensões: 4,89m de comprimento, 2,80m entre-eixos, 220mm de vão livre, porta-malas de 470L. Capacidade de reboque de 2,5t._\n\n_Interior: bancos com ajuste elétrico, aquecidos/ventilados/massagem, tela de 15,6\" central + 12,3\" pro passageiro, som Devialet 18 alto-falantes, teto solar panorâmico, geladeira de 4,5L._\n\n_Segurança: 11 airbags, câmeras 360°, ACC, AEB, monitoramento de ponto cego, entre outros._\n\n_Cores externas: Olive Green, Sky Blue, Onyx Black, Stone Gray, Pearl Gold._", "next": "listen", "skills": [], "info": {}, "lang": "en", "awaitFrom": "owner"}
2026-07-28T22:47:13.252Z [log] TURN -> {"next":"listen","skills":[],"hasSay":true}
2026-07-28T22:48:03.710Z [log] TRANSCRIPT>>>
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
- _Nível: intermediári… «truncated 5053 chars»
2026-07-28T22:48:10.018Z [log] ROUTER RAW: {"say": null, "next": "execute", "skills": ["feedback"], "info": {"note": "The secretary claimed it could not read PDFs when a PDF was sent before the @mary trigger tag, but it can read PDFs sent after being tagged. It seems the secretary doesn't see/process attachments sent prior to being summoned with the trigger tag.", "what_went_wrong": "The secretary told the owner it cannot open/read attached PDFs at all, but later successfully summarized a PDF once asked again — meaning it can read PDFs but fails to see ones sent before the @mary trigger message.", "expected": "The secretary should be able to see and read a PDF that was sent earlier in the chat, even if it was sent before the message that tags @mary.", "suspected_skill": null}, "lang": "pt", "awaitFrom": "owner"}
2026-07-28T22:48:10.021Z [log] TURN -> {"next":"execute","skills":["feedback"],"hasSay":false}
```

## Conversation transcript
```
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
ME: *[Marcelo's AI Assistant]:*

_Noted — logged as a mistake to investigate: "The secretary replied in Portuguese even though the conversation was originally started in English, switching languages mid-conversation". It goes into the next improvement pass. I haven't changed anything yet._
ME: @mary me resuma o PDF acima, sobre o Denza.
ME: *[Marcelo's AI Assistant]:*

_Não consigo abrir ou ler arquivos anexados (PDF, imagens etc.) aqui na conversa — não tenho essa capacidade. Se puder colar o texto do PDF aqui, eu leio e resumo pra você._
ME: @mary esse arquivo aqui
ME: *[Marcelo's AI Assistant]:*

*[Marcelo's AI Assistant]:*

_Aqui está o resumo do PDF — DENZA B5:_

_SUV híbrido plug-in de luxo, chassi sobre longarinas (on-frame), com até 1.200 km de autonomia combinada (100 km no elétrico). Tração integral 4WD inteligente, suspensão hidráulica Disus-P, modos de terreno (lama/areia/neve/rocha) e raio de giro de 3,4m em modo U-Turn._

_Performance: 505 kW e 760 Nm combinados, 0-100 km/h em 4,8s. Motor a combustão de 143 kW + motores elétricos dianteiro (200 kW) e traseiro (285 kW). Bateria LFP de 31,8 kWh, carrega até 100 kW em DC e suporta V2L (descarrega pra outros aparelhos)._

_Dimensões: 4,89m de comprimento, 2,80m entre-eixos, 220mm de vão livre, porta-malas de 470L. Capacidade de reboque de 2,5t._

_Interior: bancos com ajuste elétrico, aquecidos/ventilados/massagem, tela de 15,6" central + 12,3" pro passageiro, som Devialet 18 alto-falantes, teto solar panorâmico, geladeira de 4,5L._

_Segurança: 11 airbags, câmeras 360°, ACC, AEB, monitoramento de ponto cego, entre outros._

_Cores externas: Olive Green, Sky Blue, Onyx Black, Stone Gray, Pearl Gold._
ME: @mary anote erro. vc é capaz de ler PDFs, mas por algum motivo vc nao ve o PDF que foi enviado antes de eu te chamar com a @ de início.
```
