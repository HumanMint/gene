# Fri tool results and everyday controls

Tool receipts distinguish `completed`, `rejected`, `failed` and `uncertain`.
A completed check is evidence only, never a new permission. A definite controller
refusal keeps its reason. If Fri cannot finish the model reply after a tool runs,
she returns a deterministic receipt without repeating the tool. A queued song is
reported as queued; an ambiguous result is never reported as success. Earlier
successful saves remain visible if a later step fails. Conversation history keeps
the existing neutral tool breadcrumb rather than retrieved member evidence.
If recording that breadcrumb or rechecking access fails, Fri preserves safe
completion metadata without revealing retrieved contents or asking for a blind retry.

Say `fri controls` for the compact control menu. Exact pause, resume, skip, stop,
volume, queue, now playing, status and version requests use the existing Gene
handoff without a model call, including during a social cooldown. Normal current
membership, source-message, voice, DJ, permission and Red cooldown checks still
apply. This does not add arbitrary commands, administration or release authority.
Complex music requests continue through ordinary conversation.
Bare pause/resume/skip/stop needs a controller receipt from this member and
conversation within the last two minutes. Otherwise name the object, such as
`fri stop the music` or `fri pause music`. An unrelated DM saying only “stop”
cannot silently stop the shared audio, even if the conversation model selects it.

A separately persisted allowance permits twelve operational requests per member
and server in thirty minutes. It includes at most one inference turn answering an
already accepted five-minute clarification. That turn can only continue its music
command or public research; it cannot gain unrelated saved-work or feature tools.
Only the existing controller transaction consumes music permission. Cancellation,
source edits, privacy changes, expiry and restart keep their existing fences.
Operational admission records contain source digests and budget metadata, never
message text. Verified inference-free closing acknowledgments do not spend a
social turn.

Offline tests cover persisted saves after final-reply failure, typed refusals,
uncertain/time-limited outcomes, prevention of repeated tools, source and role
checks, concurrent allowance reservations and restart. Music continuation tests
also use the installed Red parser and checks with fictional members and offline
Discord transports. They do not establish real Discord delivery, model quality or
audible playback.
