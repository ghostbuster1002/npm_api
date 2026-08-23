# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### ⚠️ Breaking Changes

Three changes require action before upgrading. Each is repeated in the
relevant section below.

1. **`host ssl-enable` takes a certificate ID first and a host selector, not
   two positional arguments.** `host ssl-enable 42 123` becomes
   `host ssl-enable 123 --ids 42`. It also no longer forces `ssl_forced` and
   `http2_support` to true; only the certificate changes.
2. **`host bulk-add-domain` and `host bulk-remove-domain` refuse to run without
   a host selector.** They previously fell through to every host.
3. **`host split`, `host ssl-enable` and `host bulk-update` exit non-zero when
   any host fails.** Scripts that ignored the exit status will now see failures.

### Added

- `host split <glob> --cert <id>` — move domains matching a glob out of
  existing hosts onto brand-new hosts carrying the given certificate. Solves
  the case of one host answering to both an internal and a public name: NPM
  renders one nginx `server` block per host with a single `ssl_certificate`, so
  such a host can only present one certificate and the other name throws an SSL
  error. Takes `--ids` / `--pattern` / `--interactive`, `--preview/--no-preview`
  and `-y`.
  - The source keeps its unmatched domains and its existing certificate; split
    never changes the source's cert.
  - Every other setting — websockets, force SSL, HSTS, custom locations,
    advanced config, access list — is copied to the new host verbatim.
  - Hosts with fewer than two domains, or where the glob matches none or all of
    their domains, are skipped with a warning rather than failing the run.
  - Domain collisions against all existing hosts are checked up front.
  - The source's domain list is trimmed *before* the new host is created, so
    the two never hold the same domain simultaneously (NPM rejects duplicates).
    A failed create rolls the source back; a failed rollback prints the
    original domain list so it can be restored by hand.
- `host clone <id> --domain <d> [--domain <d> ...]` — copy a host to new
  domains, leaving the source untouched. `--domain` is required and repeatable
  because NPM requires unique domain names. `--cert` is optional and inherits
  the source's certificate when omitted; pass `none` for no certificate.
  `--forward-host` and `--forward-port` override the copied backend. Wildcard
  domains and domains already claimed by another host are rejected.
- `--json` on `host list`, `host show`, `cert list` and `cert show` — unstyled
  JSON on stdout, pipeable into `jq`.
- `host update <id> field=value` now handles list fields and JSON literals:
  `domain_names=a.lan,b.lan` splits on commas, and any value may be given as a
  JSON literal, e.g. `locations='[...]'`. Free-text fields such as
  `advanced_config` keep their commas.
- `host show` now displays HSTS settings (including subdomains) and custom
  locations, which it previously omitted entirely.
- Certificate assignments are validated: setting `certificate_id` through
  `host bulk-update` or `host ssl-enable` checks that the certificate exists,
  reports its expiry, and warns about host domains it does not cover.

### Changed

- **BREAKING — `host ssl-enable` signature.** Was
  `ssl-enable <host_id> <cert_id>`, a single host by positional argument. It is
  now `ssl-enable <cert_id> [--ids <csv> | --pattern <p> | --interactive]
  [--preview/--no-preview] [-y]`, a thin alias for
  `host bulk-update certificate_id <id>`.
- **BREAKING — `host ssl-enable` no longer forces `ssl_forced` and
  `http2_support` to true.** The old version silently set both on every
  invocation. Only the certificate changes now; set the other flags explicitly
  with `host bulk-update ssl_forced true` if you want them.
- **BREAKING — `host bulk-add-domain` and `host bulk-remove-domain` require a
  host selector.** They previously fell through to every host, so a bare
  `host bulk-remove-domain com` would have rewritten the entire estate.
  `--ids`, `--pattern` (where supported) or `--interactive` is now required.
- **BREAKING — `host split`, `host ssl-enable` and `host bulk-update` exit
  non-zero when any host fails.** They previously always exited 0, making
  partial failures invisible to scripts.
- `--pattern` on `host bulk-update` (and the new `host split` / `host
  ssl-enable`) accepts a glob as well as a plain substring, so `*.internal.lan` and
  `internal.lan` both work.
- `--ids` on those same commands reports unparseable input and warns about IDs
  that match no host, instead of raising an unhandled error.
- Host create/update payloads are now built by exclusion rather than from a
  fixed allowlist, so fields added by newer NPM releases survive a clone or an
  update instead of being silently reset.

### Fixed

- Certificate expiry status was read from a `cert["expired"]` field that NPM's
  API does not return, so every certificate always displayed as VALID. Status
  is now derived from `expires_on`, with a 30-day warning tier. Affected
  `cert list`, `cert show` and the `info` dashboard.
- `cert generate` could never renew an expired certificate: the same missing
  `expired` key made its "a valid certificate already exists" guard always
  true, so it refused and returned. It now renews, and an unreadable expiry
  falls through to regeneration rather than blocking.
- Authenticated API calls had no timeout, so a hung NPM would block forever.
  They now time out after 30 seconds.
- `--no-preview` was a no-op. The condition `if preview or not yes` meant it
  only suppressed output when combined with `-y`, at which point nothing would
  have printed anyway.
- `host update` and `host bulk-update` now share one value parser, which
  matches integers strictly. A malformed numeric value such as `--5` can no
  longer reach `int()` and blow up with an uncaught `ValueError`; it is sent as
  a string instead.
- `update_host` did not include `trust_forwarded_proto` (added by NPM 2.15), so
  it was absent from update payloads.

### Removed

- `NPMClient.enable_host_ssl` — dead after the `ssl-enable` rewrite, and it
  force-set `ssl_forced` and `http2_support` as a side effect.
