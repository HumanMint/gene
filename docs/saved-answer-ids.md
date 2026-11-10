# Saved answer ancestry

`GET /fri/contract` advertises `saved-answer-ids`. Saved context turns expose `answer_id`: the delivered Discord reply ID, or a stable random `turn:` identity when no delivered ID was recorded. Fri sends optional `carried_answers` on `/chat/turn` only with that capability, accumulating identities after withholding at every model request. These identities are metadata, never model instructions.

Gene records exact structural ancestor links, including ancestors of answers carried from saved history. Withdrawal retires those descendants while preserving a turn that saw only the question. Existing `drew_on` semantics remain. Both lookups use indexed tables; SQLite triggers maintain and clean links under older controller writes too. No reply-text matching or word-based retirement is used.

Bounds: 1,000 ancestors per turn, depth 64, and 10,000 tracked turns per guild. A valid save exceeding a bound returns success with `kept: false`; it cannot leave a partly tracked saved reply. An invalid or missing optional field is ignored completely, retaining prior behavior. These bounds apply to the new ancestry; legacy `drew_on` behavior is preserved.

The migration is additive and uses SQLite 3.40.1-compatible SQL. Old Fri omits metadata. Old Gene ignores the optional field. Rollback preserves retired breadcrumbs and new tables; old writers' deletes and inserts remain compatible. During rollback, new ancestry is not recorded by old Gene. Delayed saves after a retirement retain the pre-existing limitation noted in #267. Gene must merge and deploy before the paired Fri change.
