# Models and fast-changing implementations

Model choice and “latest/current” advice require a new check of official provider documentation at the time of the decision. Never select a default model from memory or an old tutorial. Record exact provider, model ID, access surface, observed availability, official URL, verification date and unresolved regional/account limitations. A marketing name is not necessarily an API ID or a model available in the user's subscription.

Model blocks need `kind: model`, `provider`, `model_id`, `official_url`, `verified_at` and citations to archived text from that exact official URL. Use `archive-web --run ID URL`, inspect the relevant text, then write the block. The CLI has a small official-domain registry; unsupported providers stay unverified until that registry is deliberately extended from official evidence. Source presence does not itself prove a claim, so perform the semantic check before applying it.

Model verification becomes review_due after seven days as an initial policy; `--current` always requires a new official check even for a recent entry. Historical knowledge can be read with a warning but must not become a current recommendation. Do not reset verified_at when changing prose.

Framework implementation notes carry a version and dated verification; a target version mismatch defers the body. Principles do not expire simply because the source is old. Style trends need a defined platform/time window and review date. Future verification dates are rejected.

