# Semantic parser compatibility matrix

The versioned machine-readable source is [`catalogues/semantic-compatibility-v1.json`](catalogues/semantic-compatibility-v1.json). It records the survey completed on 2026-09-12 using the development extractor/probe workflow from #82/#83 and the repository's reviewed hardware evidence.

| Model/profile | Bundle evidence | Static semantic coverage | Parser evidence | Result |
|---|---|---|---|---|
| Unknown `sn8` / STANDARD | Untested: no model identity | None | Untested | Keep validated local STANDARD decoding. Semantic support cannot be selected safely. Relevant to #47 when model metadata is recovered. |
| `171000AU` / AQUAPURA | Android 1.7.0 plugin `2024032801` reported in epic evidence (package region unknown); US catalogue lookup returns `2200007`; authoritative unpacked artifact absent | Reported semantic names/selectors and constraints remain unverified in this workspace; #84 is blocked | Parser-generated temperature encode is reported by epic evidence and paired with hardware validation of frame shape; repository vectors pending #85. Decode untested. | Promising evidence, not runtime-ready. Local AQUAPURA profile remains authoritative. |
| `171H120F` / ATW | US: `2200004` (product exists, no registered plugin); EU unverified because no EU-account result is available | None | Unavailable | Cannot benefit from this parser workflow today. #42 still needs an official-app command capture; neither `0xC3` nor AQUAPURA semantics imply compatibility. |
| `17100003` / KJRH120L | US: `2200004`; EU unverified because no EU-account result is available | Hardware-validated local short DHW operations only; no extracted semantic catalogue | Unavailable | Preserve local gated behavior. The unsafe `0x08` Zone-1 experiment must not be revived. |
| `17100007` / STANDARD default | US bundle `0xC3` v1.0.65 fetched from the authorized account | Extractor found 28 direct `luaControl`/control sites, all with dynamic parameter assembly; no selector or field was asserted | Untested: parser endpoint was not recovered | This is a real extraction result and an explicit coverage gap. No hardware profile or cross-model equivalence is established. |

## Evidence categories

- `bundle_lookup` records only region, plugin version when returned, and sanitized vendor response category.
- `static_extraction` records only facts recovered by the extractor from an authorized bundle; dynamic expressions remain unresolved.
- `epic_81_established_evidence` records facts already established in the epic but not independently reproducible from an artifact available to this workspace.
- `parser_generated` means the undocumented semantic service produced an output; it is still untrusted until local validation.
- `hardware_validation` means an appliance observation confirmed a narrow behavior. It does not generalize across models.
- `local_capture` refers to existing sanitized regression fixtures and does not imply plugin availability.

No row infers compatibility from device type `0xC3`. Shared-operation comparison can only be generated once two reviewed catalogues exist; currently #84 is blocked on its authorized source artifact and `17100007` has only dynamic unresolved calls. That absence is a survey result, not permission to merge field lists by name. Parser endpoint configuration is unresolved for every row and therefore no network probe was attempted.

## Coverage gaps and issue impact

- #42 (ATW writes): semantic parsing cannot unblock it because the surveyed US tenant returns `2200004` for `171H120F`; the EU tenant remains unverified. Retain write refusal until captured vendor commands exist.
- #47 (unknown Dantex layout): read-only semantic decode could help only after `sn8`, plugin availability, and parser support are established. Until then, template sensor bytes stay untrustworthy and STANDARD must not be replaced speculatively.
- AQUAPURA: the parser-generated temperature evidence can support #85's offline vector work, but the missing authoritative #84 catalogue prevents architecture adoption.
- `17100007`: fetch/extract/probe is useful future work, but without hardware validation it must remain on the default-safe STANDARD profile.

## Regeneration

For each model and authorized region:

1. Set `ILETCOMFORT_ACCOUNT` and `ILETCOMFORT_PASSWORD` from an approved secret store (never shell history), then run `python3 scripts/fetch_plugin.py --model <sn8> --region <region> --out-dir /tmp/iletcomfort-plugins --token-file /tmp/iletcomfort-token.json --metadata-only`; record only `available`, `2200004`, `2200007`, or another sanitized response category.
2. If available, fetch and unpack locally with `python3 scripts/fetch_plugin.py --model <sn8> --region <region> --out-dir /tmp/iletcomfort-plugins --token-file /tmp/iletcomfort-token.json --unpack --save-metadata`, then run `python3 scripts/extract_semantic_api.py /tmp/iletcomfort-plugins/<unpacked> --model <sn8> --plugin-version <version> --output /tmp/<sn8>-catalogue.json`. Never commit the bundle or signed URL.
3. Recover and review a credential-free HTTPS parser base/path from the authorized bundle before probing. Then run `python3 scripts/probe_semantic_parser.py encode-query --sn8 <sn8> --base-url https://<reviewed-host> --encode-path <path> --input /tmp/sanitized-query.json` (or `decode` with a sanitized frame). It cannot send appliance controls and prints structural summaries only.
4. Add a new catalogue/version rather than overwriting provenance. Compare shared operations only from reviewed catalogues and review matches for false equivalence.
5. Update the JSON matrix's survey date and evidence categories; keep unavailable and untested states explicit.

No live lookup or appliance command is part of automated regeneration. Credentials, tokens, full serials, endpoint signatures, and private telemetry must remain outside Git and console output.
