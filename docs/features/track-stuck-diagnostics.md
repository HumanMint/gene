# Track Stuck diagnostics

The installed Red Audio handler produces the **Track Stuck** embed for a
Lavalink `TRACK_STUCK` (`TrackStuckEvent`) event. Lavalink supplies `thresholdMs`
to listeners as an integer. Red's notification includes the track description
but omits that threshold. The notification alone does not establish a cause.

Red already owns error counting, removal of failed queue entries, conditional
skipping when the node is ready, and shutdown after repeated errors. Gene does
not change those paths or react to Discord embeds.

While loaded, Gene registers a read-only Lavalink listener for its configured
server. Each stuck event produces a warning in `gene.audio` containing JSON:
the threshold and position in milliseconds, current-track presence and length,
paused and reported-playing flags, connection and node readiness, and queue
size. Missing or invalid scalar values are `null`. No track titles, URLs,
encoded tracks, member identities, guild/channel identifiers, or raw payloads
are logged. The listener is removed when Gene unloads.

Use the warning timestamp to compare with separately available backend logs.
Listeners run concurrently, so player fields are observations at callback time;
they do not identify the failed track reliably or prove that recovery succeeded.
In particular, `paused=false` does not establish playback. There is no new retry,
skip, queue mutation, notification, or controller call. A root-cause repair
requires additional evidence; changes to the backend or dependencies require
maintainer work.
