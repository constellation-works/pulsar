---
type: design
summary: "Reference: config.toml keys"
last_validated: 2026-09-26
---

# Reference: `config.toml`

Optional, in the pulsar home ([settings.py](../../../../src/pulsar/core/settings.py)). Every key
has a default. An unknown key or bad value fails with `invalid_config` naming the file, rather
than falling back silently. It is the only source of settings for every surface, the Orbit
plugin included (`[plugins.pulsar]` takes no keys).

```toml
default_account = "x:constworks"     # the account a call without `account` acts as

[accounts."x:constworks"]
expected_handle = "constworks"       # bound handle must match, else account_mismatch

[prices.x]                           # USD per post: estimated_cost_usd and budgets
plain_post_usd = 0.015               # X changes its prices; verify on the developer portal
url_post_usd = 0.20                  # (flat plain_post_usd/url_post_usd under [prices] = X)

[policy]                             # checked before any network call
daily_budget_usd = 1.0               # all accounts; 0 stops all paid publishing
monthly_budget_usd = 10.0
max_posts_per_day = 5                # per account; every post of a thread counts
quiet_hours = "23:00-07:00"          # optional; default none
timezone = "UTC"                     # IANA zone for the day/month boundary and quiet hours

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none (path uploads off)
```

| Key | Default | Where it is enforced |
|---|---|---|
| `default_account` | none: the only bound account | [Accounts — Design §3](../../accounts/2_design.md) |
| `accounts.<alias>.expected_handle` | none: the alias's handle only | [Accounts — Design §3](../../accounts/2_design.md) |
| `prices.<provider>.plain_post_usd`, `url_post_usd` | X: 0.015 / 0.20; others: 0 | [specs/policy.md](../specs/policy.md) |
| `policy.*` | as above, no quiet hours | [specs/policy.md](../specs/policy.md) |
| `media.roots` | none | [specs/media-confinement.md](../specs/media-confinement.md) |

`pulsar serve` prints the effective media roots to stderr at startup.
