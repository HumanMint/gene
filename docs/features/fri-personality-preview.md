# Fri personality settings preview

Status: prepared for owner review; not applied. The accompanying
[JSON proposal](fri-personality-preview.json) contains only the proposed
`voice_notes` replacement. Publishing this candidate does not activate it.

The proposal makes dry sarcasm and playful pushback available from the start
of casual chat. It reduces assistant phrasing, automatic agreement, canned
praise, and questions added merely to prolong conversation. Replies stay short
and natural. Disagreement needs a reason; jokes are optional. Boundaries include
no manufactured conflict, escalating insults, humiliation, or piling on, and
backing off when unwelcome. Serious topics get direct, careful replies.

Use Fri's existing verified-owner settings conversation to prepare a versioned
preview of this text. Review the exact resulting `voice_notes` and confirm that
it is the only changed setting before clicking **Apply owner settings**.
The JSON is a proposed changes object, not an authenticated request or an Apply
receipt. It deliberately contains no runtime version: Fri must obtain the
current revision when preparing the preview. A stale preview requires a fresh
preview and review. Cancel leaves settings untouched.

This replaces the complete existing `voice_notes` value. Review any existing
style preferences before applying; preserve compatible preferences in a fresh
preview if needed. For a later reversal, preview the prior value against the
then-current revision and use the same owner Apply flow.

The existing runtime accepts these style notes without a code change. Changing
Fri's built-in prompt or settings workflow requires maintainer work outside
the feature directories. This candidate adds no runtime hooks or permissions.

Offline tests exercise preview creation without saving, owner/message checks,
Cancel, Apply through the controller's in-process API, persistence, and stale
revision rejection using temporary state and synthetic test actors. They do
not evaluate generated tone. A later isolated voice evaluation should check
casual chat without prior teasing, justified disagreement, neutral greetings,
unwelcome banter, and serious topics. Reject forced jokes, hostility, automatic
agreement, and needless questions. No model, Discord, or production services
were used to prepare this proposal; model behavior and live Discord delivery
remain unverified.
