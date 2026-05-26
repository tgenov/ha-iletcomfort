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
