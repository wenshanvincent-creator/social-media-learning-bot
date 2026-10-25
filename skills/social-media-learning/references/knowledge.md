# Knowledge block input

The `put` command reads a JSON object. Required fields: `id` (lowercase slug), `title`, `summary`, `kind`, `applies_to`, `verification_status`, `citations`, `body`.

`title`, `summary`, `applies_to` and `body` are strings. In particular, `applies_to` is one compact text field, not a list. Optional `aliases`, `tags` and `not_for` are lists of strings. Worker submission validates the complete block shape before accepting it; semantic review still belongs to the integrator.

Optional route fields: `aliases`, `tags`, `not_for`, `version`, `verified_at`, `review_after`. Kinds: principle, case, procedure, implementation, trend, model. Do not set verification dates from edit dates.

Each citation is `{ "source_id": "actual-source-id", "segment_ids": ["actual-segment-id"] }`, uses `frame_times` for extracted video frames, or `image_ids` for imported post images. Obtain IDs with source-read/frame/source-images; never invent them. Storage validates their hashes and locations. The agent still checks whether they support the claim. State whether the review covered images, sampled still frames or actual motion. An image citation identifies that image, not every image in the post.

The body should preserve conditions, reasoning, actionable method, counterexamples and unresolved issues. Claims from a speaker are attributed; repeated reposts are not independent corroboration. Mark agent adaptations as synthesis.

`put` writes only knowledge/blocks/<id>.md, archives the prior version, and rebuilds the expendable index. Existing blocks require the current file hash; user-notes are outside this generated area. Changed source hashes prevent normal retrieval until the block is reviewed and updated.

The first version deliberately does not accept a `tested` label. A source demonstrating a method is not evidence that this agent executed it successfully.

New blocks store fingerprints for the cited evidence channels separately: text, video and exact frame hashes. A new transcript alone does not invalidate a frame-only case. Replaced/missing cited frames or a changed video do defer visual knowledge, even if caption text is unchanged. Earlier pilot blocks remain readable under their original source hashes.

