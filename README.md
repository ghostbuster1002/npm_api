# NPM-API

[![Build and Release](https://github.com/YOUR_USERNAME/npm-api/actions/workflows/build.yml/badge.svg)](https://github.com/YOUR_USERNAME/npm-api/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

A Python CLI tool for managing [Nginx Proxy Manager](https://nginxproxymanager.com/) via its API.

**Features:**
- 🌐 Manage proxy hosts (create, update, delete, enable/disable)
- 🔒 SSL certificate management (Let's Encrypt, including wildcards)
- 👥 User management
- 🛡️ Access list management
- 📦 Bulk operations (add/remove/replace domains across multiple hosts)
- 💾 Full backup and restore
- 🔐 Secure credential handling via environment variables or config files

## Installation

### Download Pre-built Binary

Download the latest release for your platform from [Releases](https://github.com/YOUR_USERNAME/npm-api/releases):

```bash
# Linux
wget https://github.com/YOUR_USERNAME/npm-api/releases/latest/download/npm-api-linux-amd64
chmod +x npm-api-linux-amd64
sudo mv npm-api-linux-amd64 /usr/local/bin/npm-api

# macOS
wget https://github.com/YOUR_USERNAME/npm-api/releases/latest/download/npm-api-macos-amd64
chmod +x npm-api-macos-amd64
sudo mv npm-api-macos-amd64 /usr/local/bin/npm-api
```

### Build from Source

```bash
git clone https://github.com/YOUR_USERNAME/npm-api.git
cd npm-api
make build
sudo make install
```

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

# Enable SSL for a host
npm-api host ssl-enable 42 123

# Bulk add a domain to multiple hosts
npm-api host bulk-add-domain newdomain.com --interactive
```

## Commands

```
npm-api --help              Show all commands
npm-api info                Dashboard and configuration info
npm-api backup              Full backup of all configurations

npm-api host --help         Proxy host management
npm-api cert --help         SSL certificate management
npm-api user --help         User management
npm-api acl --help          Access list management
```

### Host Commands

| Command | Description |
|---------|-------------|
| `host list` | List all proxy hosts |
| `host show <id>` | Show host details |
| `host search <pattern>` | Search hosts by domain |
| `host create <domain> -i <ip> -p <port>` | Create new host |
| `host delete <id>` | Delete a host |
| `host enable <id>` | Enable a host |
| `host disable <id>` | Disable a host |
| `host update <id> <field>=<value>` | Update a host field |
| `host ssl-enable <id> <cert_id>` | Enable SSL |
| `host ssl-disable <id>` | Disable SSL |
| `host bulk-add-domain <domain>` | Add domain to multiple hosts |
| `host bulk-remove-domain <pattern>` | Remove domains from hosts |
| `host bulk-replace-domain <old> <new>` | Replace domain in hosts |
| `host bulk-update <field> <value>` | Update field across hosts |

### Certificate Commands

| Command | Description |
|---------|-------------|
| `cert list` | List all certificates |
| `cert show <id or domain>` | Show certificate details |
| `cert generate <domain>` | Generate Let's Encrypt cert |
| `cert delete <id>` | Delete a certificate |
| `cert download <id>` | Download certificate files |

## Bulk Operations

Powerful bulk operations for managing multiple hosts:

```bash
# Add a new domain to hosts based on subdomain pattern
# If host has [app.domain1.com, app.domain2.com]
# This adds app.domain3.com
npm-api host bulk-add-domain domain3.com --interactive

# Remove domains matching a pattern
npm-api host bulk-remove-domain olddomain.com

# Replace one domain with another
npm-api host bulk-replace-domain old.com new.com

# Update a field across multiple hosts
npm-api host bulk-update forward_host 192.168.1.100 --ids 1,2,3
```

## Security

- **Never commit credentials** to version control
- Use **environment variables** in CI/CD and Docker
- Config files are automatically excluded via `.gitignore`
- Secure config files with `chmod 600`

## Credits

Based on [nginx-proxy-manager-Bash-API](https://github.com/Erreur32/nginx-proxy-manager-Bash-API) by Erreur32.

## License

MIT License - see [LICENSE](LICENSE) for details.
