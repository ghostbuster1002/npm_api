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

API tokens are stored in the data directory with `chmod 600` permissions (owner read/write only). Tokens are automatically refreshed when they expire.

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
- ✅ Token files stored with restrictive permissions (600)
- ✅ Private key files stored with restrictive permissions (600)
- ✅ Path traversal protection on file operations
- ✅ Zip slip attack prevention
- ✅ No command injection vectors (no shell execution)
- ✅ No eval() or exec() usage
- ✅ Config files excluded from git via .gitignore
