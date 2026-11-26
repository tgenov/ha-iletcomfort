# Release & Change Management Guide

This integration is distributed via **HACS** to Home Assistant installations that rely on stable entity IDs, automations, and Recorder / Energy statistics.

## Backwards-Compatibility Invariants

1. **Entity Registry & Recorder immutability:** Never change an existing entity's `unique_id`, `device_class`, `state_class`, or `native_unit_of_measurement` in place. All existing contracts are pinned in `tests/test_hardware_corpus.py`.
2. **Additive entity gating:** Never delete already-registered entities for existing users; use `entity_registry_enabled_default=False` or additive capability gating for model-specific sensors.
3. **Config entry rollback safety:** New persisted keys in `entry.data` or `entry.options` must remain readable alongside legacy keys so users can roll back cleanly across minor versions.
4. **Hardware-corpus gate:** Every internal refactor (`refactor:`) must pass both the full unit suite and `tests/test_hardware_corpus.py` with zero behavior change.

## Standard Releases (`release-please`)

- Conventional Commits on `main` drive versioning automatically (`feat:` → minor, `fix:` → patch, `refactor:`/`test:`/`ci:`/`docs:` → no bump).
- Because `release-please` opens bot-authored PRs under `GITHUB_TOKEN`, branch protection (`enforce_admins`) requires closing and reopening the release PR as a maintainer to trigger CI checks before merging (or configuring a GitHub App token for `release-please.yml`).

## HACS Pre-Release (`beta`) Flow for Hardware-Touching Changes

Changes that introduce a **new write dialect** (e.g., ATW `C3Basic` SET) or **new optional subtype polling** should ship as a HACS pre-release first so reporters can validate on live hardware before general users auto-update:

1. Merge the feature behind an opt-in or single-probe fallback to `main` (or cut from a release branch).
2. Create a GitHub **Pre-release** tagged `v0.X.0-beta.1` (check **"Set as a pre-release"** in GitHub Releases).
3. Ask reporters to enable **Show beta versions** in HACS → iLetComfort → Redownload and report diagnostics + app confirmation.
4. Once live hardware confirms the change, merge the `release-please` PR to publish the stable `v0.X.0` release.
