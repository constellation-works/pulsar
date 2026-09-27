---
title: Channels — Design
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Accepted
feature: channels
doc_role: design
type: design
summary: The Channel protocol's failure semantics, and how the X adapter measures text, uploads media, maps errors and fingerprints posts.
tags: [channels, providers, x, adapter, fingerprint]
paths: ["src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/channels/x/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-13006, ORB-13028, ORB-13207]
---

# Channels — Design

The adapter contract and the X adapter as built. Other providers are in
[3_vision.md](./3_vision.md).

## 1. The Contract

[adapter.py](../../../src/pulsar/app/core/channels/contract.py) `Channel` is a protocol; core imports no
provider and no HTTP library.

| Method | Network | Contract |
|---|---|---|
| `capabilities()` | no | static `Capabilities` |
| `check_post(post, prices)` | no | provider text rules; returns normalised text, length, `has_url`, estimated cost |
| `check_media(media)` | no | count and combination rules over sniffed types |
| `check_target(reply_to, quote)` | no | id syntax, reply/quote support |
| `fingerprint(text)` | no | normalised hash of text as pulsar sends it |
| `whoami()` | yes | `Identity(provider_user_id, handle)` of the bound account |
| `upload(media)` | yes | media id; creates nothing visible |
| `create(post, …)` | yes | `Published(post_id, url, text)`; the only non-idempotent step |
| `delete(post_id)` | yes | idempotent at the provider |
| `recent_posts(since)` | yes | `RecentPosts(posts, complete)`, each with the provider-side fingerprint |
| `mentions(since, max_posts)` | yes | `Page[Mention]`: others' posts mentioning the account, with author, reply target and public metrics |
| `own_posts(since, max_posts)` | yes | `Page[OwnPost]`: the account's posts with their metrics |

A `Page` says whether it is `complete` and how many posts the provider `fetched` (and billed),
which can exceed what it returns. `Capabilities.mentions` and `.metrics` say whether the two
reads work.

**Failure semantics every adapter keeps** (the ledger depends on them):

- a `PulsarError` other than `outcome_unknown` means the provider did not act;
- `OutcomeUnknown` means it may have acted: a timeout, dropped connection or 5xx after the
  request left, or a success response without a readable id;
- a failure before the request left (connect error, pool timeout) is `api_error`, retryable.

`AuthFlow` (`begin`, `complete`) is declared for per-provider logins; X's login predates it and
is still module functions in [app/core/channels/x/auth.py](../../../src/pulsar/app/core/channels/x/auth.py).

## 2. X Text Rules

[text.py](../../../src/pulsar/app/core/channels/x/text.py): at most 280 weighted characters; a URL
counts 23, CJK and emoji count 2, other characters 1. Empty text, control characters, and reply
plus quote together are `invalid_text`. The estimated cost is `url_post_usd` when the text
contains a URL, else `plain_post_usd`.

## 3. X Media

- Types: PNG, JPEG, GIF, WebP images up to 5 MiB; MP4 video up to 100 MiB (a local cap; X also
  checks the account's entitlement). At most 4 items per post; video and GIF must be alone.
- Upload uses the v2 chunked endpoints (`initialize`, `append`, `finalize`): 1 MiB chunks for
  images, 4 MiB for video. From before `initialize` through the `finalize` response, the
  monotonic deadline is 5 seconds plus media bytes divided by 5 MiB/s (at most 25 seconds for
  the 100 MiB local cap). Each request is bounded by the remaining time, and the client checks
  the deadline after each response. On expiry, no further chunk or `finalize` is sent and the
  client raises retryable `upload_timeout`. The publisher records the item failed, so a retry
  can upload again.
  Video then waits up to five minutes for processing state `succeeded`;
  `failed` or a timeout is `invalid_media` with the last processing detail and no media id.
- Alt text goes to `POST /2/media/metadata` after the upload and before the post, for images
  and GIFs. For video it is kept in the plan and ledger but not sent.

## 4. X Error Mapping

[client.py](../../../src/pulsar/app/core/channels/x/client.py) `map_http_error` turns X's responses
into codes, passing X's reason through in `detail`: `duplicate` (400 naming a duplicate),
`forbidden` (403), `rate_limited` (429, retryable), `not_found` (404), and `api_error` for the
rest. A refresh X rejects is `auth_expired` and marks the account `reauth_required`. On
`create`, 5xx and post-send transport failures are `outcome_unknown`. Ids (`post_id`, reply and quote targets, media ids) must be 1–19 digits
(`invalid_argument`).

## 5. Fingerprint

X rewrites posted text: links become `t.co` links, `&`, `<`, `>` come back HTML-escaped, a
post with media gets a trailing `t.co` link, and a reply gets the replied-to accounts'
`@handles` prepended. [app/core/channels/x/adapter.py](../../../src/pulsar/app/core/channels/x/adapter.py):

- `fingerprint(text)` hashes the text with every URL and any leading run of `@handles`
  removed, NFC, whitespace collapsed. It is applied to what pulsar sends.
- `remote_fingerprint(text)` unescapes X's HTML entities first, then fingerprints. It is applied
  only to X's copy: a local `&amp;` is literal text.

## 6. Recent Posts

`recent_posts(since)` pages the account's timeline from `since`, 100 posts a page, up to three
pages, and reports `complete: false` when it stopped at the page cap with more to read. X bills post reads, so reconcile
calls it only when something is unresolved.

## 7. Reads

`mentions` pages `/users/:id/mentions` and `own_posts` pages `/users/:id/tweets` (retweets
excluded) from `since`, newest first, up to `max_posts` and three pages. Each page asks for no
more than is still wanted (X's minimum is 5, its maximum 100), since X bills every post a page
returns. Mentions expand their authors for handles; text comes back with X's HTML entities
unescaped. Own posts ask for `non_public_metrics` (impressions, link clicks, profile clicks),
which X gives only to the author for posts from the last 30 days; public counts (likes,
replies, reposts, quotes, bookmarks) come with both. A post without an id or a readable
`created_at` is left out and the page marked incomplete.

## 8. Concerns & Honest Limitations

- **One provider has exercised the contract.** Its shape is inferred from X plus a paper study
  of Bluesky, Mastodon and LinkedIn; the second adapter will find what it missed.
- **`AuthFlow` is unimplemented.** The login path is X-specific CLI code until a second
  provider needs the abstraction.
- **Fingerprint collisions.** Posts that differ only in links, leading mentions or whitespace
  collide. Reconcile compares one account's posts inside a claim's time window and never
  matches an id the ledger already holds, and a collision errs towards "published".
- **Prices are X's, by hand.** X changes its price list without notice; the configured table
  is only as right as its last manual check. The read price is an estimate, and X may also
  bill the author records a mentions read expands.
- **Video processing has a separate deadline.** The five-minute status poll can exceed the
  Orbit plugin's 60-second call timeout; the upload deadline only covers `initialize` through
  `finalize`.

## Task References

- [ORB-13006] — added MP4 upload with processing poll.
- [ORB-13028] — introduced the contract, the X channel and the fingerprint.
- [ORB-13207] — bounded chunked media upload before finalize.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
