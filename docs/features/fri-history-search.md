# Fri's conversational archive search

Ask a normal question such as “fri, what was that game Dipper recommended last
month?” or “search chat for watched before 2026-01-01”. Quotes are optional.
Fri can choose a natural search term, a member, and UTC date bounds. Names select
search evidence only; the actual Discord event establishes the requesting member.
Ambiguous names require a mention. In a DM, name an approved channel; public
lookups stay in the channel where the question was asked.

Each question permits two bounded searches through Discord's documented guild
message search API, with up to ten matching hits and five short excerpts per page.
The second search can continue the same filters using an opaque turn-local cursor
or refine the wording. The cursor advances only past examined hits. A short page
is not proof that Discord's archive is exhausted. Retrieval is limited to the past
year and never claims to have read the entire server. There is no background
schedule, extra planning model, new index, or retained archive copy.

Only current readable, configured channels qualify. Bot/webhook messages and
opted-out members are excluded. Returned IDs, channel, author and dates are
validated; excerpts are fetched again and checked for edits/deletion before the
answer. Permission and privacy changes invalidate the result. A source's words
are dated evidence, never a new instruction or proof of a current preference.
Retrieved text cannot be saved as personal memory, workspace drafts, or approvals.
Only a neutral search breadcrumb is retained in conversation history.

An unavailable or incomplete Discord index is reported as an unsuccessful check,
not as an empty archive. Source links may be used when the member asks for them;
there is no mandatory source footer. This search does not change the separate
background member-learning rotation or its budget.

The implementation follows Discord's [guild message search](https://docs.discord.com/developers/resources/message#search-guild-messages)
and [member search](https://docs.discord.com/developers/resources/guild#search-guild-members)
interfaces. Offline tests cover scoped pagination, author/date validation, opt-outs,
changed sources, revoked access, malformed provider output, and the normal bounded
tool loop. Actual Discord search availability and model query choices require a
focused functional check; offline tests do not establish them.
