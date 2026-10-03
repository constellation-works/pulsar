---
type: design
summary: "Reference: config.toml keys"
last_validated: 2026-09-26
---

# Reference: `config.toml`

Optional, in the pulsar home ([settings.py](../../../../src/pulsar/app/settings.py)). Every key
has a default. An unknown key or bad value fails at load with `invalid_config` whose message
starts with the resolved path of the file and names the key (`detail: {path, key}`), rather
than falling back silently. It is the only source of settings for every surface, the Orbit
plugin included (`[plugins.pulsar]` takes no keys).

The file may be world-readable, but a symlinked `config.toml`, one owned by another user, or
one writable by group or other users is refused (`insecure_storage`, fix `chmod go-w <path>`):
it sets budgets, media roots and `expected_handle`.

```toml
default_account = "x:constworks"     # the account a call without `account` acts as

[accounts."x:constworks"]
expected_handle = "constworks"       # bound handle must match, else account_mismatch

[prices.x]                           # USD per post: estimated_cost_usd and budgets
plain_post_usd = 0.015               # X changes its prices; verify on the developer portal
url_post_usd = 0.20                  # (flat plain_post_usd/url_post_usd under [prices] = X)
read_post_usd = 0.005                # per post a read returns (mentions, metrics); an estimate

[policy]                             # checked before any network call
daily_budget_usd = 1.0               # all accounts; 0 stops all paid publishing
monthly_budget_usd = 10.0
max_posts_per_day = 5                # per account; every post of a thread counts
quiet_hours = "23:00-07:00"          # optional; default none
timezone = "UTC"                     # IANA zone for the day/month boundary and quiet hours

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none (path uploads off)

[dispatch]                           # the plans `pulsar.dispatch` may publish
plans = ["x-updates/**/*.yaml", "engagement/**/*.yaml"]   # default: none (dispatch idle)
```

| Key | Default | Where it is enforced |
|---|---|---|
| `default_account` | none: the only bound account | [Accounts — Design §3](../../accounts/2_design.md) |
| `accounts.<alias>.expected_handle` | none: the alias's handle only | [Accounts — Design §3](../../accounts/2_design.md) |
| `prices.<provider>.plain_post_usd`, `url_post_usd` | X: 0.015 / 0.20; others: 0 | [specs/policy.md](../specs/policy.md) |
| `prices.<provider>.read_post_usd` | X: 0.005; others: 0 | [Engagement — Design §1](../../engagement/2_design.md#1-reads) |
| `policy.*` | as above, no quiet hours | [specs/policy.md](../specs/policy.md) |
| `media.roots` | none | [specs/media-confinement.md](../specs/media-confinement.md) |
| `dispatch.plans` | none: dispatch publishes nothing | [Engagement — Design §5](../../engagement/2_design.md#5-scheduled-dispatch) |

Numbers must be finite (TOML's `nan` and `inf` are refused) and within these bounds, which exist
to catch a typo at load rather than let it through a budget:

| Key | Type | Allowed |
|---|---|---|
| `prices.<provider>.plain_post_usd`, `url_post_usd`, `read_post_usd` | number | 0 to 100 |
| `policy.daily_budget_usd` | number | 0 to 10,000 |
| `policy.monthly_budget_usd` | number | 0 to 100,000 |
| `policy.max_posts_per_day` | integer (not `5.0`) | 0 to 10,000 |

`~` in a media root expands against the user's home that the surface resolved and passed down
(`Paths.user_home`); a surface that passes none gets `invalid_config` for a `~` root and must
spell the path out. `~name` is not expanded.

`dispatch.plans` is a list of at most 20 glob patterns relative to the workspace the Orbit
plugin runs in. A pattern that is absolute, starts with `~`, has a `..` component or a
backslash is `invalid_config`; the plan location is configuration, never a tool argument.

`pulsar serve` prints the effective media roots to stderr at startup.
