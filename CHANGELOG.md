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
4. **`host clone` and `host split` now refuse a certificate that does not
   exist** instead of printing the warning and continuing. A script that
   cloned or split against a deleted certificate used to "succeed" and produce
   hosts with no TLS listener; it now exits 1. Pass `--cert none` to get the
   old outcome deliberately.

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
- `restore <backup>` — rebuild proxy hosts, access lists and settings from a
  backup. Takes either the `full_config_*.json` file or a directory holding
  one, following `full_config_latest.json` and falling back to the newest file
  if that symlink is stale. Built for a freshly set up NPM; pointed at a
  populated one it deletes the existing hosts and access lists first, says how
  many, and names the backup to take before doing so.
  - **Certificates are matched, never written.** Restoring them would mean
    reading private keys off disk and POSTing them to an endpoint that is plain
    HTTP by default. Each backed-up certificate is matched against one already
    installed in the target, on its *set of domain names* rather than on
    `nice_name`, which is free text. The name is the fallback whenever the
    domain match finds nothing on either side — most often an uploaded
    certificate in the target whose `domain_names` NPM never recorded.
  - IDs are never carried across — NPM assigns them on create, so a backup's
    `certificate_id` means nothing elsewhere. A host whose certificate has no
    match comes back with `certificate_id`, `ssl_forced` and `hsts_enabled` all
    cleared, and is named in both the preview and the closing summary.
  - Access lists are recreated before hosts, since hosts carry
    `access_list_id`; an access list that fails to create leaves the hosts
    referencing it with access control dropped rather than pointing at nothing.
    An entry whose password is absent from the backup is named so it can be set
    again by hand.
  - **Users are not restored.** NPM's API never exports password material, so
    they could only be recreated with invented passwords.
  - Settings are written only for IDs the target already defines, so a backup
    from a later NPM release cannot introduce settings the instance has never
    had. Skipped ones are named. In NPM 2.x this is `default-site` alone.
  - Existing hosts are deleted before existing access lists — NPM will not drop
    an access list a host still references.
  - Before the first delete, the target's current hosts, access lists and
    settings are written to `pre_restore_<timestamp>.json` in the backup
    directory at mode `0600`. A restore that cannot write it refuses to run.
- `host merge --into <id>` — the inverse of `host split`. Folds several hosts
  into one and deletes the sources. The `--into` host is kept whole and
  supplies every setting; the others contribute only their domain names. Takes
  `--ids` / `--pattern` / `--interactive`, `--cert`,
  `--allow-different-targets`, `--preview/--no-preview` and `-y`.
  - Merging is structurally how a host ends up answering to names its single
    certificate does not cover, so the resulting domain list is validated
    against that certificate and every uncovered name is reported. A
    certificate that does not exist stops the merge.
  - The `--into` host is never deleted, even when `--pattern` matches it too.
  - A source forwarding to a different scheme, host or port stops the merge:
    its domains would silently start reaching another backend.
    `--allow-different-targets` overrides.
  - The preview names, per source, which settings differ from the target and
    will therefore change behaviour for the domains moving across.
  - `--cert none` also clears `ssl_forced` and `hsts_enabled`, since forcing
    SSL without a certificate redirects to a listener NPM never renders.
  - Before the first delete, every host involved is written to
    `pre_merge_<id>_<timestamp>.json` in the backup directory at mode `0600`.
    A merge that cannot write that file refuses to run.
  - Sources are deleted one at a time, each followed immediately by adding its
    domains to the target — NPM will not let two hosts hold the same domain, so
    the delete has to come first. A rejected update recreates the source, under
    a new ID; a failed recreate prints the snapshot path.
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
- A test suite, `test_npm_api.py`, covering the pure helpers, the shared bulk
  infrastructure, and the backup and dashboard paths against stubbed clients.
  171 cases, standard library only —
  no pytest or other dev dependency, so any machine that can run `npm-api` can
  run its tests. No network and no live NPM required. `make test` runs it, and
  `make build` runs it after installing dependencies. `npm_api.py` remains a
  single self-contained file and is not needed to run the suite from elsewhere.
- `backup --output/-o <dir>` writes the backup to a chosen directory instead of
  the configured data directory.
- `backup --include-keys` downloads certificate private keys. Without it the
  backup holds configuration and certificate metadata only, and now says so
  explicitly. With it, the command states that the output contains unencrypted
  key material and where it landed.
- Certificates that NPM's API does not export are named individually, along
  with the `docker cp` command that fetches each from the container filesystem.
  The path is chosen from the certificate's `provider`:
  `/etc/letsencrypt/live/npm-<id>` for `letsencrypt`, `/data/custom_ssl/npm-<id>`
  otherwise, and both are offered when NPM reports no provider at all. These do
  not fail the backup, since a certificate NPM will never export would break
  every scheduled run.

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
- **BREAKING — every bulk command exits non-zero when any host fails.** That is
  `host split`, `host ssl-enable`, `host bulk-update`, `host bulk-add-domain`,
  `host bulk-remove-domain` and `host bulk-replace-domain`. They previously
  always exited 0, making partial failures invisible to scripts.
- **BREAKING — `host bulk-replace-domain` requires a host selector.** It
  previously defaulted to every host carrying the old domain, so a short
  argument like `com` would have rewritten the whole estate. Pass
  `--pattern <old_domain>` for the old behaviour.
- **BREAKING — `user create` no longer takes the password as a positional
  argument.** It is prompted for, with confirmation, or passed via
  `--password`. A positional password lands in shell history and is readable
  in `ps` output by every other user on the machine while the command runs.
- `bulk-update certificate_id 0` and `access_list_id 0` now clear the link
  instead of trying to look up a certificate with ID 0 and failing. `0`,
  `none` and `null` all mean the same thing, matching what `--cert 0` already
  did for `host split` and `host clone`.
- **Diagnostics now go to stderr; only a command's own output goes to stdout.**
  Previously every message shared stdout, so a warning or the
  "Configuration Required" banner landed in the middle of `--json` output and
  broke `jq` with a parse error. Tables and detail blocks still go to stdout,
  so `host list | grep` keeps working.
- Authentication and connection failures report one clear line instead of a
  Rich traceback: wrong credentials name the host and the env vars to check, an
  unreachable NPM says so plainly rather than printing urllib3's nested
  exception repr.
- API errors report the message NPM sent rather than requests' generic repr,
  so a rejected write says `HTTP 400: Domain already in use` instead of
  `400 Client Error: Bad Request for url: ...`.
- `--json` now covers `info`, `host search`, `user list`, `acl list` and
  `acl show`, alongside the host and certificate commands that already had it.
- `host bulk-remove-domain` and `host bulk-replace-domain` gained `--pattern`.
  `bulk-remove-domain` already named the option in its error message without
  accepting it. All six bulk commands now take the same
  `--ids / --pattern / --interactive / --preview / -y` set.
- **BREAKING — `backup` no longer downloads certificate private keys by
  default.** Pass `--include-keys` to restore the old behaviour. Note that the
  old behaviour largely did not work; see Fixed.
- **BREAKING — `backup` exits non-zero when a section fails**, naming the
  sections that could not be written. It previously printed a warning and
  exited 0, so a cron job recorded a partial backup as a success.
- **BREAKING — the minimum supported Python is now 3.10**, up from an
  advertised 3.8. Both 3.8 and 3.9 are past end of life.
- `NPMClient.full_backup()` returns a `BackupResult` (`path`, `failures`,
  `key_failures`, `complete`) rather than a path string, and
  `NPMClient.download_certificate()` returns the list of files it wrote and
  raises `CertificateDownloadError` rather than returning a bare boolean.
  Both matter only if you import the module rather than using the CLI.
- `--pattern` on `host bulk-update` (and the new `host split` / `host
  ssl-enable`) accepts a glob as well as a plain substring, so `*.internal.lan` and
  `internal.lan` both work.
- `--ids` on those same commands reports unparseable input and warns about IDs
  that match no host, instead of raising an unhandled error.
- Host create/update payloads are now built by exclusion rather than from a
  fixed allowlist, so fields added by newer NPM releases survive a clone or an
  update instead of being silently reset.

### Fixed

- `host merge` and `host clone` now also warn when the resulting host would
  answer to more than one base domain, checked against the domain names
  themselves rather than against the certificate's metadata. The existing
  coverage check consults `domain_names` on the certificate, which NPM keeps as
  free-form metadata it never uses when serving — for uploaded certificates it
  is routinely unusable, so the check degraded to a dim "coverage not verified"
  note printed directly beneath a green `VALID`. That is precisely the case
  where merging recreates the dual-domain fault `host split` exists to undo, so
  the reassuring output appeared exactly when the warning was most needed. The
  new check needs no certificate metadata and is advisory: a multi-SAN
  certificate spanning several bases is legitimate.
- The certificate status line now reads `expiry: ✅ VALID` rather than a bare
  `✅ VALID`, which sat immediately above the coverage warnings and read as an
  endorsement of the whole assignment rather than a statement about the date.
- `warn_on_mixed_bases` folded no case, so one base domain spelled two ways —
  `App.Example.com` and `api.example.com` — was reported as *"2 unrelated base
  domains: Example.com, example.com"*. DNS is case-insensitive and NPM stores
  whatever was typed, and `dedupe_domains` keeps the first spelling rather than
  a normalised one, so mixed spellings of one base are routine. Case is now
  folded at the comparison, as it already was everywhere else in the tool; the
  false alarm mattered because the warning's value depends on firing rarely.
- `host clone` and `host split` called `validate_certificate_assignment` but
  ignored its return value, so the one case it treats as fatal — a certificate
  ID that no longer exists — printed a red refusal and then went ahead anyway.
  Both now exit 1. This is the failure mode the tool was written to catch: NPM
  wraps the entire `listen 443 ssl` block in a conditional on the linked
  certificate, so a host pointed at a deleted ID is rendered with no TLS
  listener and reports no error. Found while adding `host merge`, which
  inherited the same omission and would have deleted the source hosts before
  producing the broken survivor.
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
- `update_host` and `create_host` built their payloads from hardcoded
  allowlists, so any field a newer NPM adds was silently reset on every write.
  `update_host` now shares the copy-by-exclusion path used for clones, and
  `create_host` sends `trust_forwarded_proto`. NPM's expanded `certificate`,
  `owner` and `access_list` objects are excluded, so a host fetched with
  expansions can be written back without sending an object where an ID belongs.
- `host show` omitted `trust_forwarded_proto`.
- `info` and `check-token` exited 0 when authentication failed, so a scheduled
  health check could not tell a working NPM from an unreachable one.
- Assigning a certificate whose `domain_names` NPM reports as null crashed with
  `TypeError: can only join an iterable`. `dict.get` returns `None` rather than
  the default when a key is present holding null. This was inside the guard
  that refuses to point a host at a certificate which cannot serve it, so the
  guard aborted with a traceback instead of warning.
- `info` reported 0 for any dashboard section whose request failed, making a
  sick NPM render identically to an empty one. Unreadable sections now show `?`
  (`null` under `--json`), the reasons are listed, and the command exits
  non-zero — so a count of 0 now only ever means zero.
- **Certificate backups failed silently.** `download_certificate` wrapped both
  of its download routes in `except Exception: pass` and returned `False`,
  which `full_backup` ignored — so `backup` reported "Backed up N certificates"
  over directories containing nothing but `certificate_meta.json`. Failures are
  now raised, named per certificate, and the success line no longer claims more
  than metadata was written.
- `download_certificate` wrote an empty `.key` file when NPM answered 200 with
  no key material, which reads as a successful backup of an unusable key. That
  response is now reported as a failure.
- The zip-slip guard compared resolved paths with `str.startswith`, which
  accepts a sibling directory sharing the prefix (`/backup` vs
  `/backup-evil`). It now uses `Path.is_relative_to`. Python's
  `ZipFile.extract` sanitises member paths on its own, so this was a weak
  second line of defence rather than a live escape.
- Private keys and API tokens were written and then `chmod`ed to 600, leaving
  them world-readable in between under a default umask. They are now created
  with mode 600 via `os.open`. Keys extracted from the legacy ZIP route were
  never chmodded at all and kept whatever mode the archive carried.
- `host bulk-add-domain` dropped subdomain labels. It took only the first label
  as the prefix, contradicting its own comment, so a host holding
  `sub.ex.old.com` gained `sub.new.com` instead of `sub.ex.new.com`. Apex names
  such as `old.com` no longer produce a nonsensical `old.new.com` either; they
  are skipped, having no prefix to carry over.
- `host bulk-replace-domain` could store the same domain twice on one host,
  when the rewrite landed on a name the host already carried. Duplicates are
  now dropped and the affected hosts named in the preview.
- `--ids` raised an unhandled `ValueError` on non-numeric input in the three
  bulk domain commands; they now share the parser that reports it properly and
  warns about IDs matching no host.
- The bulk domain apply loops caught bare `Exception`, so a bug in the client
  was indistinguishable from an API rejection. They now catch
  `requests.HTTPError` like the rest of the bulk commands.
- `full_backup` crashed on its own `full_config_latest.json` symlink if the
  backup it pointed at had been pruned: `Path.exists()` follows the link, so a
  dangling one read as absent and `symlink_to` then raised `FileExistsError`.

### Removed

- `NPMClient.enable_host_ssl` — dead after the `ssl-enable` rewrite, and it
  force-set `ssl_forced` and `http2_support` as a side effect.
- `NPMClient.check_connection` — never called from anywhere, and `requests`
  already surfaces connection failures with a clearer message.
