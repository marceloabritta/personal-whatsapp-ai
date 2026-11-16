"""History search — "@lisa I spoke to someone about X, who was it?", in the owner's chat with himself.

An agent with read-only tools over every WhatsApp message Evolution has stored, plus the transcripts
of voice notes. The parts, each in its own module so they can be built and tested apart:

  types.py    the contracts every other module codes against (results, the store, the media seam)
  store.py    PgHistoryStore — the queries behind the tools, over the lisa_search index
  indexer.py  HistoryIndexer — keeps lisa_search in step with Evolution's "Message" table
  voice.py    VoiceCapture — transcribes every voice note in every chat as it arrives, and the
              ones still downloadable from before, so spoken conversations become searchable
  media.py    HistoryMedia — fetch a file / transcribe a voice note on demand; send files back
  agent.py    run_history_agent — the client-tool loop that answers one turn
  runtime.py  wiring: what deps and the FastAPI lifespan build and start

Every switch ships OFF (config.py: HISTORY_*). Off, nothing here is imported by the reply path."""
