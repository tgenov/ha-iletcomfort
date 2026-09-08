# Contributing to ha-iletcomfort

Thanks for helping out. This project uses **Conventional Commits** and **release-please** to drive versioning, changelogs, and HACS releases — there is no manual tagging.

## Commit & PR title format

Every PR title (and every commit landing on `main`) **must** start with a Conventional Commits type:

```
<type>[optional scope][!]: <subject>
```

The PR-title check (`.github/workflows/conventional-pr-title.yml`) blocks merges that don't follow this format.

### Types

| Type        | Triggers release | Effect on version (currently 0.x) |
|-------------|------------------|-----------------------------------|
| `feat`      | yes              | minor bump (`0.2.0` → `0.3.0`)    |
| `fix`       | yes              | patch bump (`0.2.0` → `0.2.1`)    |
| `feat!` / `fix!` / `BREAKING CHANGE:` in body | yes | major bump (`0.2.0` → `1.0.0`) |
| `docs`      | no               | —                                 |
| `chore`     | no               | —                                 |
| `ci`        | no               | —                                 |
| `test`      | no               | —                                 |
| `refactor`  | no               | —                                 |
| `style`     | no               | —                                 |
| `perf`      | yes (patch)      | patch bump                        |
| `build`     | no               | —                                 |
| `revert`    | yes (patch)      | patch bump                        |

### Examples

```
feat: add EU region selection to config flow
fix(api): raise AuthError only for 14xxx login codes
docs: document HACS install steps for EU users
chore: bump pytest-homeassistant-custom-component
feat!: drop Python 3.11 support
```

For a breaking change without the `!`, include a `BREAKING CHANGE:` paragraph in the commit body.

## How a release happens

1. Land your PR on `main` with a Conventional Commits title.
2. `release-please` notices the unreleased `feat:` / `fix:` commits and opens (or updates) a **release PR** titled something like `chore(main): release 0.3.0`. That PR contains the version bump in `custom_components/iletcomfort/manifest.json` and a new `CHANGELOG.md` entry.
3. Merge the release PR. `release-please` then:
   - tags the commit as `v0.3.0`
   - creates a GitHub Release with the changelog as the body
4. HACS picks up the new release within ~1 hour and shows installed users an "Update available" prompt.

You never run `git tag` by hand.

## Running tests locally

```bash
# fastest: with uv (matches CI)
uv venv
uv pip install -r requirements_test.txt
uv run pytest tests/

# or with pip
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest tests/
```

CI runs the same suite on every PR via `.github/workflows/tests.yml`.

## Fetching a vendor plugin bundle (reverse-engineering aid)

`scripts/fetch_plugin.py` downloads the vendor's own per-model plugin bundle from the Dollin cloud. For `0xC3` that bundle is a [Weex](https://weex.apache.org/) package — one readable (minified) JS file per app screen — and it is the vendor's source of truth for **control field names, their value domains, and the fault-code table**.

This is a **development tool only**. Nothing under `custom_components/` imports it, and it is never loaded at runtime.

Why it exists: it beats guessing control-frame layouts from status-frame offsets, and it does not need a packet capture. Capture is a dead end on Android anyway — the Android app controls the appliance over **MQTT (TLS :8883)**, which an HTTP proxy cannot intercept (see issue #42). Credit for the technique: @dzerik in issue #48.

### Usage

```bash
export ILETCOMFORT_ACCOUNT='you@example.com'
export ILETCOMFORT_PASSWORD='...'

# look up what is available without downloading
python3 scripts/fetch_plugin.py --model 17100007 --metadata-only

# download and unpack
python3 scripts/fetch_plugin.py --model 17100007 --unpack
```

`--model` takes an **`sn8` model code** (e.g. `17100007`), not a per-device serial. Find yours in a diagnostics download under the `appliance` block. Use `--region eu` for EU accounts.

### How it works

It POSTs to `/v1/product/upgrade/plugin/get/latest` claiming the installed plugin version is `0.0.0`. The server then believes the client is out of date and answers with a `packageUrl`; claim the real version and it correctly reports you are already current and returns **no URL** (`code=0` with metadata but no `packageUrl`).

Two protocol details that are easy to get wrong, both established against the live endpoint:

- the request body must be signed with the **iot-key** prefix (`meicloud`), not the app-key prefix used by other v1 calls — otherwise `code=3301 Signature Failed`;
- the token goes in its own **`accessToken` header**; this endpoint does not accept `Authorization: Bearer` — otherwise `code=1105 ... accessToken[REQUIRE]`.

There is **no ownership check**: any model code can be requested from any account, so you do not need to own the appliance whose bundle you want.

### Common responses

| code | meaning |
|---|---|
| `0` + `packageUrl` | bundle available |
| `0`, no `packageUrl` | the claimed version is already current — request `0.0.0` |
| `2200004` | the product exists but has **no plugin registered** for this region/tenant |
| `2200007` | the product does not exist in this region's catalogue |
| `1105` | no `accessToken` header |
| `3301` | wrong signing prefix |
| `14005` | token rejected (see the single-session note below) |

### Credentials, privacy, and the login war

- Credentials are read **only** from `ILETCOMFORT_ACCOUNT` / `ILETCOMFORT_PASSWORD`. There is deliberately no command-line option for them, so they cannot leak through shell history or `ps`.
- Nothing the script prints or writes contains the account, password, access token, account UID, or the presigned download URL — that URL embeds AWS SigV4 credentials and is always shown as `<redacted>`. This matches the PII discipline in `diagnostics.py`.
- The access token is cached (`--token-file`, default `~/.iletcomfort_plugin_token.json`) so repeat runs do not log in again.
- **The cloud allows one active session per account** (`AGENTS.md` §3). Logging in invalidates the session held by the iLetComfort app *and* by a running Home Assistant instance. Prefer a separate account, or expect HA to re-auth.

### Do not commit bundles

Downloads land in `plugin-bundles/`, which is gitignored. Keep it that way: the bundles are the vendor's proprietary app code and contain hardcoded third-party credentials.
