# Setting pulsar up with a human

Read this when pulsar is not installed, no account is bound, or `pulsar.status`
reports a login, storage or config problem.

pulsar publishes to X as accounts a human binds on one host, the *posting host*.
Setting it up needs a browser, the X developer portal and a login, so the human
does those steps. Your job is to know the order, give the exact command for each
step, explain what the human will see, check the result and diagnose failures.

## Rules

- **Never ask for, accept or repeat a credential.** No token, key, password or
  client secret goes into the chat, a file you write, or a command you run. If
  the human pastes one, tell them to rotate it. The OAuth *client id* is public,
  but let the human type it into their own terminal.
- **Do not open the home's files** (`key`, `tokens.enc`, `accounts.json`,
  `ledger.sqlite3`). Ask the human to run a command and paste its output.
- **Nothing live without a yes.** `pulsar auth status --live` rotates the token
  pair, and every post costs money (about $0.015, or $0.20 with a URL). Ask first.
- **One posting host.** Two hosts sharing a copied home kill each other's tokens,
  because X rotates the refresh token on every use. Retire the old copy before
  binding on a new host.
- Confirm each step worked before you move to the next.

## 0. Find out where they are

Ask which host will post, and whether the Orbit plugin, the MCP server or only the
CLI will be used. Then check what already works:

| Check | Tells you |
|---|---|
| `orbit pulsar status` | the plugin is installed; its `attention` list names what is missing |
| `uv run pulsar auth status` (in the checkout) | the CLI runs, and which accounts are bound and healthy |

Skip the steps that are already done.

## 1. Prerequisites

On the posting host: Python 3.12+, [uv](https://docs.astral.sh/uv/), git, and a
browser the human can reach. That browser can be on their laptop, with the
callback forwarded over SSH (step 5). The plugin needs Orbit 0.24 or newer.

## 2. Install the code

```bash
git clone https://github.com/constellation-works/pulsar.git
cd pulsar && uv sync
uv run pulsar
```

`pulsar` alone lists the commands.

## 3. Choose the home

The home holds the encrypted tokens, `config.toml` and the ledger. Every surface
that shares a home shares one ledger, and so one budget and one duplicate guard.

- **With the Orbit plugin:** install the plugin first (step 7), then use its home,
  `~/.orbit/state/plugins/pulsar/home`. Every CLI command then needs
  `PULSAR_HOME` set to that path, or it acts on a different ledger.
- **Without it:** the default, `~/.config/pulsar`, or any directory named by
  `PULSAR_HOME`.

pulsar creates the home as owner-only. It refuses a home that other users can
read or write, or that is a symlink (`insecure_storage`), and prints the fix. It
never changes permissions itself.

## 4. The X developer app (human, in a browser)

On the X developer portal, the human creates an app, or opens the existing one,
and sets up **OAuth 2.0 user authentication**:

- **Type of app:** *Native App*. This is a public client with no client secret.
  A confidential client (*Web App*) fails the token exchange with HTTP 401.
- **Callback URI:** exactly `http://127.0.0.1:8976/callback`.
- **App permissions:** read and write.
- Posting is billed per post; the account's API access must allow writes.

They copy the **client ID**. pulsar never needs a client secret.

## 5. Bind the account (human, in a terminal)

Aliases are `provider:handle`, for example `x:constworks`. The human signs in to X
in their browser **as that account** first, because pulsar refuses a token that
belongs to anyone else (`account_mismatch`).

On the posting host:

```bash
uv run pulsar auth login --account x:<handle> --client-id <CLIENT_ID>
```

Over SSH, forward the callback port from the laptop, and print the URL instead of
opening a browser:

```bash
ssh -L 8976:127.0.0.1:8976 <posting-host>
cd pulsar && PULSAR_HOME=<home> uv run pulsar auth login --account x:<handle> --client-id <CLIENT_ID> --no-browser
```

The human opens the printed URL, approves, and the browser lands on the
callback. pulsar asks X who owns the new token and stores it only if the handle
matches. Later logins remember the client id.

| What they see | Cause and fix |
|---|---|
| `account_mismatch` | the browser was signed in as another account: switch accounts, log in again |
| `token exchange failed (HTTP 401)` | the app is not a *Native App*, or the callback differs from step 4 |
| `timed out waiting for the browser redirect` | the callback never reached the host: check the `ssh -L` forward and the callback URI |
| the login cannot listen on port 8976 | something else holds it (`ss -ltnp` shows what); stop it and retry |
| `insecure_storage` | run the `chmod` in the message, or set `PULSAR_HOME` to the real path it names |
| `credentials_unreadable` | the key no longer matches the stored tokens: restore the key; log in again only if it is gone for good |

## 6. Configure (optional)

Settings live in `config.toml` in the home. Every key has a default: $1 a day,
$10 a month, 5 posts per account per day, no quiet hours and no media roots.
Draft the file for the human, and have them save it and check its mode. It must
be their own regular file, not writable by group or other users (`chmod go-w`).

```toml
default_account = "x:<handle>"

[accounts."x:<handle>"]
expected_handle = "<handle>"          # pin: a token for any other handle is refused

[policy]
daily_budget_usd = 1.0
monthly_budget_usd = 10.0
max_posts_per_day = 5
timezone = "UTC"

[media]
roots = ["/path/to/media"]            # needed before the MCP server uploads files by path
```

A bad key or value fails with `invalid_config`, which names the file and the key.

## 7. Connect the front ends

**Orbit plugin**: install from a clean export. Orbit installs its
`.orbit-plugin/` directory, which contains the committed runtime package.

```bash
rm -rf /tmp/pulsar-plugin && mkdir /tmp/pulsar-plugin
git -C <checkout> archive agent-main | tar -x -C /tmp/pulsar-plugin
orbit plugin add /tmp/pulsar-plugin --enable --grant 'fs={{workspace}},{{plugin_state}}' --grant network
```

If it is already installed, use
`orbit plugin upgrade pulsar /tmp/pulsar-plugin` with the same `--grant` flags.
The first call syncs dependencies and takes about 10 seconds.

**MCP server**: register it with the client, pointing it at the same home:

```bash
claude mcp add pulsar -e PULSAR_HOME=<home> -- uv --directory <checkout> run pulsar serve
```

## 8. Verify

```bash
PULSAR_HOME=<home> uv run pulsar auth status
orbit pulsar status
```

`auth status` exits 0 when every account is healthy; it reads local state only.
To prove the refresh works end to end, `pulsar auth status --live` refreshes the
token and asks X who it belongs to. That rotates the token pair, so ask first.

Then validate a one-post plan offline. Nothing is sent:

```bash
PULSAR_HOME=<home> uv run pulsar validate plan.yaml
```

From here, drafting and publishing follow [the skill](../SKILL.md). The first
live post is the human's call: `pulsar publish plan.yaml --confirm` costs money.

## After an upgrade

A newer pulsar may need the home brought up to date. Report first, then apply:

```bash
PULSAR_HOME=<home> uv run pulsar migrate
PULSAR_HOME=<home> uv run pulsar migrate --confirm
```

An upgraded ledger is refused by an older pulsar, so apply it only once every
surface on the host runs the new version.
