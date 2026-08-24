# Security Considerations

## Credential Storage

### Recommended: Environment Variables
For production, CI/CD, and Docker environments, use environment variables:

```bash
export NPM_API_USER="admin@example.com"
export NPM_API_PASS="your_password"
```

Environment variables are:
- Not written to disk
- Not visible in process listings (on most systems)
- Easy to rotate
- Native to container orchestration

### Config Files
If using config files, ensure proper permissions:

```bash
chmod 600 ~/.config/npm-api/npm-api.conf
```

The `.gitignore` excludes all `.conf` files to prevent accidental commits.

## Token Storage

API tokens are stored in the data directory, created at mode 600 (owner read/write only). Tokens are automatically refreshed when they expire.

## Certificate Private Keys

`npm-api backup` does **not** capture certificate private keys unless you pass `--include-keys`. A default backup therefore holds configuration and certificate metadata only — enough to rebuild proxy hosts, not enough to serve TLS.

When you do pass `--include-keys`:

- Keys are written **unencrypted**, at mode 600. Encrypt the backup yourself if it leaves the machine (`age`, `gpg`, or an encrypted filesystem).
- Keep the destination off shared storage and out of version control. `.gitignore` excludes `certificates/`, `backups/` and common key extensions, but that only protects a clone of this repo.
- NPM's API exports Let's Encrypt certificates it issued, but not certificates uploaded to it, so a backup taken with `--include-keys` may still be missing key material. The command names each certificate it could not retrieve and prints the `docker cp` needed to fetch it from the container filesystem. Check that output rather than assuming the backup is complete.

`npm-api cert download` always writes key material — that is its purpose — and prints the same warning.

`npm-api restore` never writes key material and never sends it anywhere. Restoring certificates would mean reading private keys off disk and POSTing them to an endpoint that is plain HTTP by default, so the tool does not do it: backed-up certificates are only *matched* against ones already installed in the target, and a host whose certificate has no match comes back HTTP-only rather than pointing at something that is not there. Install certificates in NPM yourself, then repoint the hosts with `host bulk-update certificate_id`.

## State Snapshots

`host merge` and `restore` are the two commands that delete objects NPM cannot bring back. Before either deletes anything it writes the current configuration of everything it will touch to the backup directory at mode 600 — `pre_merge_<id>_<timestamp>.json` and `pre_restore_<timestamp>.json`. A command that cannot write its snapshot refuses to run.

These files hold whatever the affected objects hold, which can include:

- `advanced_config` blocks, which routinely carry internal hostnames and auth headers
- access list entries, including any basic-auth passwords NPM returns

They are **not encrypted**, on the same reasoning as backups. Treat them like backups: keep them off shared storage, and delete them once the change has been verified. `.gitignore` excludes `backups/`, but that only protects a clone of this repo.

## Network Security

⚠️ **Important**: This tool uses HTTP by default to communicate with Nginx Proxy Manager's API (typically on port 81). This is consistent with NPM's default configuration.

**Recommendations:**
- Only run NPM API on trusted networks (localhost, private LAN)
- Use a VPN or SSH tunnel for remote access
- Consider putting NPM behind a reverse proxy with HTTPS for the admin interface

## Reporting Security Issues

If you discover a security vulnerability, please:
1. **Do not** open a public issue
2. Email [kalras.kapil@gmail.com] with details
3. Allow time for a fix before public disclosure

## Security Features

- ✅ No hardcoded credentials (defaults are rejected)
- ✅ Token and private key files created at mode 600, with the mode applied at creation rather than afterwards
- ✅ Private keys are opt-in for backups, never written by default
- ✅ `restore` never reads or transmits private key material
- ✅ Destructive commands snapshot what they are about to delete, at mode 600, and refuse to run if they cannot
- ✅ Path traversal protection on file operations
- ✅ Zip slip attack prevention

Not claimed:

- ❌ Backups, snapshots and downloaded keys are **not** encrypted at rest
- ❌ HTTP, not HTTPS, to the NPM API by default (see Network Security above)
- ✅ No command injection vectors (no shell execution)
- ✅ No eval() or exec() usage
- ✅ Config files excluded from git via .gitignore
