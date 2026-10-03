---
title: Channels — Design
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Accepted
feature: channels
doc_role: design
type: design
summary: The Channel protocol's failure semantics, the provider -> channel factory, and how the X and Bluesky adapters measure text, upload media, map errors and fingerprint posts.
tags: [channels, providers, x, bluesky, adapter, fingerprint, facets, dpop]
paths: ["src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/channels/x/**", "src/pulsar/app/core/channels/bluesky/**", "src/pulsar/app/runtime.py"]
related_features: [publishing, accounts]
related_artifacts: [ORB-13006, ORB-13028, ORB-13031, ORB-13207, ORB-13285]
---

# Channels — Design

The adapter contract, the factory that picks a provider's channel, and the X and Bluesky
adapters as built. Other providers are in [3_vision.md](./3_vision.md).

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

[tests/test_channel_contract.py](../../../tests/test_channel_contract.py) runs one suite over every
channel on its fake transport: offline checks, a thread with media and alt text, a quote,
delete, reconcile after a lost reply, `whoami` and both reads.

**The provider -> channel factory.** An alias names its provider (`x:…`, `bsky:…`).
[app/runtime.py](../../../src/pulsar/app/runtime.py) `client_for` builds the account's client for
that provider and `channel` binds it as a `Channel`; any other provider is `unsupported`.
Identity checks call the channel's `whoami`. Nothing else outside a provider's package names
it, except X's own login, health check, single-post tools and `import-posted`, which predate
the rule; `tests/test_layers.py` keeps that list closed. The single-post tools
(`create_post`, `upload_media`, `delete_post`) are X's and refuse another provider's account
with `unsupported`; a Bluesky post is published as a plan.

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
  monotonic deadline is 30 seconds plus media bytes divided by 256 KiB/s (about 2 Mbit/s;
  430 seconds for 100 MiB). Each request is bounded by the remaining time. A request still in
  flight at the deadline, or a next request that cannot start before it, fails as retryable
  `upload_timeout`; a completed response is kept. Each HTTP request also has a 30-second
  timeout. The Orbit plugin passes one absolute deadline 55 seconds after `publish` starts,
  five seconds below its 60-second backend limit; the client uses the earlier deadline. CLI
  and MCP do not pass this cap. The publisher records a timed-out item failed, so a retry can
  upload again.
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

## 8. Bluesky Text and Facets

[bluesky/text.py](../../../src/pulsar/app/core/channels/bluesky/text.py): at most 300 graphemes
(extended grapheme clusters: an emoji ZWJ sequence, a flag or a letter with its combining marks
counts one) and 3000 UTF-8 bytes. Empty text and control characters are `invalid_text`, as on
X. Posting is free: the default `[prices.bsky]` table is zero.

Links, mentions and hashtags are not markup in the text but facets beside it, each a range of
UTF-8 byte offsets. `detect_facets` finds `http(s)://` links and bare domains with an
alphabetic TLD (sent as `https://`), dropping one trailing `.,;:!?` and an unbalanced `)`;
`@handle` mentions (a domain-shaped handle, lower-cased); and `#tag` or `＃tag` up to 64
characters that are not all digits. `create` resolves each mention's handle to a DID; a handle
that does not resolve stays plain text.

## 9. Bluesky Posts and Media

[bluesky/adapter.py](../../../src/pulsar/app/core/channels/bluesky/adapter.py) writes an
`app.bsky.feed.post` record with `com.atproto.repo.createRecord`. A post's id is its AT URI
(`at://<did>/app.bsky.feed.post/<rkey>`); its URL is `https://bsky.app/profile/<handle>/post/<rkey>`.

- **Threads and replies.** A reply names its parent and the thread's root by strong reference
  (URI and CID). The channel remembers the posts it created, so a thread replies from memory; a
  reply to any other post reads it first (`getRecord` for the account's own, else
  `app.bsky.feed.getPosts`) and takes the root from the parent's own reply, if it has one. A
  target that does not exist is `not_found` and nothing is created.
- **Quotes** are an `app.bsky.embed.record` embed; with media, `recordWithMedia`.
- **Media.** `upload` sends the bytes with `com.atproto.repo.uploadBlob` and returns the blob's
  CID as the media id; the channel keeps the blob and alt text for `create`, so a media id from
  another channel or run is `invalid_media` before anything is sent. Up to four images
  (PNG, JPEG, GIF, WebP; 1,000,000 bytes each) with alt text up to 2000 graphemes, or one
  video (MP4, 100,000,000 bytes) alone with alt text up to 1000. Uploads have the deadline X's
  have (30 seconds plus bytes at 256 KiB/s, capped by the caller's); a video is attached as an
  `app.bsky.embed.video` blob, without the video service's transcoding step.
- **Delete** takes the post's AT URI and deletes the record; deleting a missing record
  succeeds. A URI in another account's repository is `invalid_argument`.

## 10. Bluesky Client: Auth and Errors

[bluesky/client.py](../../../src/pulsar/app/core/channels/bluesky/client.py) speaks XRPC to the
account's PDS with the stored access token. It refreshes ahead of expiry, or once when the PDS
answers 401 or `ExpiredToken`/`InvalidToken`, under the same lock and compare-and-swap save as
X; a refused refresh is `auth_expired`. Like `XClient`, a client is pinned to the binding its
identity check was for (`account_mismatch` otherwise).

A bundle whose `token_type` is `DPoP` needs a proof signer: the client takes one by injection
(a `DpopProof`; [bluesky/dpop.py](../../../src/pulsar/app/core/channels/bluesky/dpop.py)
`Es256Proof` signs RFC 9449 proofs with a P-256 key). Each request, the token refresh
included, carries a fresh proof with the method, URL, the access token's hash and the server's
latest nonce; a `use_dpop_nonce` answer is retried once with the new nonce. A DPoP bundle
without a signer is `unsupported` before anything is sent.

Errors map like X's: 429 `rate_limited` (retryable), 404 or `RecordNotFound` `not_found`, 413
or `BlobTooLarge` `invalid_media`, 403 `forbidden`, the rest `api_error`, with the PDS's
`error` and `message` (redacted, at most 500 characters) in `detail`. On `create`, a 5xx, a
post-send transport failure or a success without a URI and CID is `outcome_unknown`.

## 11. Bluesky Fingerprint, Recent Posts and Reads

Bluesky stores text as sent, so its fingerprint is NFC with whitespace collapsed, links and
mentions included, applied to both copies.

`recent_posts(since)` lists the account's own `app.bsky.feed.post` records
(`com.atproto.repo.listRecords`, newest first, 100 a page, up to five pages) back to `since`.
Reconcile needs no deterministic record key: `create` gets no idempotency key to derive one
from, and the listing is free. It is `complete: false` at the page cap or when a record has no
readable text or `createdAt`.

`mentions` pages `app.bsky.notification.listNotifications` for the `mention`, `reply` and
`quote` reasons and takes counts from `app.bsky.feed.getPosts`. `own_posts` pages
`app.bsky.feed.getAuthorFeed` (`posts_with_replies`, reposts and others' posts left out). Both
stop at `since`, `max_posts` or three pages. Bluesky has public counts only: likes, replies,
reposts and quotes; impressions and clicks stay empty. A conversation is identified by its root
post's URI.

## 12. Concerns & Honest Limitations

- **Bluesky has no login yet.** Its channel runs on a credential bundle someone else stored;
  the atproto OAuth login (PAR, PKCE, DPoP) is a sibling task. Until it stores the DPoP key,
  the PDS URL and the token endpoint, the factory builds every Bluesky client for
  `bsky.social` without a proof, so a DPoP-bound bundle is `unsupported` before anything is
  sent.
- **`AuthFlow` is unimplemented.** Both providers fit the contract unchanged, which the second
  adapter was meant to test; the login path is still X-specific CLI code.
- **Graphemes are approximated.** pulsar's segmentation omits the Indic conjunct rule, so it
  can count more graphemes than Bluesky does and refuse a post Bluesky would take, never the
  reverse. Facet detection follows the Bluesky app's rules, not a spec; a link it misses posts
  as plain text.
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
- [ORB-13031] — added the Bluesky channel, the contract suite and the provider -> channel factory.
- [ORB-13207] — bounded chunked media upload before finalize.
- [ORB-13285] — set a conservative upload rate and pass the Orbit deadline to the client.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
