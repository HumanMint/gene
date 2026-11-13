# Requested image sharing

Fri can fulfill an explicit request such as “share a random sfw image featuring
hatsune miku from danbooru” with one attached image and a clearly labeled original
post link. General web answers still omit optional citations; a requested URL is
part of the answer and appears in the first reply. Later source requests use the
existing member/conversation-scoped public research receipts.

The first image provider is Danbooru's Safebooru endpoint. Other sites remain
link-only. No new account or paid API is used. Subject selection and visual
inspection use the configured meme model/effort, currently the inexpensive Luna
role; usage appears under “image searches and checks.” The normal reply retains
the conversation model and voice.

One isolated planner sees only the validated current public image
request. Image requests can contain paragraphs and up to 4,000 characters; the
harness uses the complete current request, even when the action selected a shorter
excerpt, so late visual requirements remain part of inspection. The 240-character
web-query limit still applies to ordinary web research and saved source labels.
No history or inferred subject is added to an image request.
It chooses a subject tag. The provider returns up to six random,
general-rated records. Fri considers up to three eligible, distinct posts,
decodes each still image, and asks a tool-free vision evaluator to check relevance
and general-audience suitability. A rejected, uncertain, or failed inspection
never becomes an upload. If all candidates fail, Fri says they could not verify
one; they do not claim to have shown or inspected an unseen image.

## Boundaries

- Current explicit Discord message, existing member/read/write scope and web
  enablement only. No ambient image searches, imported-history authority,
  model-selected destination, shell, arbitrary download URL, or cross-channel post.
- Fixed provider and CDN hosts/paths, HTTPS, no redirects, cookies, credentials,
  proxy environment, or compressed HTTP bodies. Only PNG, JPEG and WebP stills.
- Maximum 4 MiB per candidate and 20 megapixels; three candidate downloads per
  request, with a 180-second deadline. Actual stream bytes and decoded image
  dimensions are checked. Images are resized to fit 1600 pixels and re-encoded
  without metadata. The exact re-encoded bytes inspected are the bytes attached.
- All image data remains in memory on the bot host for the turn. No image cache,
  files, raw pixels, local paths or data URLs enter chat memory. The selected
  public post link and short visual result use the existing expiring research
  receipt, scoped to that member/conversation and covered by memory erasure.
- Before work and delivery, recheck request identity/edit/deletion, membership,
  enabled settings, allowed channel, readable evidence and memory revision.
  The final text, attachment and attribution are sent as one reply. Missing
  attachment permission or a definite rejected upload gives a link fallback;
  an ambiguous server failure is not automatically retried.

Visual moderation is an additional check, not a guarantee. No explicit or
questionable rating is accepted even if a model says otherwise. Only selected,
accepted artwork receives a source link from this tool; rejected candidates are
not surfaced as a workaround.

The offline workflow checks real image decoding and the chat/controller/send
path, with provider and Discord I/O substituted. They cover rejected candidates,
corrupt/oversized responses, stale permission/request state, persistence, and
upload failure. Live provider availability and model judgement require separate
focused checks; passing unit tests alone does not establish those.

Provider fields: [Danbooru posts API](https://safebooru.donmai.us/wiki_pages/api:posts).
Delivery: [discord.py image upload](https://discordpy.readthedocs.io/en/stable/faq.html#how-do-i-upload-an-image).
