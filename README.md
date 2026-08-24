# NPM-API

[![Build and Release](https://github.com/ghostbuster1002/npm_api/actions/workflows/build.yml/badge.svg)](https://github.com/ghostbuster1002/npm_api/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A Python CLI tool for managing [Nginx Proxy Manager](https://nginxproxymanager.com/) via its API.

**Features:**
- 🌐 Manage proxy hosts (create, update, delete, enable/disable)
- 🔒 SSL certificate management (Let's Encrypt, including wildcards)
- 👥 User management
- 🛡️ Access list management
- 📦 Bulk operations (add/remove/replace domains across multiple hosts)
- ✂️ Split, clone and merge hosts so each domain can carry its own certificate
- 🧾 JSON output (`--json`) for scripting and `jq`
- 💾 Full backup of hosts, certificates, access lists, users and settings, and
  restore of the parts that can be safely rebuilt
- 🔐 Secure credential handling via environment variables or config files

## Installation

### Download Pre-built Binary

Download the latest release for your platform from [Releases](https://github.com/ghostbuster1002/npm_api/releases):

```bash
# Linux
wget https://github.com/ghostbuster1002/npm_api/releases/latest/download/npm-api-linux-amd64
chmod +x npm-api-linux-amd64
sudo mv npm-api-linux-amd64 /usr/local/bin/npm-api

# macOS
wget https://github.com/ghostbuster1002/npm_api/releases/latest/download/npm-api-macos-amd64
chmod +x npm-api-macos-amd64
sudo mv npm-api-macos-amd64 /usr/local/bin/npm-api
```

### Build from Source

```bash
git clone https://github.com/ghostbuster1002/npm_api.git
cd npm_api
make build
sudo make install
```

`make build` installs dependencies, runs the test suite, then builds. To run the
tests on their own:

```bash
make test                          # or:
python3 -m unittest discover -v    # or:
python3 test_npm_api.py
```

The suite uses the standard library only — no pytest, no other dev dependency —
so any machine that can run `npm-api` can run its tests. It needs no network and
no live NPM: it imports `npm_api.py` directly and stubs the API client.

`npm_api.py` stays a single self-contained file and can still be copied to a
machine on its own; the test file is only needed in the repo.

## Configuration

Configure credentials using **environment variables** (recommended) or a **config file**.

### Environment Variables (Recommended)

```bash
export NPM_API_HOST="192.168.1.100"
export NPM_API_PORT="81"
export NPM_API_USER="admin@example.com"
export NPM_API_PASS="your_password"
```

### Config File

Create `~/.config/npm-api/npm-api.conf`:

```ini
NGINX_IP="192.168.1.100"
NGINX_PORT="81"
API_USER="admin@example.com"
API_PASS="your_password"
```

Config file locations (searched in order):
1. `./npm-api.conf`
2. `~/.config/npm-api/npm-api.conf`
3. `/etc/npm-api/npm-api.conf`
4. `<directory of the script or binary>/npm-api.conf`

## Quick Start

```bash
# Show dashboard
npm-api info

# List all proxy hosts
npm-api host list

# Create a new proxy host
npm-api host create example.com -i 192.168.1.10 -p 8080

# Generate SSL certificate
npm-api cert generate example.com

# Assign certificate 123 to host 42
npm-api host ssl-enable 123 --ids 42

# Move the internal names off host 42 onto a host of their own, with its own cert
npm-api host split '*.internal.lan' --cert 5 --ids 42

# Bulk add a domain to multiple hosts
npm-api host bulk-add-domain newdomain.com --interactive
```

## Commands

```
npm-api --help              Show all commands
npm-api info                Dashboard and configuration info
npm-api backup              Full backup of all configurations
                            (add --include-keys to capture private keys)
npm-api restore <backup>    Rebuild hosts, access lists and settings
                            from a backup

npm-api host --help         Proxy host management
npm-api cert --help         SSL certificate management
npm-api user --help         User management
npm-api acl --help          Access list management
```

### Host Commands

| Command | Description |
|---------|-------------|
| `host list [--json]` | List all proxy hosts |
| `host show <id> [--json]` | Show host details |
| `host search <pattern>` | Search hosts by domain |
| `host create <domain> -i <ip> -p <port>` | Create new host |
| `host clone <id> --domain <domain>` | Copy a host to new domains |
| `host split <glob> --cert <cert_id>` | Move matching domains onto new hosts |
| `host merge --into <id>` | Fold hosts into one, deleting the sources |
| `host delete <id>` | Delete a host |
| `host enable <id>` | Enable a host |
| `host disable <id>` | Disable a host |
| `host update <id> <field>=<value>` | Update a host field |
| `host ssl-enable <cert_id>` | Assign a certificate to hosts |
| `host ssl-disable <id>` | Disable SSL |
| `host bulk-add-domain <domain>` | Add domain to multiple hosts |
| `host bulk-remove-domain <pattern>` | Remove domains from hosts |
| `host bulk-replace-domain <old> <new>` | Replace domain in hosts |
| `host bulk-update <field> <value>` | Update field across hosts |

#### Selecting Hosts

Commands that write to more than one host take a selector, and all but
`bulk-replace-domain` refuse to run without one — a bare
`host bulk-remove-domain com` would otherwise rewrite every host you have:

| Command | Selectors |
|---------|-----------|
| `host split` | `--ids 1,2,3` · `--pattern <domain>` · `--interactive` |
| `host merge` | `--ids 1,2,3` · `--pattern <domain>` · `--interactive` |
| `host ssl-enable` | `--ids 1,2,3` · `--pattern <domain>` · `--interactive` |
| `host bulk-update` | `--ids 1,2,3` · `--pattern <domain>` · `--interactive` |
| `host bulk-add-domain` | `--ids 1,2,3` · `--pattern <domain>` · `--interactive` |
| `host bulk-remove-domain` | `--ids 1,2,3` · `--interactive` |
| `host bulk-replace-domain` | `--ids 1,2,3` · `--interactive` (defaults to every host holding the old domain) |

On `host split`, `host ssl-enable` and `host bulk-update`, `--pattern` accepts
either a glob or a plain substring, so `*.internal.lan` and `internal.lan` both work;
on `host bulk-add-domain` it is a plain substring match. Every command in the
table also takes `--preview/--no-preview` (preview is on by default) and
`-y`/`--yes` to skip the confirmation prompt.

### Certificate Commands

| Command | Description |
|---------|-------------|
| `cert list [--json]` | List all certificates |
| `cert show <id or domain> [--json]` | Show certificate details |
| `cert generate <domain>` | Generate Let's Encrypt cert |
| `cert delete <id>` | Delete a certificate |
| `cert download <id>` | Download certificate files |

Certificate validity is derived from the certificate's `expires_on` date:
certificates within 30 days of expiry are flagged with the days remaining,
expired ones are shown with how long ago they lapsed, and a certificate with no
readable expiry is reported as UNKNOWN rather than assumed valid.

## Splitting Dual-Domain Hosts

NPM renders one nginx `server` block per proxy host, with a single
`ssl_certificate`. A host that answers to both an internal and a public name —
say `app.internal.lan` and `app.example.com` — can therefore only ever present
**one** certificate, and whichever name that certificate does not cover throws
an SSL error in the browser.

`host split` fixes this by moving the matching names out onto brand-new hosts
that carry a certificate of their own. The source host keeps its remaining
domains **and its existing certificate** — split never touches the source's
cert. Everything else (websockets, force SSL, HSTS, custom locations, advanced
config, access list) is copied to the new host verbatim.

```bash
# 1. Always back up first
npm-api backup

# 2. Preview: move every *.internal.lan name off hosts 11, 12 and 13 onto new
#    hosts carrying the internal wildcard certificate (ID 5)
npm-api host split '*.internal.lan' --cert 5 --ids 11,12,13
```

The preview lists, per host, the domains that stay, the domains that move, and
the certificate the source will keep. Nothing is written until you confirm —
add `-y` to skip the prompt, or `--no-preview` to suppress the table.

```bash
# 3. The sources now hold only their public names, but still carry the
#    internal certificate they started with. Repoint them at the public one.
npm-api host bulk-update certificate_id 6 --ids 11,12,13
```

Quote the glob so your shell does not expand it. Matching uses `fnmatch`, where
`*` also spans dots, so `*.internal.lan` matches `a.b.internal.lan` as well as
`app.internal.lan`.

**Safety behaviour:**

- A host is **skipped with a warning** (not a hard failure) when it has fewer
  than two domains, or when the glob matches none or all of its domains — so
  you can select a whole batch and let the irrelevant ones fall out.
- Domain collisions against every existing host are checked before anything is
  written; a clashing host is skipped and reported.
- The source's domain list is trimmed **before** the new host is created, so
  the two never hold the same domain at once (NPM rejects duplicates). If the
  create then fails, the source is rolled back; if the rollback also fails, the
  original domain list is printed so you can restore it by hand.
- The command exits non-zero if any host failed.

## Cloning a Host

`host clone` copies a host onto new domains and never modifies the source.

```bash
# Inherit the source's certificate
npm-api host clone 42 --domain app.example.com

# Several domains, an explicit certificate and a different backend port
npm-api host clone 42 --domain a.example.com --domain b.example.com \
    --cert 15 --forward-port 8081

# No certificate at all
npm-api host clone 42 --domain plain.example.com --cert none
```

`--domain` is required and repeatable — NPM requires unique domain names, so a
clone always needs new ones. `--cert` is optional and inherits the source's
certificate when omitted. Wildcard domains, and domains already claimed by
another host, are rejected.

## Merging Hosts

`host merge` is the inverse of `host split`: it folds several hosts into one
and **deletes the sources**. Useful when a set of names that were separated
turn out to belong together — same backend, one certificate that covers all of
them.

```bash
# 1. Always back up first
npm-api backup

# 2. Preview: fold hosts 13 and 14 into host 12
npm-api host merge --into 12 --ids 13,14
```

The `--into` host is kept whole and supplies **every** setting. The others
contribute their domain names and nothing else — their certificate, force SSL,
HSTS, websockets, caching, access list, advanced config and custom locations
are all discarded in favour of the target's. The preview names, per source,
exactly which of those settings differ and will therefore change behaviour for
the domains moving across.

> **Read this before merging.** One NPM host is one nginx `server` block with
> one `ssl_certificate`. Merging is precisely how a host ends up answering to
> names its certificate does not cover — the problem `host split` exists to
> undo.

Merge checks the result two ways, because the first one goes quiet exactly
where it matters most:

- Against the **certificate's** recorded domains, naming every host name it
  does not cover. NPM keeps that list as free-form metadata it never consults
  when serving, so for uploaded certificates it is routinely unusable and the
  check can only report "coverage not verified".
- Against the **domain names themselves**, warning when the merged host would
  answer to more than one base domain. This needs no certificate metadata at
  all, so it still fires on uploaded certificates:

```
⚠️  Host 12 will answer to 2 unrelated base domains: example.com, internal.lan
   One NPM host is one nginx server block with one certificate, so a single
   certificate has to cover every one of them.
```

Both are advisory — a multi-SAN certificate covering several bases is perfectly
valid, and the tool cannot tell that apart from a mistake. Neither blocks the
merge; the confirmation prompt is where you decide.

**Sources are deleted, not disabled.** NPM enforces domain uniqueness at the
API layer and does not care whether a host is enabled — adding a name another
host holds returns `HTTP 400: <domain> is already in use`. So a source's names
have to be freed before the target can claim them, and disabling would not free
them. The `pre_merge_*.json` snapshot exists because of this.

```bash
# Give the merged host a certificate that covers the whole union
npm-api host merge --into 12 --ids 13,14 --cert 6

# Or merge them as plain HTTP, to fix the certificate afterwards
npm-api host merge --into 12 --ids 13,14 --cert none
```

`--cert` is optional and inherits the target's certificate when omitted.
`--cert none` also clears `ssl_forced` and `hsts_enabled`, because a host that
forces SSL with no certificate redirects to an HTTPS listener NPM never
renders — strictly worse than plain HTTP.

**Safety behaviour:**

- The `--into` host is **never** deleted, even when `--pattern` also matches it.
- A source that forwards somewhere else — different scheme, host or port —
  **stops the merge**, because its domains would silently start reaching a
  different backend. Pass `--allow-different-targets` if that is intended.
- A certificate that no longer exists stops the merge outright rather than
  producing a survivor with no TLS listener.
- Before the first delete, the full configuration of every host involved is
  written to `pre_merge_<id>_<timestamp>.json` in the backup directory, mode
  `0600`. If the merge cannot write that file, it refuses to run.
- Sources are deleted **one at a time**, each immediately followed by adding
  its domains to the target — NPM will not let two hosts hold the same domain,
  so the name has to be freed first. If the target then rejects the update, the
  source is recreated; a failed recreate prints the snapshot path.
- A recreated source comes back under a **new ID**. NPM assigns IDs on create
  and offers no way to ask for a particular one.
- The command exits non-zero if any source failed.

## Bulk Operations

Powerful bulk operations for managing multiple hosts:

```bash
# Add a new domain to hosts based on subdomain pattern
# If host has [app.domain1.com, app.domain2.com]
# This adds app.domain3.com
npm-api host bulk-add-domain domain3.com --interactive

# Remove domains matching a pattern
npm-api host bulk-remove-domain olddomain.com --ids 1,2,3

# Replace one domain with another
npm-api host bulk-replace-domain old.com new.com --pattern old.com

# Update a field across multiple hosts
npm-api host bulk-update forward_host 192.168.1.100 --ids 1,2,3

# List fields are split on commas; free-text fields keep theirs
npm-api host update 42 domain_names=a.lan,b.lan
npm-api host update 42 'locations=[{"path":"/api","forward_host":"10.0.0.5","forward_port":8080,"forward_scheme":"http"}]'
```

`host update` and `host bulk-update` take `field=value` / `<field> <value>`
pairs. `true`/`false` become booleans, `null`/`none` become null, whole numbers
become integers, and a value starting with `[` or `{` is parsed as JSON. Only
the list fields (`domain_names`, `locations`) are split on commas, so
`advanced_config` and other free text keep their commas intact.

Every bulk command takes the same selector set — `--ids`, `--pattern`,
`--interactive` — plus `--preview/--no-preview` and `-y`. A selector is
**required**: none of them will act on the whole estate by default. They also
exit non-zero if any host failed, so partial failures are visible to scripts.

`bulk-add-domain` reuses each existing name's subdomain prefix, so a host
holding `sub.app.old.com` gains `sub.app.new.com`. Names that are already apex
domains have no prefix to reuse and are skipped. The base domain is assumed to
be two labels, so a suffix like `.co.uk` keeps one label too many.

## JSON Output

`info`, `host list`, `host show`, `host search`, `cert list`, `cert show`,
`user list`, `acl list` and `acl show` accept `--json`. Output is unstyled and
written to stdout, so it pipes straight into `jq`:

```bash
npm-api host list --json | jq '.[] | select(.certificate_id == 27) | .id'
npm-api host show 42 --json | jq -r '.domain_names[]'
npm-api cert list --json | jq '.[] | {id, nice_name, provider, expires_on}'
npm-api info --json | jq '.stats.proxy_hosts'
```

`cert show <id> --json` emits a single object; `cert show <domain> --json`
emits an array of every matching certificate.

Only a command's own output goes to stdout — tables, detail blocks and JSON.
Warnings, previews, progress and errors go to stderr, so `--json` stays
parseable even when a run fails before producing any. If a command cannot
authenticate or reach NPM it writes nothing to stdout and exits non-zero:

```bash
npm-api host list --json | jq '.[].id' || echo "NPM unreachable"
```

## Backups

```bash
npm-api backup                                  # configuration only
npm-api backup -o /mnt/nas/npm-2026-08-23       # choose the destination
npm-api backup --include-keys                   # also capture private keys
```

By default a backup captures hosts, certificates *metadata*, access lists,
users and settings — enough to rebuild every proxy host, **not** enough to
serve TLS. The command says so explicitly when it finishes.

Pass `--include-keys` to download certificate private keys as well. They are
written unencrypted at mode 600, and the command tells you the backup now
contains key material. Encrypt it yourself if it leaves the machine.

Two limits worth knowing:

- NPM's API exports Let's Encrypt certificates it issued, but **not**
  certificates uploaded to it (`provider: other`) — those return HTTP 404 and
  400 from both download routes. The backup names each one and prints the
  `docker cp` that fetches it from the container filesystem instead. These do
  not fail the backup, since an uploaded certificate fails every single run.
- If a section fails, `backup` reports which one and **exits non-zero**, so a
  cron job will not record a partial backup as a success.

## Restoring

```bash
npm-api restore ~/.npm-api/backups                  # newest backup in a directory
npm-api restore /mnt/nas/npm-2026-08-23/full_config_2026_08_23__09_14_55.json
```

Point it at a backup file, or at a directory — it follows
`full_config_latest.json`, or falls back to the newest `full_config_*.json` if
that symlink is stale.

Restore is built for a **freshly set up NPM**. Pointing it at one that already
holds hosts deletes them first: this replaces configuration, it does not
reconcile it. The preview says how many objects go, and the command names the
backup you should take first.

### What comes back, and what does not

| Section | Restored? |
|---------|-----------|
| Access lists | Yes — recreated first, because hosts reference them |
| Proxy hosts | Yes — with certificate and access-list IDs remapped |
| Settings | Only IDs this NPM already defines |
| Certificates | **No** — matched only; the target's are left untouched |
| Users | **No** |

**Users** cannot be restored: NPM's API never exports password material, so the
only alternative would be recreating them with invented passwords. Make them by
hand.

**Certificates** are matched, never written. Restoring them would mean reading
private keys off disk and POSTing them to an endpoint that is plain HTTP by
default, so the tool does not do it. Each backed-up certificate is instead
matched against one already installed here — on its **set of domain names**,
since `nice_name` is free text a user can edit. The name is the fallback
whenever the domain match finds nothing on either side, which most often means
an uploaded certificate installed here whose `domain_names` NPM never filled
in.

IDs are never carried across. NPM assigns them on create, so a backup's
`certificate_id` means nothing in a different instance; writing one back
unchecked is exactly how a host ends up pointing at a certificate that is not
there. Any host whose certificate has no match comes back with
`certificate_id`, `ssl_forced` and `hsts_enabled` all cleared — HTTP-only and
honest, rather than redirecting to an HTTPS listener that was never rendered.
Those hosts are named twice, in the preview and again at the end:

```bash
# Install the certificates in NPM, then repoint the hosts
npm-api cert list
npm-api host bulk-update certificate_id 6 --pattern example.com
```

**Settings** are written only for IDs the target already defines. In NPM 2.x
that means `default-site` and nothing else; the guard exists so a backup taken
from a later release cannot introduce settings this instance has never had.
Skipped ones are named.

**Access list passwords** may not survive. If the backup holds an entry with no
password, restore says which user on which list, and you set it again in NPM.

**Safety behaviour:**

- Before the first delete, the target's current hosts, access lists and
  settings are written to `pre_restore_<timestamp>.json` in the backup
  directory at mode `0600`. If that file cannot be written, restore refuses to
  run.
- Existing hosts are deleted before existing access lists — NPM will not drop
  an access list a host still references.
- A single failed create is reported and counted; the rest of the restore
  continues. The command exits non-zero if anything failed.

## Gotcha: Deleted Certificates Leave Dangling IDs

If you delete a certificate from the NPM UI, every host still referencing it
keeps the now-dangling `certificate_id`. NPM then renders those hosts with no
`listen 443 ssl` line at all, silently dropping them to HTTP-only: HTTPS
requests fall through to another server block and present the wrong
certificate. `cert list` will not show the deleted certificate, but `host list`
still displays its ID in the SSL column.

To find dangling references, compare the two lists:

```bash
npm-api cert list --json | jq '[.[].id]'
npm-api host list --json | jq '[.[] | {id, domain_names, certificate_id}]'
```

Assigning a certificate through `host bulk-update certificate_id <id>` or
`host ssl-enable <id>` now guards against creating one: the certificate must
exist, its expiry is reported, and any host domain the certificate does not
cover is flagged. Coverage is checked against the domain list NPM records for
the certificate, which for uploaded certificates can be incomplete — when that
list is unusable the tool says coverage was not verified rather than guessing.

## Security

- **Never commit credentials** to version control
- Use **environment variables** in CI/CD and Docker
- Config files are automatically excluded via `.gitignore`
- Secure config files with `chmod 600`
- Certificate private keys are **opt-in**: `backup` writes none unless you pass
  `--include-keys`. Keys and tokens are created at mode 600 but are not
  encrypted at rest — see [SECURITY.md](SECURITY.md)

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes, including breaking changes.

## Credits

Based on [nginx-proxy-manager-Bash-API](https://github.com/Erreur32/nginx-proxy-manager-Bash-API) by Erreur32.

## License

MIT License - see [LICENSE](LICENSE) for details.
