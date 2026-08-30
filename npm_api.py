#!/usr/bin/env python3
"""
Nginx Proxy Manager CLI Script
Python version converted from bash script
Original: https://github.com/Erreur32/nginx-proxy-manager-Bash-API
By Erreur32 - July 2024

Python conversion with improvements:
- Native JSON handling
- Better error handling
- Type hints
- Cleaner argument parsing with Typer
- Rich console output
"""

import json
import os
import sys
import re
import shutil
import unicodedata
import zipfile
from fnmatch import fnmatch
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

try:
    import requests
    import typer
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table
    from rich.panel import Panel
    from rich.syntax import Syntax
except ImportError as e:
    # stderr, like every other diagnostic here: a caller piping our stdout into
    # jq should get a clean parse failure on empty input, not this banner.
    print("\n" + "=" * 60, file=sys.stderr)
    print("ERROR: Required Python packages are not installed!", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"\nMissing module: {e.name}", file=sys.stderr)
    print("\nPlease install the required packages:", file=sys.stderr)
    print("\n  Option 1 - Using pip (recommended):", file=sys.stderr)
    print('    pip install requests "typer[all]" rich', file=sys.stderr)
    print("\n  Option 2 - Using a virtual environment:", file=sys.stderr)
    print("    python3 -m venv venv", file=sys.stderr)
    print("    source venv/bin/activate  # On Windows: venv\\Scripts\\activate",
          file=sys.stderr)
    print('    pip install requests "typer[all]" rich', file=sys.stderr)
    print("\n  Option 3 - Using pipx (for isolated installation):", file=sys.stderr)
    print("    pipx install npm-api  # If packaged", file=sys.stderr)
    print("\n" + "=" * 60, file=sys.stderr)
    sys.exit(1)

# Version. Tracks the git tag and the GitHub release, so a bug report quoting
# `npm-api info` names a build that can be checked out. Was "3.0.7-py",
# carried over from the bash script this was ported from; no Python release
# ever bore a 3.x number, and the release history is the lineage that is
# actually public. Bump this in the same commit that tags a release.
VERSION = "2.0.0"

# Initialize Rich consoles and Typer app.
#
# Two streams, split the usual Unix way:
#
#   console      (stderr) — diagnostics. Status, warnings, errors, previews,
#                           progress spinners, confirmation prompts, and the
#                           per-item success/failure lines write commands emit.
#   out_console  (stdout) — primary output. The data the user ran the command
#                           to see: the list tables, the show/detail blocks,
#                           the info dashboard, show-defaults.
#
# stderr is the default so a print added later lands there unless someone
# deliberately reaches for out_console, and so `host list | grep example.com`
# keeps working while a failed run still leaves the pipe empty.
#
# print_json() bypasses both and writes plain stdout: it is the one path with a
# hard guarantee, so `--json | jq` stays valid under every failure mode.
console = Console(stderr=True)
out_console = Console()
app = typer.Typer(
    name="npm-api",
    help="Nginx Proxy Manager CLI - Manage NPM via API",
    add_completion=False,
    rich_markup_mode="rich"
)

# Sub-commands
host_app = typer.Typer(help="Proxy host management")
cert_app = typer.Typer(help="SSL certificate management")
user_app = typer.Typer(help="User management")
acl_app = typer.Typer(help="Access list management")

app.add_typer(host_app, name="host")
app.add_typer(cert_app, name="cert")
app.add_typer(user_app, name="user")
app.add_typer(acl_app, name="acl")


# =============================================================================
# Configuration and Data Classes
# =============================================================================

@dataclass
class Config:
    """Configuration for NPM API connection.
    
    Configuration is loaded from (in priority order):
    1. Environment variables (highest priority)
    2. Config file (searched in multiple locations)
    3. Default values
    
    Environment variables:
        NPM_API_HOST     - NPM server IP/hostname (default: 127.0.0.1)
        NPM_API_PORT     - NPM server port (default: 81)
        NPM_API_USER     - API username/email (required)
        NPM_API_PASS     - API password (required)
        NPM_API_DATA_DIR - Data directory for backups/tokens
    
    Config file locations (searched in order):
        1. ./npm-api.conf (current directory)
        2. ~/.config/npm-api/npm-api.conf
        3. /etc/npm-api/npm-api.conf
        4. <script_dir>/npm-api.conf
    """
    nginx_ip: str = "127.0.0.1"
    nginx_port: str = "81"
    api_user: str = "admin@example.com"
    api_pass: str = "changeme"
    data_dir: str = ""
    token_expiry: str = "1y"
    _config_source: str = "defaults"
    _config_file_path: str = ""
    _searched_paths: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        if not self.data_dir:
            if getattr(sys, "frozen", False):
                # PyInstaller defines __file__ inside its temp extraction
                # directory, which the bootloader deletes on exit. Tokens and
                # backups written there would silently vanish, so anchor the
                # frozen binary's state in the user's home instead.
                self.data_dir = str(Path.home() / ".npm-api" / "data")
            else:
                self.data_dir = str(Path(__file__).parent / "data")
    
    @property
    def base_url(self) -> str:
        return f"http://{self.nginx_ip}:{self.nginx_port}/api"
    
    @property
    def data_dir_id(self) -> str:
        ip_port_dir = f"{self.nginx_ip.replace('.', '_').replace(':', '_')}_{self.nginx_port}"
        return str(Path(self.data_dir) / ip_port_dir)
    
    @property
    def token_dir(self) -> str:
        return str(Path(self.data_dir_id) / "token")
    
    @property
    def backup_dir(self) -> str:
        return str(Path(self.data_dir_id) / "backups")
    
    @property
    def token_file(self) -> str:
        return str(Path(self.token_dir) / "token.txt")
    
    @property
    def expiry_file(self) -> str:
        return str(Path(self.token_dir) / "expiry.txt")
    
    @classmethod
    def _get_config_search_paths(cls) -> List[Path]:
        """Get list of paths to search for config file."""
        paths = []
        
        # 1. Current working directory
        paths.append(Path.cwd() / "npm-api.conf")
        
        # 2. User config directory
        paths.append(Path.home() / ".config" / "npm-api" / "npm-api.conf")
        
        # 3. System config directory (Linux/macOS)
        paths.append(Path("/etc/npm-api/npm-api.conf"))
        
        # 4. Script directory (where the binary/script is located)
        try:
            script_dir = Path(__file__).parent.resolve()
            paths.append(script_dir / "npm-api.conf")
        except NameError:
            # __file__ may not be defined in frozen executables
            # Try to get the executable path
            try:
                import sys
                if getattr(sys, 'frozen', False):
                    # Running as compiled
                    exe_dir = Path(sys.executable).parent
                    paths.append(exe_dir / "npm-api.conf")
            except Exception:
                pass
        
        return paths
    
    @classmethod
    def _load_from_file(cls, file_path: Path) -> Dict[str, str]:
        """Load configuration from a file."""
        config_values = {}
        
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip().lower()
                        value = value.strip().strip('"').strip("'")
                        config_values[key] = value
        except Exception:
            pass
        
        return config_values
    
    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """Load configuration from environment variables and/or config file.
        
        Priority:
        1. Environment variables (highest priority, override everything)
        2. Config file
        3. Default values
        """
        config = cls()
        config._searched_paths = []
        
        # Step 1: Try to load from config file
        search_paths = cls._get_config_search_paths()
        
        if config_path:
            search_paths.insert(0, Path(config_path))
        
        found_config = None
        for path in search_paths:
            config._searched_paths.append(str(path))
            if path.exists() and path.is_file():
                found_config = path
                break
        
        if found_config:
            file_values = cls._load_from_file(found_config)
            
            if 'nginx_ip' in file_values:
                config.nginx_ip = file_values['nginx_ip']
            if 'nginx_port' in file_values:
                config.nginx_port = file_values['nginx_port']
            if 'api_user' in file_values:
                config.api_user = file_values['api_user']
            if 'api_pass' in file_values:
                config.api_pass = file_values['api_pass']
            if 'data_dir' in file_values:
                config.data_dir = file_values['data_dir']
            
            config._config_source = "file"
            config._config_file_path = str(found_config)
        
        # Step 2: Override with environment variables (highest priority)
        env_mappings = {
            'NPM_API_HOST': 'nginx_ip',
            'NPM_API_PORT': 'nginx_port',
            'NPM_API_USER': 'api_user',
            'NPM_API_PASS': 'api_pass',
            'NPM_API_DATA_DIR': 'data_dir',
        }
        
        env_used = []
        for env_var, attr_name in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                setattr(config, attr_name, value)
                env_used.append(env_var)
        
        if env_used:
            if config._config_source == "file":
                config._config_source = "file+env"
            else:
                config._config_source = "env"
        
        return config
    
    def is_using_defaults(self) -> bool:
        """Check if we're still using default credentials."""
        return self.api_user == "admin@example.com" or self.api_pass == "changeme"
    
    def get_config_info(self) -> str:
        """Get human-readable info about configuration source."""
        if self._config_source == "env":
            return "environment variables"
        elif self._config_source == "file":
            return f"config file: {self._config_file_path}"
        elif self._config_source == "file+env":
            return f"config file ({self._config_file_path}) + environment variables"
        else:
            return "defaults (not configured)"


@dataclass
class ProxyHostDefaults:
    """Default values for creating proxy hosts"""
    forward_scheme: str = "http"
    caching_enabled: bool = False
    block_exploits: bool = True
    allow_websocket_upgrade: bool = False
    http2_support: bool = False
    ssl_forced: bool = False
    hsts_enabled: bool = False
    hsts_subdomains: bool = False
    advanced_config: str = ""
    custom_locations: List[Dict] = field(default_factory=list)
    trust_forwarded_proto: bool = False


# =============================================================================
# Secret file helpers
# =============================================================================

class CertificateDownloadError(RuntimeError):
    """Raised when a certificate's files could not be retrieved from NPM."""


class NPMError(RuntimeError):
    """An operational failure talking to NPM, not a bug in this script.

    Bad credentials, an unreachable server and a reply that is not NPM's are
    all things the user can fix. The message is written to be the whole of
    what they see: main() prints it on one line and exits non-zero, so no
    traceback escapes for any of them.
    """


@dataclass
class CertKeyFailure:
    """A certificate whose key material NPM's API would not hand over.

    Carries enough to tell the user how to fetch it by hand: NPM keeps issued
    certificates under /etc/letsencrypt and uploaded ones under /data, and the
    directory is named for the certificate ID either way.
    """
    cert_id: int
    name: str
    provider: Optional[str]
    reason: str

    @property
    def container_paths(self) -> List[str]:
        """Where to look on the container, most likely location first.

        An absent provider yields both candidates rather than a guess: NPM has
        no obligation to send the field, and printing one confident wrong path
        is worse than printing two and letting the user see which exists.
        """
        issued = f"/etc/letsencrypt/live/npm-{self.cert_id}"
        uploaded = f"/data/custom_ssl/npm-{self.cert_id}"
        if self.provider == "letsencrypt":
            return [issued]
        if self.provider:
            return [uploaded]
        return [uploaded, issued]

    def __str__(self) -> str:
        return f"certificate {self.cert_id} ({self.name}): {self.reason}"


@dataclass
class BackupResult:
    """Outcome of a full backup, including anything that did not get written.

    Carried back to the caller rather than only printed, so that a scheduled
    run can fail loudly instead of exiting 0 over a half-written backup.
    """
    path: str
    failures: List[str] = field(default_factory=list)
    key_failures: List[CertKeyFailure] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures


def format_http_error(exc: Exception) -> str:
    """Render the message NPM actually sent, not requests' generic repr.

    An HTTPError stringifies as "400 Client Error: Bad Request for url: ...",
    which buries the reason. NPM replies with {"error": {"message": ...}}.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)

    try:
        body = response.json()
    except ValueError:
        detail = (response.text or "").strip()
        return f"HTTP {response.status_code}: {detail[:200]}" if detail \
            else f"HTTP {response.status_code}"

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return f"HTTP {response.status_code}: {error['message']}"
    if isinstance(error, str) and error:
        return f"HTTP {response.status_code}: {error}"
    return f"HTTP {response.status_code}: {json.dumps(body)[:200]}"


# urllib3 nests the real cause inside a chain of pool and retry reprs; the
# only actionable part is the OS-level reason it ends with, e.g.
# "[Errno 111] Connection refused" or "[Errno -2] Name or service not known".
_ERRNO_REASON_RE = re.compile(r"\[Errno -?\d+\]\s*([^'\")]+)")


def describe_connection_error(exc: Exception) -> str:
    """Reduce a requests transport failure to one readable clause.

    str() on a ConnectionError runs to a couple of hundred characters of
    library internals, which buries "the host is not listening" — the only
    thing the user can act on.
    """
    if isinstance(exc, requests.Timeout):
        return "timed out"

    match = _ERRNO_REASON_RE.search(str(exc))
    if match:
        return match.group(1).strip()

    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    return type(exc).__name__


def describe_unreachable(config: "Config", exc: Exception) -> str:
    """Name the endpoint alongside the reason; a wrong port looks identical
    to a stopped server otherwise."""
    return f"Cannot reach NPM at {config.base_url} — {describe_connection_error(exc)}"


def write_secret(path: Path, content: str) -> Path:
    """Write a file that only its owner can read, private from the first byte.

    write_text() followed by chmod(0o600) leaves the file world-readable for
    the moment in between, which matters when the content is a private key or
    an API token. Passing the mode to os.open() applies it at creation. An
    existing file keeps its old mode through O_CREAT, so remove it first.
    """
    path = Path(path)
    if path.exists() or path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    return path


def write_state_snapshot(config: Config, label: str, payload: Dict) -> Path:
    """Record live configuration to disk before a command destroys it.

    Used by merge and restore, the two commands that delete objects NPM cannot
    bring back. Both roll back in the ordinary case, but neither can if the
    process is killed partway through, and anything recreated comes back under
    a new ID. This file is the floor under both.
    """
    directory = Path(config.backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y_%m_%d__%H_%M_%S")
    path = directory / f"{label}_{stamp}.json"
    # 0600 like everything else this tool writes: advanced_config can carry auth
    # headers and internal hostnames, and access lists carry credentials.
    return write_secret(path, json.dumps(dict(payload, created=stamp), indent=2))


# =============================================================================
# Host & certificate helpers
# =============================================================================

# Assigned by NPM; never sent back when creating or updating a host. The
# trailing three are objects NPM expands alongside their *_id counterparts when
# a query asks for them; echoing them back would send a nested object where the
# API expects an integer.
HOST_READONLY_FIELDS = {
    "id", "created_on", "modified_on", "owner_user_id",
    "certificate", "owner", "access_list",
}

# Runtime status NPM writes into meta, not part of a host's configuration
HOST_META_RUNTIME_KEYS = {"nginx_online", "nginx_err"}

# What full_backup writes and load_backup looks for. Shared so the two cannot
# drift, and because the link matches the glob and has to be excluded from it —
# Path.glob lists dangling symlinks, so a stale link would otherwise sort last
# and be picked as the newest backup.
BACKUP_GLOB = "full_config_*.json"
LATEST_BACKUP_LINK = "full_config_latest.json"

# Host fields whose values are lists rather than scalars
HOST_LIST_FIELDS = {"domain_names", "locations"}

# Link fields where 0 is NPM's way of saying "nothing linked". Treated as null
# so that `bulk-update certificate_id 0` clears the certificate, matching what
# `--cert 0` already means to split and clone.
HOST_UNSET_ON_ZERO_FIELDS = {"certificate_id", "access_list_id"}


def host_config_payload(host: Dict, overrides: Optional[Dict] = None) -> Dict:
    """Reduce a host object to a payload suitable for create or update.

    Copies by exclusion rather than by allowlist so that fields introduced by
    newer NPM releases survive a clone. NPM 2.15 added trust_forwarded_proto,
    which an allowlist written against an older release would silently reset.
    """
    payload = {k: v for k, v in host.items() if k not in HOST_READONLY_FIELDS}

    payload["meta"] = {
        k: v for k, v in (payload.get("meta") or {}).items()
        if k not in HOST_META_RUNTIME_KEYS
    }

    if overrides:
        payload.update(overrides)
    return payload


def cert_covers_domain(cert: Dict, domain: str) -> Optional[bool]:
    """Whether a certificate's recorded domain list covers `domain`.

    Returns None when that list holds nothing usable, which callers should read
    as "cannot tell" rather than as a failure. NPM keeps domain_names purely as
    metadata and never consults it when serving TLS, so for uploaded certs it
    drifts from the real SANs - a recorded entry like "*.internal," can belong
    to a certificate that genuinely serves *.internal.lan.
    """
    domain = str(domain).strip().lower().rstrip(".")
    checked_any = False

    for raw in cert.get("domain_names") or []:
        name = str(raw).strip().lower().rstrip(".")
        if not name:
            continue

        bare = name[2:] if name.startswith("*.") else name
        if "." not in bare or any(c in bare for c in ",; "):
            continue  # unusable metadata, e.g. "*.internal,"
        checked_any = True

        if name.startswith("*."):
            # A wildcard matches exactly one label: *.example.com covers
            # app.example.com but not app.eu.example.com
            head, _, tail = domain.partition(".")
            if head and tail == bare:
                return True
        elif name == domain:
            return True

    return False if checked_any else None


def coerce_field_value(field_name: str, value: str) -> Any:
    """Coerce a CLI "field=value" string into the JSON type NPM expects.

    List fields are split on commas so domain_names=a.lan,b.com works, while
    free-text fields such as advanced_config keep their commas intact. Any
    value may also be given as a JSON literal for full control.
    """
    lowered = value.strip().lower()

    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None

    if value.lstrip()[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name}: invalid JSON ({exc})") from exc

    if field_name in HOST_LIST_FIELDS:
        return [part.strip() for part in value.split(",") if part.strip()]

    # Matched strictly: "--5" survives lstrip("-").isdigit() but int() rejects it
    if re.fullmatch(r"-?\d+", value.strip()):
        number = int(value.strip())
        if number == 0 and field_name in HOST_UNSET_ON_ZERO_FIELDS:
            return None
        return number

    return value


# =============================================================================
# API Client
# =============================================================================

class NPMClient:
    """Nginx Proxy Manager API Client"""
    
    def __init__(self, config: Config):
        self.config = config
        self.token: Optional[str] = None
        # Why the last token attempt failed, kept so the caller that actually
        # needs a token can report it rather than generate_token()'s bool
        # losing the detail on the way out.
        self.auth_error: Optional[str] = None
        self._ensure_directories()
    
    def _ensure_directories(self):
        """Create required directories"""
        for dir_path in [self.config.data_dir_id, self.config.token_dir, self.config.backup_dir]:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers with authorization"""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers
    
    def load_token(self) -> bool:
        """Load token from file if valid"""
        token_file = Path(self.config.token_file)
        expiry_file = Path(self.config.expiry_file)
        
        if not token_file.exists() or not expiry_file.exists():
            return False
        
        try:
            token = token_file.read_text().strip()
            expiry_str = expiry_file.read_text().strip()
            
            # Parse expiry date
            expiry = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
            
            # Check if token expires within 1 hour
            if expiry - datetime.now(expiry.tzinfo) < timedelta(hours=1):
                return False
            
            # Validate token with API
            response = requests.get(
                f"{self.config.base_url}/tokens",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5
            )
            
            if response.status_code == 200:
                self.token = token
                return True
        except Exception:
            pass
        
        return False
    
    def generate_token(self) -> bool:
        """Generate a new API token.

        On failure the reason lands in self.auth_error rather than on the
        console: the caller knows whether this was a --json run that must keep
        stdout clean, and whether to exit or carry on.
        """
        console.print("[yellow]🔄 Generating new API token...[/yellow]")
        self.auth_error = None

        # First get temporary token
        try:
            response = requests.post(
                f"{self.config.base_url}/tokens",
                json={"identity": self.config.api_user, "secret": self.config.api_pass},
                timeout=10
            )

            if response.status_code != 200:
                if response.status_code in (401, 403):
                    self.auth_error = (
                        f"NPM at {self.config.base_url} rejected the credentials "
                        f"for {self.config.api_user} (HTTP {response.status_code}). "
                        f"Check NPM_API_USER / NPM_API_PASS or your npm-api.conf."
                    )
                else:
                    self.auth_error = (
                        f"Authentication request to {self.config.base_url} returned "
                        f"HTTP {response.status_code}. Is this really an NPM API?"
                    )
                return False

            temp_token = response.json().get("token")

            # Get long-term token
            response = requests.get(
                f"{self.config.base_url}/tokens?expiry={self.config.token_expiry}",
                headers={"Authorization": f"Bearer {temp_token}"},
                timeout=10
            )

            if response.status_code != 200:
                self.auth_error = (
                    f"NPM accepted the credentials but refused a "
                    f"{self.config.token_expiry} token (HTTP {response.status_code})."
                )
                return False

            data = response.json()
            self.token = data["token"]
            expiry = data["expires"]

            # Save token and expiry, owner-readable only from creation
            write_secret(Path(self.config.token_file), self.token)
            write_secret(Path(self.config.expiry_file), expiry)

            console.print("[green]✅ Token generated successfully![/green]")
            console.print(f"[yellow]📅 Expires: {expiry}[/yellow]")
            return True

        except (ValueError, KeyError) as e:
            # A 200 whose body is not NPM's token JSON: wrong port, a login
            # page, a captive proxy. Must precede RequestException because
            # requests' JSONDecodeError inherits from both.
            self.auth_error = (
                f"Unexpected reply from {self.config.base_url} ({e!r}). "
                f"Check NPM_API_HOST / NPM_API_PORT — is that an NPM API?"
            )
            return False
        except requests.RequestException as e:
            self.auth_error = describe_unreachable(self.config, e)
            return False

    def ensure_token(self) -> bool:
        """Ensure we have a valid token"""
        if self.token:
            return True

        if self.load_token():
            return True

        return self.generate_token()

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make an API request"""
        if not self.ensure_token():
            raise NPMError(self.auth_error or "Failed to obtain API token")

        url = f"{self.config.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        headers.update(kwargs.pop("headers", {}))

        # Every auth call sets one; without this a hung NPM blocks forever
        kwargs.setdefault("timeout", 30)

        try:
            response = requests.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as exc:
            # A cached token skips generate_token(), so this is the first place
            # a dead server shows up on a warm run. Re-raise named rather than
            # letting urllib3's repr reach the terminal.
            raise NPMError(describe_unreachable(self.config, exc)) from exc
        return response
    
    def get(self, endpoint: str, **kwargs) -> requests.Response:
        return self._request("GET", endpoint, **kwargs)
    
    def post(self, endpoint: str, **kwargs) -> requests.Response:
        return self._request("POST", endpoint, **kwargs)
    
    def put(self, endpoint: str, **kwargs) -> requests.Response:
        return self._request("PUT", endpoint, **kwargs)
    
    def delete(self, endpoint: str, **kwargs) -> requests.Response:
        return self._request("DELETE", endpoint, **kwargs)
    
    # =========================================================================
    # Proxy Host Methods
    # =========================================================================
    
    def list_hosts(self) -> List[Dict]:
        """List all proxy hosts"""
        response = self.get("/nginx/proxy-hosts")
        response.raise_for_status()
        return response.json()
    
    def get_host(self, host_id: int) -> Dict:
        """Get a specific proxy host"""
        response = self.get(f"/nginx/proxy-hosts/{host_id}")
        response.raise_for_status()
        return response.json()
    
    def search_hosts(self, search_term: str) -> List[Dict]:
        """Search hosts by domain name"""
        hosts = self.list_hosts()
        return [
            h for h in hosts 
            if any(search_term.lower() in d.lower() for d in h.get("domain_names", []))
        ]
    
    def create_host(self, domain: str, forward_host: str, forward_port: int,
                    defaults: Optional[ProxyHostDefaults] = None) -> Dict:
        """Create a new proxy host"""
        if defaults is None:
            defaults = ProxyHostDefaults()
        
        data = {
            "domain_names": [domain],
            "forward_host": forward_host,
            "forward_port": forward_port,
            "forward_scheme": defaults.forward_scheme,
            "caching_enabled": defaults.caching_enabled,
            "block_exploits": defaults.block_exploits,
            "allow_websocket_upgrade": defaults.allow_websocket_upgrade,
            "http2_support": defaults.http2_support,
            "ssl_forced": defaults.ssl_forced,
            "hsts_enabled": defaults.hsts_enabled,
            "hsts_subdomains": defaults.hsts_subdomains,
            "advanced_config": defaults.advanced_config,
            "locations": defaults.custom_locations,
            "trust_forwarded_proto": defaults.trust_forwarded_proto,
            "access_list_id": None,
            "certificate_id": None,
            "meta": {"dns_challenge": None},
            "enabled": True
        }
        
        response = self.post("/nginx/proxy-hosts", json=data)
        response.raise_for_status()
        return response.json()
    
    def create_host_from(self, source: Dict, overrides: Dict) -> Dict:
        """Create a proxy host as a copy of an existing host object"""
        payload = host_config_payload(source, overrides)

        response = self.post("/nginx/proxy-hosts", json=payload)
        response.raise_for_status()
        return response.json()

    def update_host(self, host_id: int, updates: Dict) -> Dict:
        """Update a proxy host, carrying over every field the caller did not name.

        NPM's PUT replaces the whole object, so the current config is read back
        first. Built by exclusion rather than from an allowlist: an allowlist
        written against one NPM release silently resets fields a later release
        adds, which is exactly how trust_forwarded_proto went missing.
        """
        current = self.get_host(host_id)
        data = host_config_payload(current, updates)

        response = self.put(f"/nginx/proxy-hosts/{host_id}", json=data)
        response.raise_for_status()
        return response.json()
    
    def delete_host(self, host_id: int) -> bool:
        """Delete a proxy host"""
        response = self.delete(f"/nginx/proxy-hosts/{host_id}")
        return response.status_code == 200
    
    def enable_host(self, host_id: int) -> bool:
        """Enable a proxy host"""
        response = self.post(f"/nginx/proxy-hosts/{host_id}/enable")
        return response.status_code == 200
    
    def disable_host(self, host_id: int) -> bool:
        """Disable a proxy host"""
        response = self.post(f"/nginx/proxy-hosts/{host_id}/disable")
        return response.status_code == 200
    
    def disable_host_ssl(self, host_id: int) -> bool:
        """Disable SSL for a proxy host"""
        data = {
            "certificate_id": None,
            "ssl_forced": False,
            "http2_support": False,
            "hsts_enabled": False,
            "hsts_subdomains": False
        }
        response = self.put(f"/nginx/proxy-hosts/{host_id}", json=data)
        return response.status_code == 200
    
    def enable_host_acl(self, host_id: int, access_list_id: int) -> bool:
        """Enable ACL for a proxy host"""
        data = {"access_list_id": access_list_id, "enabled": True}
        response = self.put(f"/nginx/proxy-hosts/{host_id}", json=data)
        return response.status_code == 200
    
    def disable_host_acl(self, host_id: int) -> bool:
        """Disable ACL for a proxy host"""
        data = {"access_list_id": None}
        response = self.put(f"/nginx/proxy-hosts/{host_id}", json=data)
        return response.status_code == 200
    
    # =========================================================================
    # Certificate Methods
    # =========================================================================
    
    def list_certificates(self) -> List[Dict]:
        """List all SSL certificates"""
        response = self.get("/nginx/certificates")
        response.raise_for_status()
        return response.json()
    
    def get_certificate(self, cert_id: int) -> Dict:
        """Get a specific certificate"""
        response = self.get(f"/nginx/certificates/{cert_id}")
        response.raise_for_status()
        return response.json()
    
    def find_certificate(self, domain: str) -> Optional[Dict]:
        """Find certificate by domain"""
        certs = self.list_certificates()
        for cert in certs:
            if domain in cert.get("domain_names", []):
                return cert
        return None
    
    def generate_certificate(self, domain: str, email: str,
                            dns_provider: Optional[str] = None,
                            dns_credentials: Optional[Dict] = None) -> Dict:
        """Generate a Let's Encrypt certificate"""
        is_wildcard = domain.startswith("*.")
        
        if is_wildcard and (not dns_provider or not dns_credentials):
            raise ValueError("Wildcard certificates require DNS provider and credentials")
        
        data = {
            "provider": "letsencrypt",
            "domain_names": [domain],
            "meta": {
                "letsencrypt_agree": True,
                "letsencrypt_email": email
            }
        }
        
        if is_wildcard:
            data["meta"].update({
                "dns_challenge": True,
                "dns_provider": dns_provider,
                "dns_provider_credentials": json.dumps(dns_credentials),
                "propagation_seconds": 60
            })
        
        response = self.post("/nginx/certificates", json=data)
        response.raise_for_status()
        return response.json()
    
    def delete_certificate(self, cert_id: int) -> bool:
        """Delete a certificate"""
        response = self.delete(f"/nginx/certificates/{cert_id}")
        return response.status_code == 200
    
    def download_certificate(self, cert_id: int, output_dir: str, cert_name: str) -> List[Path]:
        """Write a certificate's files to output_dir and return what was written.

        Raises CertificateDownloadError, listing what each route reported, when
        no key material could be retrieved. Returning a bare False here used to
        let full_backup claim success over a directory holding only metadata.
        """
        # Sanitize cert_name to prevent path traversal (defense in depth)
        cert_name = re.sub(r'[^a-zA-Z0-9._-]', '_', cert_name)

        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        attempts: List[str] = []

        # Preferred route: NPM returns the PEM bodies as JSON
        try:
            response = self.get(f"/nginx/certificates/{cert_id}/certificates",
                                headers={"Accept": "application/json"})
            if response.status_code != 200:
                attempts.append(f"JSON route: HTTP {response.status_code}")
            else:
                data = response.json()
                certificate = data.get("certificate") or ""
                private = data.get("private") or ""
                if not certificate or not private:
                    # NPM answers 200 with empty bodies for certificates whose
                    # key it does not hold; writing those would back up nothing
                    attempts.append("JSON route: response carried no key material")
                else:
                    written = [
                        Path(write_secret(output_path / f"{cert_name}.key", private)),
                    ]
                    cert_path = output_path / f"{cert_name}.crt"
                    cert_path.write_text(certificate)
                    written.append(cert_path)

                    if data.get("intermediate"):
                        chain_path = output_path / f"{cert_name}.chain.crt"
                        chain_path.write_text(data["intermediate"])
                        written.append(chain_path)

                    meta_path = output_path / f"{cert_name}_metadata.json"
                    meta_path.write_text(json.dumps(self.get_certificate(cert_id), indent=2))
                    written.append(meta_path)

                    return written
        except (requests.RequestException, ValueError, OSError) as exc:
            attempts.append(f"JSON route: {exc}")

        # Fallback: the legacy endpoint hands back a ZIP
        zip_path = output_path / f"{cert_name}.download.zip"
        try:
            response = self.get(f"/nginx/certificates/{cert_id}/download")
            if response.status_code != 200:
                attempts.append(f"ZIP route: HTTP {response.status_code}")
            else:
                zip_path.write_bytes(response.content)
                written = []
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        member_path = (output_path / member).resolve()
                        # startswith() would accept a sibling directory whose
                        # name merely shares the prefix, e.g. /backup-evil
                        if not member_path.is_relative_to(output_path):
                            attempts.append(f"ZIP route: skipped unsafe path {member!r}")
                            continue
                        zf.extract(member, output_path)
                        if member_path.is_file():
                            # The archive carries the key alongside the cert
                            # and its stored mode is whatever NPM chose
                            if member_path.suffix in (".key", ".pem"):
                                member_path.chmod(0o600)
                            written.append(member_path)
                if written:
                    return written
                attempts.append("ZIP route: archive was empty")
        except (requests.RequestException, zipfile.BadZipFile, OSError) as exc:
            attempts.append(f"ZIP route: {exc}")
        finally:
            if zip_path.exists():
                zip_path.unlink()

        raise CertificateDownloadError(
            f"certificate {cert_id}: " + "; ".join(attempts or ["no route succeeded"])
        )
    
    # =========================================================================
    # User Methods
    # =========================================================================
    
    def list_users(self) -> List[Dict]:
        """List all users"""
        response = self.get("/users")
        response.raise_for_status()
        return response.json()
    
    def create_user(self, username: str, email: str, password: str) -> Dict:
        """Create a new user"""
        data = {
            "name": username,
            "nickname": username,
            "email": email,
            "roles": ["admin"],
            "is_disabled": False,
            "auth": {
                "type": "password",
                "secret": password
            }
        }
        
        response = self.post("/users", json=data)
        response.raise_for_status()
        return response.json()
    
    def delete_user(self, user_id: int) -> bool:
        """Delete a user"""
        response = self.delete(f"/users/{user_id}")
        return response.status_code == 200 or response.text == "true"
    
    # =========================================================================
    # Access List Methods
    # =========================================================================
    
    def list_access_lists(self) -> List[Dict]:
        """List all access lists"""
        response = self.get("/nginx/access-lists")
        response.raise_for_status()
        return response.json()
    
    def get_access_list(self, list_id: int) -> Dict:
        """Get a specific access list with expanded details"""
        response = self.get(f"/nginx/access-lists/{list_id}?expand=items,clients")
        response.raise_for_status()
        return response.json()
    
    def create_access_list(self, name: str, satisfy_any: bool = False,
                          pass_auth: bool = False, items: List[Dict] = None,
                          clients: List[Dict] = None) -> Dict:
        """Create an access list"""
        data = {
            "name": name,
            "satisfy_any": satisfy_any,
            "pass_auth": pass_auth,
            "items": items or [],
            "clients": clients or []
        }
        
        response = self.post("/nginx/access-lists", json=data)
        response.raise_for_status()
        return response.json()
    
    def update_access_list(self, list_id: int, updates: Dict) -> Dict:
        """Update an access list"""
        current = self.get_access_list(list_id)
        
        data = {
            "name": current.get("name"),
            "satisfy_any": current.get("satisfy_any", False),
            "pass_auth": current.get("pass_auth", False),
            "items": current.get("items", []),
            "clients": current.get("clients", [])
        }
        data.update(updates)
        
        response = self.put(f"/nginx/access-lists/{list_id}", json=data)
        response.raise_for_status()
        return response.json()
    
    def delete_access_list(self, list_id: int) -> bool:
        """Delete an access list"""
        response = self.delete(f"/nginx/access-lists/{list_id}")
        return response.status_code == 200

    # =========================================================================
    # Settings Methods
    # =========================================================================

    def list_settings(self) -> List[Dict]:
        """List NPM's settings.

        In NPM 2.x this is effectively a single entry, `default-site`, which
        decides what nginx serves for an unrecognised Host header. Returned as
        a list all the same, because that is the shape the API uses and a later
        release may add to it.
        """
        response = self.get("/settings")
        response.raise_for_status()
        return response.json()

    def update_setting(self, setting_id: str, payload: Dict) -> Dict:
        """Write one setting by ID"""
        response = self.put(f"/settings/{setting_id}", json=payload)
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # Dashboard / Stats Methods
    # =========================================================================
    
    def get_dashboard_stats(self) -> Dict:
        """Collect the dashboard counts, distinguishing zero from unknown.

        A section that could not be read reports None instead of 0, and the
        reason lands in "failures". Defaulting to 0 made a sick NPM render
        identically to an empty one — "0 proxy hosts" read as a fact when it
        actually meant the request had failed.
        """
        stats: Dict[str, Any] = {
            "proxy_hosts": {"total": None, "enabled": None, "disabled": None},
            "certificates": {"total": None, "valid": None, "expired": None},
            "redirections": None,
            "streams": None,
            "users": None,
            "access_lists": None,
            "failures": [],
        }

        # HTTPError and JSONDecodeError subclass these two, so between them the
        # tuple covers a rejected request and an unparseable body alike
        expected = (requests.RequestException, ValueError, KeyError, NPMError)

        def record(section: str, exc: Exception) -> None:
            stats["failures"].append(f"{section}: {format_http_error(exc)}")

        def count_endpoint(section: str, endpoint: str) -> Optional[int]:
            try:
                response = self.get(endpoint)
                response.raise_for_status()
                return len(response.json())
            except expected as exc:
                record(section, exc)
                return None

        try:
            hosts = self.list_hosts()
            enabled = sum(1 for h in hosts if h.get("enabled"))
            stats["proxy_hosts"] = {
                "total": len(hosts),
                "enabled": enabled,
                "disabled": len(hosts) - enabled,
            }
        except expected as exc:
            record("proxy hosts", exc)

        try:
            certs = self.list_certificates()
            expired = sum(
                1 for c in certs
                if (days := cert_days_remaining(c)) is not None and days < 0
            )
            stats["certificates"] = {
                "total": len(certs),
                "valid": len(certs) - expired,
                "expired": expired,
            }
        except expected as exc:
            record("certificates", exc)

        stats["redirections"] = count_endpoint("redirections", "/nginx/redirection-hosts")
        stats["streams"] = count_endpoint("streams", "/nginx/streams")

        try:
            stats["users"] = len(self.list_users())
        except expected as exc:
            record("users", exc)

        try:
            stats["access_lists"] = len(self.list_access_lists())
        except expected as exc:
            record("access lists", exc)

        return stats
    
    # =========================================================================
    # Backup Methods
    # =========================================================================
    
    @staticmethod
    def _print_key_failures(failures: List["CertKeyFailure"], ssl_dir: Path) -> None:
        """Explain how to fetch key material the API refused to return.

        npm-api speaks to NPM over HTTP and has no access to the container, so
        it can only tell the caller what to run — hence a copy-paste command
        rather than an attempted fallback.
        """
        console.print(
            f"\n[yellow]⚠️  NPM's API returned no key material for "
            f"{len(failures)} certificate(s):[/yellow]"
        )
        for failure in failures:
            console.print(f"[yellow]   • {failure.cert_id} ({failure.name}) — "
                          f"{failure.reason}[/yellow]")

        console.print(
            "\n[cyan]   NPM's API exports Let's Encrypt certificates it issued, but not "
            "\n   certificates uploaded to it. To capture these, copy them off the "
            "\n   container filesystem on the NPM host (substitute your container "
            "\n   name from `docker ps`):[/cyan]"
        )
        for failure in failures:
            for path in failure.container_paths:
                # soft_wrap keeps the command on one line; a wrapped command is
                # not copy-pasteable, which is the whole point of printing it
                console.print(f"[cyan]     docker cp <container>:{path} "
                              f"{ssl_dir}/[/cyan]", soft_wrap=True)
            if len(failure.container_paths) > 1:
                console.print(f"[dim]       (certificate {failure.cert_id} reports no "
                              f"provider; try both, only one will exist)[/dim]")

    def full_backup(self, output_dir: Optional[str] = None,
                    include_keys: bool = False) -> BackupResult:
        """Perform a full backup of all configurations.

        Certificate private keys are only fetched when include_keys is set, so
        the default output is safe to sync or commit. Note that a backup taken
        without them cannot restore TLS on its own.
        """
        timestamp = datetime.now().strftime("%Y_%m_%d__%H_%M_%S")
        backup_path = Path(output_dir).expanduser() if output_dir else Path(self.config.backup_dir)
        
        # Create directories
        for subdir in [".user", ".settings", ".access_lists", ".Proxy_Hosts", ".ssl"]:
            (backup_path / subdir).mkdir(parents=True, exist_ok=True)

        full_config = {}
        # A section is only assigned once its fetch has returned, so a section
        # that failed is simply missing from the document — indistinguishable,
        # once the file is on disk, from a section that genuinely held nothing.
        # restore deletes what a backup says nothing about, so the file has to
        # carry the difference itself. Keyed by section name, not by the spaced
        # label result.failures uses, because that is what restore looks up.
        incomplete_sections: Dict[str, str] = {}
        result = BackupResult(path=str(backup_path))

        # Backup users
        try:
            users = self.list_users()
            full_config["users"] = users
            (backup_path / ".user" / f"users_{timestamp}.json").write_text(
                json.dumps(users, indent=2)
            )
            console.print(f"[green]✅ Backed up {len(users)} users[/green]")
        except Exception as e:
            result.failures.append(f"users: {e}")
            console.print(f"[yellow]⚠️ Failed to backup users: {e}[/yellow]")
        
        # Backup settings
        try:
            settings = self.list_settings()
            full_config["settings"] = settings
            (backup_path / ".settings" / f"settings_{timestamp}.json").write_text(
                json.dumps(settings, indent=2)
            )
            console.print("[green]✅ Backed up settings[/green]")
        except Exception as e:
            result.failures.append(f"settings: {e}")
            incomplete_sections["settings"] = str(e)
            console.print(f"[yellow]⚠️ Failed to backup settings: {e}[/yellow]")
        
        # Backup access lists
        try:
            access_lists = []
            for al in self.list_access_lists():
                full_al = self.get_access_list(al["id"])
                access_lists.append(full_al)
            
            full_config["access_lists"] = access_lists
            (backup_path / ".access_lists" / f"access_lists_{timestamp}.json").write_text(
                json.dumps(access_lists, indent=2)
            )
            console.print(f"[green]✅ Backed up {len(access_lists)} access lists[/green]")
        except Exception as e:
            result.failures.append(f"access lists: {e}")
            incomplete_sections["access_lists"] = str(e)
            console.print(f"[yellow]⚠️ Failed to backup access lists: {e}[/yellow]")
        
        # Backup proxy hosts
        try:
            hosts = self.list_hosts()
            full_config["proxy_hosts"] = hosts
            
            # Save all hosts metadata
            (backup_path / ".Proxy_Hosts" / f"all_hosts_{timestamp}.json").write_text(
                json.dumps(hosts, indent=2)
            )
            
            # Save individual host configs
            for host in hosts:
                domain = host.get("domain_names", ["unknown"])[0]
                domain_safe = re.sub(r'[^a-zA-Z0-9.]', '_', domain)
                host_dir = backup_path / ".Proxy_Hosts" / domain_safe
                host_dir.mkdir(parents=True, exist_ok=True)
                
                (host_dir / "proxy_config.json").write_text(
                    json.dumps(host, indent=2)
                )
            
            console.print(f"[green]✅ Backed up {len(hosts)} proxy hosts[/green]")
        except Exception as e:
            result.failures.append(f"proxy hosts: {e}")
            incomplete_sections["proxy_hosts"] = str(e)
            console.print(f"[yellow]⚠️ Failed to backup proxy hosts: {e}[/yellow]")
        
        # Backup certificates
        try:
            certs = self.list_certificates()
            full_config["certificates"] = certs

            keys_saved: List[str] = []

            for cert in certs:
                cert_id = cert["id"]
                cert_name = cert.get("nice_name") or cert.get("domain_names", ["cert"])[0]
                cert_name_safe = re.sub(r'[^a-zA-Z0-9.]', '_', cert_name)
                cert_dir = backup_path / ".ssl" / cert_name_safe
                cert_dir.mkdir(parents=True, exist_ok=True)

                (cert_dir / "certificate_meta.json").write_text(
                    json.dumps(cert, indent=2)
                )

                if not include_keys:
                    continue

                try:
                    self.download_certificate(cert_id, str(cert_dir), "cert")
                    keys_saved.append(f"{cert_id} ({cert_name})")
                except CertificateDownloadError as exc:
                    # Reported but not counted as a backup failure. NPM only
                    # exports certificates it issued through Let's Encrypt;
                    # uploaded ones fail here every single run, so treating
                    # that as fatal would break scheduled backups outright.
                    # The remedy is printed below instead.
                    result.key_failures.append(CertKeyFailure(
                        cert_id=cert_id,
                        name=cert_name,
                        provider=cert.get("provider"),
                        reason=str(exc).split(": ", 1)[-1],
                    ))

            console.print(f"[green]✅ Backed up metadata for {len(certs)} certificates[/green]")

            if not include_keys:
                console.print(
                    "[yellow]⚠️  Certificate private keys were NOT backed up. "
                    "This backup cannot restore TLS on its own — rerun with "
                    "--include-keys to capture key material.[/yellow]"
                )
            else:
                if keys_saved:
                    console.print(
                        f"[green]🔑 Saved key material for {len(keys_saved)} "
                        f"certificate(s) under {backup_path / '.ssl'}[/green]"
                    )
                    console.print(
                        "[red]🔐 This backup now contains unencrypted private keys. "
                        "Keep it off shared storage and out of version control.[/red]"
                    )
                if result.key_failures:
                    self._print_key_failures(result.key_failures, backup_path / ".ssl")
        except Exception as e:
            result.failures.append(f"certificates: {e}")
            incomplete_sections["certificates"] = str(e)
            console.print(f"[yellow]⚠️ Failed to backup certificates: {e}[/yellow]")

        # Only on a partial run: a clean backup must not grow a key that every
        # reader would then have to know to ignore.
        if incomplete_sections:
            full_config["incomplete_sections"] = incomplete_sections

        # Save full config
        full_config_path = backup_path / f"full_config_{timestamp}.json"
        full_config_path.write_text(json.dumps(full_config, indent=2))
        
        # Create latest symlink. exists() follows the link, so a symlink left
        # pointing at a pruned backup reads as absent and symlink_to() then
        # fails with FileExistsError; is_symlink() catches that case.
        latest_path = backup_path / LATEST_BACKUP_LINK
        if latest_path.is_symlink() or latest_path.exists():
            latest_path.unlink()
        latest_path.symlink_to(full_config_path.name)

        return result


# =============================================================================
# Global client instance
# =============================================================================

_client: Optional[NPMClient] = None

def get_client() -> NPMClient:
    """Get or create the API client"""
    global _client
    if _client is None:
        config = Config.load()
        
        # Check for default credentials
        if config.is_using_defaults():
            console.print("\n[red]╔══════════════════════════════════════════════════════════════╗[/red]")
            console.print("[red]║  ⚠️  NPM-API: Configuration Required                          ║[/red]")
            console.print("[red]╚══════════════════════════════════════════════════════════════╝[/red]")
            
            # Show where we searched
            console.print("\n[yellow]🔍 Searched for config file in:[/yellow]")
            for path in config._searched_paths:
                exists = "✓" if Path(path).exists() else "✗"
                color = "green" if Path(path).exists() else "grey"
                console.print(f"   [{color}]{exists} {path}[/{color}]")
            
            # Option 1: Environment variables (recommended)
            console.print("\n[cyan]━━━ Option 1: Environment Variables (Recommended) ━━━[/cyan]")
            console.print("[green]Best for: Docker, CI/CD, security-conscious setups[/green]\n")
            console.print("  export NPM_API_HOST=\"192.168.1.100\"")
            console.print("  export NPM_API_PORT=\"81\"")
            console.print("  [bold]export NPM_API_USER=\"admin@example.com\"[/bold]")
            console.print("  [bold]export NPM_API_PASS=\"your_password\"[/bold]")
            console.print("\n  # Or inline:")
            console.print("  NPM_API_USER=\"admin@example.com\" NPM_API_PASS=\"pass\" npm-api host list")
            
            # Option 2: Config file
            console.print("\n[cyan]━━━ Option 2: Config File ━━━[/cyan]")
            console.print("[green]Best for: Personal workstations, persistent config[/green]\n")
            console.print("  # Create one of these files:")
            console.print("  [bold]~/.config/npm-api/npm-api.conf[/bold]  (user config)")
            console.print("  [bold]/etc/npm-api/npm-api.conf[/bold]       (system-wide)")
            console.print("  [bold]./npm-api.conf[/bold]                  (current directory)")
            console.print("\n  # File contents:")
            console.print('  NGINX_IP="192.168.1.100"')
            console.print('  NGINX_PORT="81"')
            console.print('  [bold]API_USER="admin@example.com"[/bold]')
            console.print('  [bold]API_PASS="your_password"[/bold]')
            
            # Quick setup commands
            console.print("\n[cyan]━━━ Quick Setup Commands ━━━[/cyan]")
            console.print("  # User config (recommended):")
            console.print("  mkdir -p ~/.config/npm-api")
            console.print("  cat > ~/.config/npm-api/npm-api.conf << 'EOF'")
            console.print('  NGINX_IP="192.168.1.100"')
            console.print('  NGINX_PORT="81"')
            console.print('  API_USER="your_email@example.com"')
            console.print('  API_PASS="your_password"')
            console.print("  EOF")
            
            console.print("\n")
            raise typer.Exit(1)
        
        _client = NPMClient(config)
    return _client


# =============================================================================
# Display helpers
# =============================================================================

# Warn when a certificate is inside this many days of expiry
CERT_EXPIRY_WARN_DAYS = 30


def cert_days_remaining(cert: Dict) -> Optional[int]:
    """Days until a certificate expires; negative if it already has.

    NPM's API returns no "expired" flag on the certificate object, so this is
    derived from "expires_on". Returns None if that value is missing or
    unparseable.
    """
    raw = cert.get("expires_on")
    if not raw:
        return None

    try:
        # NPM sends "YYYY-MM-DD HH:MM:SS"; tolerate ISO-8601 with "Z" as well.
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None

    # Match naive/aware so the subtraction can't raise
    now = datetime.now(tz=expires.tzinfo) if expires.tzinfo else datetime.now()
    return (expires - now).days


def cert_status_label(cert: Dict) -> str:
    """Render a coloured validity label for a certificate."""
    days = cert_days_remaining(cert)

    if days is None:
        return "[yellow]❓ UNKNOWN[/yellow]"
    if days < 0:
        return f"[red]❌ EXPIRED {abs(days)}d AGO[/red]"
    if days <= CERT_EXPIRY_WARN_DAYS:
        return f"[yellow]⚠️ {days}d LEFT[/yellow]"
    return "[green]✅ VALID[/green]"


def print_json(payload: Any):
    """Emit raw JSON on stdout, unstyled so it stays pipeable into jq."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def display_domain(domain: str) -> str:
    """Render one domain for a human, with anything invisible spelled out.

    Display only — never feed the result back to NPM or into a backup, or the
    literal "\\u200b" becomes part of the stored name.

    A zero-width space inside app.exam<U+200B>ple.com makes it a different
    string from app.example.com while looking identical on screen, and Rich
    passes it straight through. The realistic cost is not an attack but a
    debugging dead end: a name pasted out of a ticket carries a stray
    invisible character, `host list` shows what looks like the right name, and
    the certificate quietly fails to match it. Spelling the character out is
    what turns that into a five-second diagnosis.

    Escaped by Unicode category rather than by a list of known offenders:
    every C* (control, format, surrogate, private use, unassigned — zero-width
    spaces and every bidi override live in Cf) and every separator that is not
    a plain ASCII space.

    The result is then markup-escaped. Rich reads square brackets in any string
    handed to console.print or Table.add_row, so an un-escaped "a[b]c" already
    renders as "ac" today and a name containing "[/]" raises MarkupError out of
    a command that has not written anything yet.
    """
    rendered = []
    for char in str(domain):
        category = unicodedata.category(char)
        if category.startswith("C") or (category.startswith("Z") and char != " "):
            rendered.append(f"\\u{ord(char):04x}")
        else:
            rendered.append(char)
    return escape("".join(rendered))


def display_domains(domains, empty: str = "") -> str:
    """Comma-join a list of domains for display, each one escaped.

    The companion to display_domain, and the reason the many bare
    `', '.join(domain_names)` sites were consolidated: one of them missing the
    escaping is exactly the case where the operator is already confused.
    """
    return ", ".join(display_domain(d) for d in (domains or [])) or empty


# =============================================================================
# CLI Commands - Main
# =============================================================================

@app.command()
def info(as_json: bool = typer.Option(False, "--json", help="Emit raw JSON on stdout")):
    """Display script variables and dashboard information"""
    client = get_client()
    config = client.config

    if as_json:
        if not client.ensure_token():
            console.print(f"[red]❌ {client.auth_error or 'Failed to authenticate'}[/red]")
            raise typer.Exit(1)
        # The API user is included but no token or password: this output is
        # meant to be pipeable and pasteable into an issue
        stats = client.get_dashboard_stats()
        print_json({
            "version": VERSION,
            "config_source": config.get_config_info(),
            "base_url": config.base_url,
            "nginx_ip": config.nginx_ip,
            "api_user": config.api_user,
            "data_dir": config.data_dir_id,
            "stats": stats,
        })
        # Counts a section could not supply are null, never 0. Exit non-zero to
        # match, so `jq` sees the whole document but the shell still sees failure
        if stats["failures"]:
            raise typer.Exit(1)
        return

    out_console.print(f"\n[yellow]Script Info: [green]{VERSION}[/green][/yellow]")
    out_console.print(f"[green]Config from[/green] : {config.get_config_info()}")
    out_console.print(f"[green]BASE URL[/green]   : {config.base_url}")
    out_console.print(f"[green]NGINX IP[/green]   : {config.nginx_ip}")
    out_console.print(f"[green]USER NPM[/green]   : {config.api_user}")
    out_console.print(f"[green]BACKUP DIR[/green] : {config.data_dir_id}")
    
    # Dashboard
    if not client.ensure_token():
        # Exit non-zero: the config lines above are printed unconditionally, so
        # a bare return would let an unusable configuration look like success.
        console.print(f"[red]❌ {client.auth_error or 'Failed to authenticate'}[/red]")
        raise typer.Exit(1)

    stats = client.get_dashboard_stats()

    def count(value: Any, colour: str = "") -> str:
        """Render a count, or '?' where the section could not be read."""
        if value is None:
            return "[dim]?[/dim]"
        return f"[{colour}]{value}[/{colour}]" if colour else str(value)

    out_console.print("\n[cyan]📊 NGINX Proxy Manager Dashboard 🔧[/cyan]")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Component", style="white")
    table.add_column("Status", justify="right")

    table.add_row("🌐 Proxy Hosts", count(stats['proxy_hosts']['total'], "yellow"))
    table.add_row("├─ Enabled", count(stats['proxy_hosts']['enabled'], "green"))
    table.add_row("└─ Disabled", count(stats['proxy_hosts']['disabled'], "red"))
    table.add_row("🔒 Certificates", count(stats['certificates']['total'], "yellow"))
    table.add_row("├─ Valid", count(stats['certificates']['valid'], "green"))
    table.add_row("└─ Expired", count(stats['certificates']['expired'], "red"))
    table.add_row("🔄 Redirections", count(stats['redirections']))
    table.add_row("🔌 Stream Hosts", count(stats['streams']))
    table.add_row("🔒 Access Lists", count(stats['access_lists']))
    table.add_row("👥 Users", count(stats['users']))

    out_console.print(table)

    if stats["failures"]:
        # Exit non-zero so a health check cannot read '?' as a clean run
        console.print(f"\n[red]❌ {len(stats['failures'])} section(s) could not "
                      f"be read — the counts above are incomplete:[/red]")
        for failure in stats["failures"]:
            console.print(f"[red]   • {failure}[/red]")
        raise typer.Exit(1)

    out_console.print("\n[yellow]💡 Use --help to see available commands[/yellow]")


@app.command()
def check_token():
    """Check current token info"""
    client = get_client()
    
    console.print("\n[cyan]🔑 Checking token validity...[/cyan]")
    
    if client.load_token():
        expiry = Path(client.config.expiry_file).read_text().strip()
        console.print(f"[green]✅ Token is valid[/green]")
        console.print(f"[yellow]📅 Expires: {expiry}[/yellow]")
    else:
        console.print("[yellow]⚠️ Token invalid or expired. Generating new token...[/yellow]")
        if client.generate_token():
            console.print("[green]✅ New token generated[/green]")
        else:
            console.print(f"[red]❌ {client.auth_error or 'Failed to generate token'}[/red]")
            raise typer.Exit(1)


@app.command()
def backup(
    output_dir: str = typer.Option(None, "-o", "--output",
                                   help="Directory to write the backup into "
                                        "(default: the configured data directory)"),
    include_keys: bool = typer.Option(False, "--include-keys",
                                      help="Also download certificate private keys. "
                                           "Written unencrypted at mode 600")
):
    """Backup all configurations.

    Without --include-keys the backup holds configuration and certificate
    metadata only, which is enough to rebuild hosts but not to serve TLS.
    """
    client = get_client()

    console.print("\n[yellow]📦 Starting full backup...[/yellow]")
    if include_keys:
        console.print("[yellow]🔑 Including certificate private keys[/yellow]")

    try:
        result = client.full_backup(output_dir=output_dir, include_keys=include_keys)
    except Exception as e:
        console.print(f"[red]❌ Backup failed: {e}[/red]")
        raise typer.Exit(1)

    if result.complete:
        console.print(f"\n[green]✅ Backup completed![/green]")
    else:
        # Exit non-zero so a scheduled run does not record a partial
        # backup as a success
        console.print(f"\n[red]❌ Backup incomplete — {len(result.failures)} "
                      f"section(s) could not be written:[/red]")
        for failure in result.failures:
            console.print(f"[red]   • {failure}[/red]")

    console.print(f"[cyan]📂 Backup location: {result.path}[/cyan]")

    if not result.complete:
        raise typer.Exit(1)


# Sections full_backup writes into full_config_<timestamp>.json. Restore reads
# three of them. `users` is skipped because NPM's API never exports password
# material, so they could only be recreated with invented passwords;
# `certificates` is read but never written, because uploading key material
# would mean POSTing private keys over what is plain HTTP by default.
BACKUP_SECTIONS = ("proxy_hosts", "access_lists", "certificates", "settings", "users")
RESTORED_SECTIONS = ("access_lists", "proxy_hosts", "settings")

# Every section restore walks, and so every section it has to satisfy itself
# about before it deletes anything. `certificates` is here despite never being
# written back: matching reads every row of it, and a bad row there crashes the
# run just as readily.
VALIDATED_SECTIONS = RESTORED_SECTIONS + ("certificates",)


def load_backup(source: str) -> Dict:
    """Read a full_config backup, given either the file or its directory.

    Returns {"path": Path, "data": dict}. Raises NPMError with something the
    user can act on rather than letting a JSONDecodeError or a KeyError reach
    the terminal.
    """
    path = Path(source).expanduser()

    if path.is_dir():
        directory = path
        latest = directory / LATEST_BACKUP_LINK
        # exists() follows the symlink, so a link left pointing at a pruned
        # backup reads as absent here and falls through to the glob.
        if latest.exists():
            path = latest.resolve()
        else:
            # The link itself has to come out of the candidates. It matches the
            # glob, Path.glob lists dangling symlinks because it never stats
            # them, and every real backup is named full_config_<digit>... —
            # so lexicographic sorting put the broken link last and [-1]
            # picked it every time, turning a directory full of good backups
            # into "No such backup".
            candidates = sorted(c for c in directory.glob(BACKUP_GLOB)
                                if c.name != LATEST_BACKUP_LINK)
            if not candidates:
                raise NPMError(f"No {BACKUP_GLOB} backup found in {directory}")
            path = candidates[-1]

    if not path.exists():
        raise NPMError(f"No such backup: {path}")

    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise NPMError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise NPMError(f"Could not read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise NPMError(f"{path} does not hold a backup object")
    if not any(key in data for key in BACKUP_SECTIONS):
        raise NPMError(f"{path} holds none of the sections a backup should "
                       f"({', '.join(BACKUP_SECTIONS)})")

    return {"path": path, "data": data}


def backup_section(data: Dict, key: str) -> Tuple[Optional[List], Optional[str]]:
    """The section's rows, or None with a reason the backup cannot speak for it.

    An absent section means the backup does not know what was there, which
    is not the same as knowing there was nothing. Restoring "nothing" over a
    live section deletes it, so absence must never be flattened to an empty
    list. An explicit [] is a positive statement and is honoured.
    """
    recorded = data.get("incomplete_sections")
    # Written by full_backup when the fetch for a section failed. Anything else
    # under that key came from a hand-edit and is ignored rather than trusted.
    if isinstance(recorded, dict) and key in recorded:
        return None, f"the backup records it as incomplete ({recorded[key]})"

    if key not in data:
        return None, "the backup has no such section"

    rows = data[key]
    if rows is None:
        return None, "the backup holds it as null"
    if not isinstance(rows, list):
        return None, (f"the backup holds it as a {type(rows).__name__}, "
                      f"not a list of records")

    return rows, None


def validate_backup_rows(data: Dict) -> None:
    """Refuse a backup whose rows are not the records the rebuild loop assumes.

    Ordering is the whole point. restore_acl_payload and host_config_payload
    both run in the rebuild loop, which is reached only after every host and
    access list on the target has been deleted, so an AttributeError raised
    there leaves a live instance emptied and not refilled. Walking the file
    first costs one pass over data already in memory.
    """
    for key in VALIDATED_SECTIONS:
        rows = data.get(key)
        # Absent or null is backup_section's business: those sections are
        # skipped rather than restored, so their shape decides nothing.
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise NPMError(f"Backup section '{key}' is a {type(rows).__name__}, "
                           f"not a list of records — nothing was changed")

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise NPMError(f"Backup section '{key}' entry {index} is a "
                               f"{type(row).__name__}, not a record — nothing "
                               f"was changed")
            if key != "access_lists":
                continue
            for nested_key in ("items", "clients"):
                nested = row.get(nested_key)
                if nested is None:
                    continue
                if not isinstance(nested, list):
                    raise NPMError(
                        f"Backup section 'access_lists' entry {index} holds "
                        f"'{nested_key}' as a {type(nested).__name__}, not a "
                        f"list — nothing was changed")
                for nested_index, nested_row in enumerate(nested):
                    if not isinstance(nested_row, dict):
                        raise NPMError(
                            f"Backup section 'access_lists' entry {index}, "
                            f"'{nested_key}' entry {nested_index} is a "
                            f"{type(nested_row).__name__}, not a record — "
                            f"nothing was changed")


def cert_match_key(cert: Dict) -> Optional[frozenset]:
    """A certificate's domain list as an order-insensitive key, or None.

    None means the metadata holds nothing usable, which NPM does emit for some
    uploaded certificates — callers fall back to the name rather than treating
    every such certificate as identical to every other.
    """
    names = {str(d).strip().lower() for d in (cert.get("domain_names") or [])
             if str(d).strip()}
    return frozenset(names) or None


def map_certificates(backup_certs: List[Dict],
                     target_certs: List[Dict]) -> Dict[int, Optional[int]]:
    """Map each backed-up certificate ID onto one in the target, or to None.

    Matched on the set of domain names, not on nice_name: the name is free text
    a user can edit at any time, while the domain set is what decides whether a
    certificate can serve a given host.

    nice_name is the fallback whenever the domain lookup finds nothing, on
    either side. That breadth is deliberate — the common case is not a backup
    with poor metadata but an *uploaded* certificate installed here whose
    domain_names NPM never filled in, which the backup does record. Narrowing
    the fallback to "the backup's own key was unusable" would miss it.

    IDs are never carried across. NPM assigns them on create, so a backup's
    certificate_id means nothing in a different instance — writing one back
    unchecked is how a host ends up pointing at a certificate that is not there.
    """
    by_domains: Dict[frozenset, int] = {}
    by_name: Dict[str, int] = {}
    for cert in target_certs:
        key = cert_match_key(cert)
        if key is not None:
            by_domains.setdefault(key, cert.get("id"))
        name = str(cert.get("nice_name") or "").strip().lower()
        if name:
            by_name.setdefault(name, cert.get("id"))

    mapping: Dict[int, Optional[int]] = {}
    for cert in backup_certs:
        key = cert_match_key(cert)
        found = by_domains.get(key) if key is not None else None
        if found is None:
            name = str(cert.get("nice_name") or "").strip().lower()
            found = by_name.get(name) if name else None
        mapping[cert.get("id")] = found
    return mapping


def restore_host_overrides(host: Dict, cert_map: Dict[int, Optional[int]],
                           acl_map: Dict[int, Optional[int]]) -> tuple:
    """Rewrite a backed-up host's ID references for the target.

    Returns (overrides, notes). Anything that cannot be resolved is cleared
    rather than carried over: a host pointing at an ID the target does not have
    is the failure this whole tool exists to catch.
    """
    notes: List[str] = []
    overrides: Dict[str, Any] = {}

    old_cert = host.get("certificate_id") or None
    new_cert = cert_map.get(old_cert) if old_cert else None
    overrides["certificate_id"] = new_cert
    if old_cert and new_cert is None:
        notes.append(f"certificate {old_cert} has no match here — HTTP-only")
        # Forcing SSL with no certificate redirects to an HTTPS listener NPM
        # never renders, which is strictly worse than plain HTTP.
        overrides["ssl_forced"] = False
        overrides["hsts_enabled"] = False

    old_acl = host.get("access_list_id") or None
    new_acl = acl_map.get(old_acl) if old_acl else None
    # 0, not None: NPM's own way of saying "no access list linked".
    overrides["access_list_id"] = new_acl or 0
    if old_acl and new_acl is None:
        notes.append(f"access list {old_acl} was not restored — access control dropped")

    return overrides, notes


def restore_acl_payload(acl: Dict) -> tuple:
    """Reduce a backed-up access list to create_access_list's arguments.

    Returns (kwargs, notes). Item and client rows are rebuilt field by field
    because the backup carries their `id` and `access_list_id`, which belong to
    the instance the backup came from.
    """
    notes: List[str] = []
    items = []
    for item in acl.get("items") or []:
        username = item.get("username")
        password = item.get("password")
        if not password:
            notes.append(f"user '{username}' has no password in the backup")
        items.append({"username": username, "password": password or ""})

    clients = [{"address": client.get("address"),
                "directive": client.get("directive") or "allow"}
               for client in acl.get("clients") or []]

    return {
        "name": acl.get("name"),
        "satisfy_any": bool(acl.get("satisfy_any")),
        "pass_auth": bool(acl.get("pass_auth")),
        "items": items,
        "clients": clients,
    }, notes


@app.command()
def restore(
    source: str = typer.Argument(..., help="Backup file, or a directory holding one"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
):
    """
    Rebuild proxy hosts, access lists and settings from a backup.

    Intended for a freshly set up NPM. Restoring into one that already holds
    hosts deletes them first — this replaces configuration, it does not
    reconcile it.

    Three things are deliberately not restored. Users, because NPM's API never
    exports password material. Certificates, because uploading them would mean
    sending private keys to an endpoint that is plain HTTP by default; instead
    each host is matched to a certificate already present here, and any host
    whose certificate has no match comes back HTTP-only and is named so you can
    repoint it. Settings the target does not already define, so a backup from a
    later NPM cannot introduce ones this instance has never had.

    Examples:
        restore ~/.npm-api/backups
        restore ~/.npm-api/backups/full_config_2026_08_24__11_20_02.json
    """
    client = get_client()
    backup = load_backup(source)
    data = backup["data"]

    # Before the target is even read, let alone emptied.
    validate_backup_rows(data)

    # Only the sections the backup can actually speak for. A section that is
    # missing from it is left out entirely rather than read as [], because the
    # delete loops below take [] as an instruction to wipe.
    sections: Dict[str, List] = {}
    for key in VALIDATED_SECTIONS:
        rows, reason = backup_section(data, key)
        if reason is not None:
            console.print(f"[yellow]⚠️  Skipping {key.replace('_', ' ')}: "
                          f"{reason}.[/yellow]")
            # What skipping costs is not the same for every section, and for
            # certificates it is emphatically not "nothing happens": matching
            # is the only thing that maps the backup's certificate IDs onto
            # this NPM's, so without it every host comes back HTTP-only.
            if key == "certificates":
                console.print("[yellow]   Every restored host will come back "
                              "without TLS — there is nothing to match its "
                              "certificate against.[/yellow]")
            else:
                console.print("[yellow]   Nothing in that section will be "
                              "deleted or created here.[/yellow]")
            continue
        sections[key] = rows

    backup_acls = sections.get("access_lists") or []
    backup_hosts = sections.get("proxy_hosts") or []
    backup_certs = sections.get("certificates") or []
    backup_settings = sections.get("settings") or []

    # A skipped section reads as empty here, so this also catches the backup
    # that can speak for nothing at all — which would otherwise run to
    # completion having done nothing and exited 0.
    if not any((backup_acls, backup_hosts, backup_settings)):
        console.print(f"[yellow]{backup['path']} holds nothing this command "
                      f"restores ({', '.join(RESTORED_SECTIONS)})[/yellow]")
        raise typer.Exit(1)

    target_hosts = client.list_hosts()
    target_acls = client.list_access_lists()
    target_certs = client.list_certificates()

    # Not fatal. Settings is the least of the three sections, and NPM answers
    # /settings with 403 for a non-admin token — no reason that should stop the
    # hosts being restored.
    try:
        target_settings = client.list_settings()
    except (requests.HTTPError, NPMError) as exc:
        console.print(f"[yellow]⚠️  Could not read this NPM's settings "
                      f"({format_http_error(exc)}) — the settings section will be "
                      f"skipped[/yellow]")
        target_settings = []
    target_setting_ids = {s.get("id") for s in target_settings}

    cert_map = map_certificates(backup_certs, target_certs)
    # For the preview, assume every access list in the backup will be created.
    # A host referencing one the backup does not contain is a real gap and has
    # to show up here rather than only at apply time.
    preview_acl_map = {acl.get("id"): acl.get("id") for acl in backup_acls}

    predicted_notes = []
    for host in backup_hosts:
        _, notes = restore_host_overrides(host, cert_map, preview_acl_map)
        if notes:
            predicted_notes.append((host, notes))

    skipped_settings = [s.get("id") for s in backup_settings
                        if s.get("id") not in target_setting_ids]

    if preview:
        console.print(f"\n[cyan]📋 Restore Preview[/cyan]")
        console.print(f"[cyan]   From: [yellow]{backup['path']}[/yellow][/cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Section", style="white")
        table.add_column("In backup", style="green", justify="right")
        table.add_column("Here now", style="yellow", justify="right")
        table.add_column("Action", style="magenta")
        for label, key, rows, here, action in (
                ("Access lists", "access_lists", backup_acls, target_acls,
                 "replaced"),
                ("Proxy hosts", "proxy_hosts", backup_hosts, target_hosts,
                 "replaced"),
                ("Settings", "settings", backup_settings, target_settings,
                 "written where already defined"),
                ("Certificates", "certificates", backup_certs, target_certs,
                 "left alone; matched only")):
            # A skipped section counts zero rows, and "0 … replaced" reads as
            # "the backup had none, so all of yours go" — the opposite of what
            # is about to happen.
            if key in sections:
                table.add_row(label, str(len(rows)), str(len(here)), action)
            else:
                table.add_row(label, "—", str(len(here)),
                              "[yellow]skipped — left as it is[/yellow]")
        table.add_row("Users", str(len(data.get("users") or [])), "—", "not restored")
        console.print(table)

        if backup_certs:
            cert_table = Table(show_header=True, header_style="bold cyan",
                               title="Certificate matching")
            cert_table.add_column("In backup", style="white")
            cert_table.add_column("Matches here", style="green")
            for cert in backup_certs:
                matched = cert_map.get(cert.get("id"))
                label = ", ".join(cert.get("domain_names") or []) or \
                    str(cert.get("nice_name") or "unnamed")
                cert_table.add_row(
                    f"{cert.get('id')}: {label}",
                    f"certificate {matched}" if matched else "[red]no match[/red]")
            console.print(cert_table)

        if predicted_notes:
            console.print(f"\n[yellow]⚠️  {len(predicted_notes)} host(s) come back with "
                          f"something dropped:[/yellow]")
            for host, notes in predicted_notes:
                domains = ", ".join(host.get("domain_names") or []) or "(no domains)"
                console.print(f"   [yellow]{domains}: {'; '.join(notes)}[/yellow]")
            console.print(f"   [dim]repoint them afterwards with "
                          f"`host bulk-update certificate_id <id> --ids ...`[/dim]")

        for setting_id in skipped_settings:
            console.print(f"[dim]Setting '{setting_id}' is not defined on this NPM "
                          f"— skipped[/dim]")

    # Only what the backup can speak for is replaced. A section it says nothing
    # about is left standing, so it must not appear in the delete loops below
    # and must not be counted in the warning above them.
    doomed_hosts = target_hosts if "proxy_hosts" in sections else []
    doomed_acls = target_acls if "access_lists" in sections else []

    if target_hosts or target_acls:
        console.print(f"\n[red]⚠️  This NPM is not empty. "
                      f"{len(target_hosts)} proxy host(s) and {len(target_acls)} access "
                      f"list(s) will be DELETED and replaced by the backup.[/red]")
        console.print("[red]   Nothing is reconciled or merged. Certificates and users "
                      "here are left alone.[/red]")
        if len(doomed_hosts) != len(target_hosts) or len(doomed_acls) != len(target_acls):
            console.print(f"[yellow]   Except for the skipped section(s) above: "
                          f"only {len(doomed_hosts)} proxy host(s) and "
                          f"{len(doomed_acls)} access list(s) are actually "
                          f"deleted.[/yellow]")
        console.print("[yellow]   If you have not already, cancel and run "
                      "`npm-api backup` first.[/yellow]")

    confirm_bulk(yes, "Restore over the current configuration?")

    try:
        snapshot = write_state_snapshot(
            client.config, "pre_restore",
            {"proxy_hosts": target_hosts, "access_lists": target_acls,
             "settings": target_settings, "restored_from": str(backup["path"])})
    except OSError as exc:
        console.print(f"[red]❌ Could not write the pre-restore snapshot: {exc}[/red]")
        console.print("[red]   Refusing to delete configuration that was not recorded "
                      "first[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]Pre-restore snapshot: {snapshot}[/dim]")

    success_count = 0
    error_count = 0

    with console.status("[bold green]Restoring...") as status:
        # Hosts before access lists: NPM will not drop an access list a host
        # still references.
        for host in doomed_hosts:
            host_id = host.get("id")
            status.update(f"[bold green]Removing host {host_id}...")
            try:
                if not client.delete_host(host_id):
                    raise NPMError("NPM refused the delete")
                console.print(f"  [dim]− removed host {host_id}[/dim]")
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Could not remove host {host_id} — "
                              f"{format_http_error(exc)}[/red]")
                error_count += 1

        for acl in doomed_acls:
            acl_id = acl.get("id")
            status.update(f"[bold green]Removing access list {acl_id}...")
            try:
                if not client.delete_access_list(acl_id):
                    raise NPMError("NPM refused the delete")
                console.print(f"  [dim]− removed access list {acl_id}[/dim]")
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Could not remove access list {acl_id} — "
                              f"{format_http_error(exc)}[/red]")
                error_count += 1

        # Access lists first: a host carries access_list_id, so the new IDs have
        # to exist before any host is written.
        acl_map: Dict[int, Optional[int]] = {}
        for acl in backup_acls:
            old_id = acl.get("id")
            kwargs, notes = restore_acl_payload(acl)
            status.update(f"[bold green]Creating access list {kwargs['name']}...")
            try:
                created = client.create_access_list(**kwargs)
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Access list '{kwargs['name']}' — "
                              f"{format_http_error(exc)}[/red]")
                acl_map[old_id] = None
                error_count += 1
                continue
            acl_map[old_id] = created.get("id")
            console.print(f"  [green]✅ Access list '{kwargs['name']}' → "
                          f"{created.get('id')}[/green]")
            for note in notes:
                console.print(f"     [yellow]⚠️  {note} — set it again in NPM[/yellow]")
            success_count += 1

        # Counted from what actually happened, not from the preview: an access
        # list that failed to create above leaves more hosts degraded than the
        # preview predicted, and the closing advice has to reflect that.
        degraded = 0
        for host in backup_hosts:
            domains = ", ".join(host.get("domain_names") or []) or "(no domains)"
            overrides, notes = restore_host_overrides(host, cert_map, acl_map)
            status.update(f"[bold green]Creating {domains}...")
            try:
                created = client.create_host_from(host, overrides)
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ {domains} — {format_http_error(exc)}[/red]")
                error_count += 1
                continue
            console.print(f"  [green]✅ {domains} → host {created.get('id')}[/green]")
            for note in notes:
                console.print(f"     [yellow]⚠️  {note}[/yellow]")
            if notes:
                degraded += 1
            success_count += 1

        for setting in backup_settings:
            setting_id = setting.get("id")
            if setting_id not in target_setting_ids:
                continue
            status.update(f"[bold green]Writing setting {setting_id}...")
            try:
                client.update_setting(setting_id, {"value": setting.get("value"),
                                                   "meta": setting.get("meta") or {}})
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Setting '{setting_id}' — "
                              f"{format_http_error(exc)}[/red]")
                error_count += 1
                continue
            console.print(f"  [green]✅ Setting '{setting_id}'[/green]")
            success_count += 1

    if degraded:
        console.print(f"\n[yellow]{degraded} host(s) came back without the "
                      f"certificate or access list they had. Install the certificates "
                      f"in NPM, then repoint them:[/yellow]")
        console.print("   [dim]npm-api cert list[/dim]")
        console.print("   [dim]npm-api host bulk-update certificate_id <id> "
                      "--pattern <domain>[/dim]")

    print_bulk_summary(success_count, error_count)


@app.command()
def show_defaults():
    """Show default settings for host creation"""
    defaults = ProxyHostDefaults()
    
    out_console.print("\n[yellow]📝 Default Settings for Creating Hosts:[/yellow]")
    out_console.print("\n[green]Basic Settings:[/green]")
    out_console.print(f"  Forward Scheme:          [cyan]{defaults.forward_scheme}[/cyan]")
    out_console.print(f"  Caching Enabled:         {'[green]true[/green]' if defaults.caching_enabled else '[red]false[/red]'}")
    out_console.print(f"  Block Exploits:          {'[green]true[/green]' if defaults.block_exploits else '[red]false[/red]'}")
    out_console.print(f"  Allow Websocket Upgrade: {'[green]true[/green]' if defaults.allow_websocket_upgrade else '[red]false[/red]'}")
    
    out_console.print("\n[green]SSL Settings:[/green]")
    out_console.print(f"  HTTP/2 Support:          {'[green]true[/green]' if defaults.http2_support else '[red]false[/red]'}")
    out_console.print(f"  SSL Forced:              {'[green]true[/green]' if defaults.ssl_forced else '[red]false[/red]'}")
    out_console.print(f"  HSTS Enabled:            {'[green]true[/green]' if defaults.hsts_enabled else '[red]false[/red]'}")
    out_console.print(f"  HSTS Subdomains:         {'[green]true[/green]' if defaults.hsts_subdomains else '[red]false[/red]'}")


# =============================================================================
# CLI Commands - Hosts
# =============================================================================

@host_app.command("list")
def host_list(
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON instead of a table")
):
    """List all proxy hosts"""
    client = get_client()

    hosts = client.list_hosts()

    if as_json:
        print_json(hosts)
        return

    if not hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return

    table = Table(title="Proxy Hosts", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="yellow", justify="right")
    table.add_column("Domain", style="green")
    table.add_column("Status", justify="center")
    table.add_column("SSL", justify="center")
    table.add_column("Forward To", style="cyan")
    
    for host in hosts:
        host_id = str(host.get("id", "?"))
        domain = display_domains(host.get("domain_names", ["?"]))
        enabled = host.get("enabled", False)
        status = "[green]enabled[/green]" if enabled else "[red]disabled[/red]"
        cert_id = host.get("certificate_id")
        ssl = f"[cyan]{cert_id}[/cyan]" if cert_id else "[red]✘[/red]"
        forward = f"{host.get('forward_scheme', 'http')}://{host.get('forward_host', '?')}:{host.get('forward_port', '?')}"
        
        table.add_row(host_id, domain, status, ssl, forward)
    
    out_console.print(table)


@host_app.command("show")
def host_show(
    host_id: int = typer.Argument(..., help="Host ID to show"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON instead of a summary")
):
    """Show details of a specific proxy host"""
    client = get_client()

    try:
        host = client.get_host(host_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Host ID {host_id} not found[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(host)
        return

    out_console.print(f"\n[yellow]📋 Host Details:[/yellow]")
    out_console.print(f"  [cyan]ID:[/cyan] {host.get('id')}")
    out_console.print(f"  [cyan]Domains:[/cyan] {display_domains(host.get('domain_names', []))}")
    out_console.print(f"  [cyan]Forward Host:[/cyan] {host.get('forward_host')}")
    out_console.print(f"  [cyan]Forward Port:[/cyan] {host.get('forward_port')}")
    out_console.print(f"  [cyan]Forward Scheme:[/cyan] {host.get('forward_scheme')}")
    out_console.print(f"  [cyan]Enabled:[/cyan] {'[green]Yes[/green]' if host.get('enabled') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]Certificate ID:[/cyan] {host.get('certificate_id') or 'None'}")
    out_console.print(f"  [cyan]SSL Forced:[/cyan] {'[green]Yes[/green]' if host.get('ssl_forced') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]HTTP/2:[/cyan] {'[green]Yes[/green]' if host.get('http2_support') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]Block Exploits:[/cyan] {'[green]Yes[/green]' if host.get('block_exploits') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]Caching:[/cyan] {'[green]Yes[/green]' if host.get('caching_enabled') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]Websocket:[/cyan] {'[green]Yes[/green]' if host.get('allow_websocket_upgrade') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]Trust Forwarded Proto:[/cyan] "
                      f"{'[green]Yes[/green]' if host.get('trust_forwarded_proto') else '[red]No[/red]'}")
    out_console.print(f"  [cyan]HSTS:[/cyan] {'[green]Yes[/green]' if host.get('hsts_enabled') else '[red]No[/red]'}"
                      f"{' [dim](+subdomains)[/dim]' if host.get('hsts_subdomains') else ''}")
    out_console.print(f"  [cyan]Access List ID:[/cyan] {host.get('access_list_id') or 'None'}")

    locations = host.get('locations') or []
    out_console.print(f"  [cyan]Custom Locations:[/cyan] {len(locations) or 'None'}")
    for loc in locations:
        scheme = loc.get('forward_scheme', 'http')
        out_console.print(f"    [dim]{loc.get('path', '?')}[/dim] → "
                          f"{scheme}://{loc.get('forward_host', '?')}:{loc.get('forward_port', '?')}")

    if host.get('advanced_config'):
        out_console.print(f"\n  [cyan]Advanced Config:[/cyan]")
        out_console.print(Syntax(host['advanced_config'], "nginx", theme="monokai"))


@host_app.command("search")
def host_search(
    search: str = typer.Argument(..., help="Domain name to search"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON on stdout")
):
    """Search proxy hosts by domain name"""
    client = get_client()

    hosts = client.search_hosts(search)

    if as_json:
        print_json(hosts)
        return

    if not hosts:
        console.print(f"[yellow]No hosts found matching '{search}'[/yellow]")
        return

    for host in hosts:
        out_console.print(f"  [yellow]{host.get('id'):4}[/yellow] "
                          f"[green]{display_domains(host.get('domain_names', []))}[/green]")


@host_app.command("create")
def host_create(
    domain: str = typer.Argument(..., help="Domain name"),
    forward_host: str = typer.Option(..., "-i", "--forward-host", help="Forward host IP/hostname"),
    forward_port: int = typer.Option(..., "-p", "--forward-port", help="Forward port"),
    forward_scheme: str = typer.Option("http", "-f", "--forward-scheme", help="Forward scheme (http/https)"),
    block_exploits: bool = typer.Option(True, "-b", "--block-exploits", help="Block exploits"),
    caching: bool = typer.Option(False, "-c", "--cache", help="Enable caching"),
    websocket: bool = typer.Option(False, "-w", "--websocket", help="Allow websocket upgrade"),
    http2: bool = typer.Option(False, "--http2", help="Enable HTTP/2"),
    advanced_config: str = typer.Option("", "-a", "--advanced-config", help="Advanced nginx config"),
    custom_locations: str = typer.Option("", "-l", "--locations", help="Custom locations (JSON)")
):
    """Create a new proxy host"""
    client = get_client()
    
    # Check for wildcard
    if domain.startswith("*."):
        console.print("[red]❌ Wildcard domains are not allowed for host creation[/red]")
        console.print("[yellow]Wildcards are only supported for SSL certificates[/yellow]")
        raise typer.Exit(1)
    
    defaults = ProxyHostDefaults(
        forward_scheme=forward_scheme,
        block_exploits=block_exploits,
        caching_enabled=caching,
        allow_websocket_upgrade=websocket,
        http2_support=http2,
        advanced_config=advanced_config,
        custom_locations=json.loads(custom_locations) if custom_locations else []
    )
    
    console.print(f"\n[cyan]🌍 Creating proxy host: [green]{domain}[/green][/cyan]")
    
    try:
        result = client.create_host(domain, forward_host, forward_port, defaults)
        proxy_id = result.get("id")
        console.print(f"[green]✅ Proxy host created successfully![/green]")
        console.print(f"[cyan]   ID: {proxy_id}[/cyan]")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to create host: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


@host_app.command("delete")
def host_delete(
    host_id: int = typer.Argument(..., help="Host ID to delete"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation")
):
    """Delete a proxy host"""
    client = get_client()
    
    # Get host info first
    try:
        host = client.get_host(host_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Host ID {host_id} not found[/red]")
        raise typer.Exit(1)
    
    domain = display_domains(host.get("domain_names", ["unknown"]))
    
    if not yes:
        console.print(f"\n[yellow]⚠️ About to delete:[/yellow]")
        console.print(f"   ID: {host_id}")
        console.print(f"   Domain: {domain}")
        
        if not typer.confirm("Are you sure?", err=True):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    if client.delete_host(host_id):
        console.print(f"[green]✅ Host {domain} (ID: {host_id}) deleted successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to delete host[/red]")
        raise typer.Exit(1)


@host_app.command("enable")
def host_enable(host_id: int = typer.Argument(..., help="Host ID to enable")):
    """Enable a proxy host"""
    client = get_client()
    
    if client.enable_host(host_id):
        console.print(f"[green]✅ Host {host_id} enabled successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to enable host[/red]")
        raise typer.Exit(1)


@host_app.command("disable")
def host_disable(host_id: int = typer.Argument(..., help="Host ID to disable")):
    """Disable a proxy host"""
    client = get_client()
    
    if client.disable_host(host_id):
        console.print(f"[green]✅ Host {host_id} disabled successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to disable host[/red]")
        raise typer.Exit(1)


@host_app.command("update")
def host_update(
    host_id: int = typer.Argument(..., help="Host ID to update"),
    field_value: str = typer.Argument(..., help="Field=value to update (e.g., forward_host=192.168.1.1)")
):
    """Update a specific field of a proxy host"""
    client = get_client()
    
    if "=" not in field_value:
        console.print("[red]❌ Invalid format. Use: field=value[/red]")
        raise typer.Exit(1)
    
    field_name, raw_value = field_value.split("=", 1)
    field_name = field_name.strip()

    try:
        value = coerce_field_value(field_name, raw_value)
    except ValueError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        raise typer.Exit(1)

    require_nonempty_domain_names(field_name, value)

    try:
        result = client.update_host(host_id, {field_name: value})
        console.print(f"[green]✅ Host {host_id} updated successfully![/green]")
        console.print(f"   {field_name} = {result.get(field_name)}")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to update host: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


def _require_selection(selected: List[Dict], option: str, given: str) -> List[Dict]:
    """Hand back a non-empty selection, or refuse with a non-zero exit.

    Every caller of select_hosts treats an empty list as a quiet no-op, prints
    "No hosts selected for processing" and returns 0. That made three
    different mistakes indistinguishable from a clean run in a cron log:
    `--ids ,` (a shell join over an empty array), `--ids 99` for a host that
    has been deleted, and a `--pattern` whose base domain was misspelled. A
    selector that was given and found nothing is an error.

    Distinct from the refusal at the bottom of select_hosts, which is the
    different fault of giving no selector at all — that one tells the operator
    which options exist, this one tells them which of their arguments came up
    empty.
    """
    if selected:
        return selected

    # escaped: --pattern is handed to us verbatim, and '[/]' is both a
    # plausible typo and a Rich closing tag that would raise MarkupError here.
    console.print(f"[red]❌ {option} {escape(repr(given))} matched no hosts[/red]")
    console.print(f"[red]   Nothing was changed. A selector that selects nothing is "
                  f"reported as a failure rather than a silent success, so a scripted "
                  f"run cannot read it as a clean pass.[/red]")
    raise typer.Exit(1)


def select_hosts(client: NPMClient, host_ids: Optional[str], pattern: Optional[str],
                 interactive: bool, *, detail_field: Optional[str] = None,
                 default_filter: Optional[Callable[[Dict], bool]] = None,
                 default_label: str = "the domain argument",
                 default_given: str = "") -> List[Dict]:
    """Resolve --ids / --pattern / --interactive into a list of hosts.

    Refuses to act when no filter is given. bulk-add-domain and
    bulk-remove-domain previously fell through to every host, so a bare
    `bulk-remove-domain com` would have rewritten the entire estate.

    Also refuses when a filter *was* given and matched nothing — see
    _require_selection. The one empty list still returned is the estate that
    has no hosts at all, which is not the operator getting their selector
    wrong.

    `default_filter` lets a command derive its own selection when the operator
    gave none, for the case where the command's own argument already says
    which hosts are meant — bulk-replace-domain's old base domain names them
    exactly, so demanding it a second time as --pattern was pure ceremony. It
    is only safe where that argument cannot be a substring of the whole
    estate, so the predicate must match on whole labels; passing one that does
    not reopens the bare-`com` hole this function exists to close.
    """
    all_hosts = client.list_hosts()
    if not all_hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return []

    if host_ids:
        try:
            wanted = {int(x.strip()) for x in host_ids.split(",") if x.strip()}
        except ValueError:
            console.print(f"[red]❌ --ids must be comma-separated numbers, "
                          f"got '{host_ids}'[/red]")
            raise typer.Exit(1)

        selected = [h for h in all_hosts if h.get("id") in wanted]
        missing = wanted - {h.get("id") for h in selected}
        if missing:
            console.print(f"[yellow]⚠️  No such host(s): "
                          f"{', '.join(str(m) for m in sorted(missing))}[/yellow]")
        return _require_selection(selected, "--ids", host_ids)

    if pattern:
        # Accepts a glob or a plain substring, so '*.example.com' and 'example.com'
        # both work and the option means the same thing across every command
        needle = pattern.lower()
        matched = [
            h for h in all_hosts
            if any(fnmatch(d.lower(), needle) or needle in d.lower()
                   for d in h.get("domain_names", []))
        ]
        return _require_selection(matched, "--pattern", pattern)

    if interactive:
        console.print("\n[cyan]Select hosts:[/cyan]\n")
        for idx, host in enumerate(all_hosts):
            domains = display_domains(host.get("domain_names", []))
            extra = (f" ({detail_field}={host.get(detail_field, 'N/A')})"
                     if detail_field else "")
            console.print(f"  [{idx + 1}] [yellow]ID {host.get('id')}[/yellow]: "
                          f"[green]{domains}[/green]{extra}")

        console.print("\n[cyan]Enter host numbers (comma-separated) or 'all':[/cyan]")
        selection = typer.prompt("Selection", err=True)

        if selection.strip().lower() == "all":
            return all_hosts
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",")]
        except ValueError:
            console.print("[red]❌ Invalid selection[/red]")
            raise typer.Exit(1)
        chosen = [all_hosts[i] for i in indices if 0 <= i < len(all_hosts)]
        return _require_selection(chosen, "--interactive", selection.strip())

    if default_filter is not None:
        derived = [h for h in all_hosts if default_filter(h)]
        return _require_selection(derived, default_label, default_given)

    console.print("[red]❌ Please specify --ids, --pattern, or --interactive[/red]")
    raise typer.Exit(1)


def confirm_bulk(yes: bool, prompt: str = "Apply these changes?"):
    """Gate a bulk write behind a confirmation unless -y was given.

    err=True here and at every other prompt: click writes prompts to stdout by
    default, which would put "Apply these changes? [y/N]:" in the middle of a
    pipe. stderr still reaches the terminal the user is answering on.

    click still emits one space on stdout per prompt — its readline backspace
    workaround, which err= does not cover. Harmless: no --json command prompts,
    so the "stdout is JSON or empty" guarantee is unaffected.
    """
    if yes:
        return
    if not typer.confirm(prompt, err=True):
        console.print("[red]❌ Cancelled[/red]")
        raise typer.Exit(0)


def domain_prefix(domain: str) -> Optional[str]:
    """Everything ahead of the registrable base of a domain name.

    "ex.example.com" -> "ex", "sub.ex.example.com" -> "sub.ex",
    "example.com" -> None, since an apex name has no subdomain to carry over
    onto a different base.

    The base is assumed to be two labels, so a multi-part suffix such as
    .co.uk keeps one label too many. Doing better needs public-suffix data
    that neither this tool nor NPM has.
    """
    parts = domain.strip().strip(".").split(".")
    if len(parts) <= 2:
        return None
    return ".".join(parts[:-2])


def domain_base(domain: str) -> Optional[str]:
    """The registrable base of a domain name — its last two labels.

    "ex.example.com" -> "example.com", "example.com" -> "example.com",
    "localhost" -> None.

    The complement of domain_prefix, and assumes the same two-label base, so a
    multi-part suffix such as .co.uk reads as "co.uk". Enough to notice that a
    host answers to two unrelated names; not a public-suffix list.
    """
    parts = [p for p in domain.strip().strip(".").split(".") if p]
    if len(parts) < 2:
        return None
    return ".".join(parts[-2:])


def domain_labels(domain: str) -> List[str]:
    """A domain split into its labels, ignoring case-free surrounding noise."""
    return [p for p in domain.strip().strip(".").split(".") if p]


def domain_is_under(domain: str, base: str) -> bool:
    """True when `domain` is `base` itself or a name beneath it.

    Whole labels, never characters: "example.com" covers "nas.example.com"
    and "example.com", and does not cover "myexample.com".
    """
    labels = [p.lower() for p in domain_labels(domain)]
    base_labels = [p.lower() for p in domain_labels(base)]
    if len(base_labels) < 2 or len(labels) < len(base_labels):
        return False
    return labels[-len(base_labels):] == base_labels


def replace_domain_base(domain: str, old_base: str, new_base: str) -> Optional[str]:
    """`domain` moved from `old_base` onto `new_base`, or None if unaffected.

    The rule this replaced was `old in domain` followed by str.replace, which
    matched characters rather than names. Two things fell out of that: renaming
    "example.com" also rewrote "myexample.com" into "myexample.net", and a
    one-label argument like "com" matched most of the estate — which is why
    the command used to insist on a host selector, a speed bump that stopped
    neither fault since passing '-p com' reproduced both exactly.

    The subdomain keeps the spelling NPM holds for it; only the base is
    rewritten, and it lands spelled the way the operator typed it. So
    "Shop.Example.COM" onto "example.net" gives "Shop.example.net", not a
    silently lowercased name the operator never asked for.
    """
    if not domain_is_under(domain, old_base):
        return None
    labels = domain_labels(domain)
    depth = len(domain_labels(old_base))
    prefix = labels[:-depth]
    tail = ".".join(domain_labels(new_base))
    return ".".join(prefix + [tail]) if prefix else tail


def warn_on_mixed_bases(domains: List[str], subject: str) -> List[str]:
    """Warn when one host would answer to more than one base domain.

    Returns the bases found. One NPM host renders one nginx `server` block with
    one `ssl_certificate`, so every name on it must be covered by that single
    certificate. Two unrelated bases means either a multi-SAN certificate or a
    name that will fail TLS — and the second is the exact fault `host split`
    exists to undo.

    Deliberately checked against the names themselves rather than against the
    certificate's metadata. NPM keeps domain_names on a certificate as
    free-form metadata it never consults when serving, so for uploaded
    certificates it is routinely unusable and cert_covers_domain returns "cannot
    tell" — which is precisely the case where this warning is worth the most.
    """
    # Folded here rather than inside domain_base, which stays a plain "last two
    # labels" like domain_prefix. DNS is case-insensitive and NPM stores
    # whatever was typed, so one base routinely arrives spelled several ways —
    # dedupe_domains keeps the first spelling it sees, not a normalised one.
    # Comparing raw made "App.Example.com" and "api.example.com" two "unrelated
    # base domains", a false alarm on a warning whose whole value is that it
    # fires rarely. Every other domain comparison in this tool folds case too.
    bases = sorted({b for b in (domain_base(d.lower()) for d in domains) if b})
    if len(bases) < 2:
        return bases

    console.print(f"\n[yellow]⚠️  {subject} will answer to {len(bases)} unrelated base "
                  f"domains: {', '.join(bases)}[/yellow]")
    console.print(f"[yellow]   One NPM host is one nginx server block with one "
                  f"certificate, so a single certificate has to cover every one of "
                  f"them. Verify that it does — a host answering to a name its "
                  f"certificate omits is the dual-domain fault `host split` "
                  f"undoes.[/yellow]")
    return bases


# Set once the internationalised-domain warning has been printed. The condition
# is a property of the names in play rather than of one command, so a bulk run
# over thirty hosts would otherwise repeat the same paragraph thirty times and
# bury the per-host output it is meant to annotate.
_idn_warning_shown = False


def warn_on_idn_domains(domains: List[str], subject: str) -> List[str]:
    """Warn once per run when an internationalised domain name is in play.

    Returns the domains that triggered it. A name may contain non-ASCII
    characters, münchen.example.com; DNS carries only ASCII, so the same name
    also has a punycode spelling, xn--mnchen-3ya.example.com. They are one
    name. This tool compares domains as plain strings — dedupe_domains, the
    conflict checks, every selector — so it reads the two spellings as
    unrelated, and the "two hosts must not hold the same domain" guard misses
    a pair holding one form each.

    Warned rather than normalised, deliberately. Python's built-in idna codec
    implements IDNA 2003 and raises UnicodeError on input NPM stores happily —
    an underscore in a label, a label past 63 characters — so wiring it into
    the comparison paths would trade a rare wrong answer for a common crash on
    names that work today. A third-party idna library would handle it, and is
    not worth a dependency for a case this rare.
    """
    global _idn_warning_shown

    # Checked per label: the punycode form of a subdomain is
    # app.xn--mnchen-3ya.com, whose first label is perfectly ASCII.
    flagged = [d for d in domains
               if not str(d).isascii()
               or any(label.startswith("xn--")
                      for label in str(d).lower().split("."))]
    if not flagged or _idn_warning_shown:
        return flagged

    _idn_warning_shown = True
    console.print(f"\n[yellow]⚠️  {subject} involves internationalised domain name(s): "
                  f"{display_domains(flagged)}[/yellow]")
    console.print(f"[yellow]   A name written in non-ASCII characters and the same name "
                  f"written in punycode (xn--…) are one name to DNS, but this tool "
                  f"compares domains as plain text and will not notice the same name "
                  f"spelled both ways. Check by hand that no two hosts hold one "
                  f"spelling each.[/yellow]")
    return flagged


def dedupe_domains(domains: List[str]) -> List[str]:
    """Drop repeats case-insensitively, keeping first occurrence and order.

    Rewriting one base domain onto another can collide with a name the host
    already carries; NPM would then hold the same name twice.
    """
    seen = set()
    unique = []
    for domain in domains:
        key = domain.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(domain)
    return unique


def require_domain_argument(value: str, label: str) -> None:
    """Refuse a blank domain argument before it reaches the string surgery.

    `bulk-replace-domain "$OLD" "$NEW"` with OLD unset is one keystroke away
    from every scripted invocation, and "" is not a value these commands can
    do anything sensible with: it is a substring of every name, and
    str.replace("", new) inserts the replacement between every character, so
    app.example.com becomes example.netaexample.netp… on every selected host —
    written, reported as a success, and exited 0. An empty base is as bad the
    other way round, turning bulk-add-domain's f"{prefix}.{base}" into "ex.".

    Blankness only. Whether a non-blank argument is a well-formed domain is a
    separate question these commands deliberately do not ask, since NPM itself
    accepts names this tool has no business second-guessing.
    """
    if not value or not value.strip():
        console.print(f"[red]❌ {label} is blank — refusing to rewrite domain "
                      f"names from an empty value[/red]")
        raise typer.Exit(1)


def require_nonempty_domain_names(field: str, value: Any) -> None:
    """Refuse a write that would leave a host answering to nothing.

    coerce_field_value folds "", " ", ",", " , " and "[]" alike into [], so
    every one of them reaches update_host as a domain_names of zero names —
    an nginx server block with no server_name, which NPM keeps and nothing can
    reach. split skips a host it would empty and bulk-remove-domain skips one
    too; update and bulk-update write the field straight through and were the
    hole in that rule.

    Refused rather than skipped, unlike those two: they are removing names and
    can sensibly leave a host alone, whereas naming domain_names explicitly and
    giving it nothing has no reading under which the write was wanted.
    """
    if field != "domain_names":
        return
    if value is None or (isinstance(value, list) and not value):
        console.print("[red]❌ domain_names is empty — refusing to leave a host "
                      "with no domain names at all[/red]")
        raise typer.Exit(1)


def host_changed_since(client: NPMClient, host: Dict) -> Optional[str]:
    """A description of how the host differs from this copy, or None.

    Every bulk command reads its hosts, prints a preview, waits for a human,
    and only then writes. That wait is the one interval in the program measured
    in minutes rather than milliseconds, and the writes are full-field
    overwrites — so a domain added in the NPM UI while the prompt is on screen
    is written straight back out again unless somebody looks. This is that look,
    called immediately before each destructive write.

    Both domain_names and modified_on are compared, not just the timestamp:
    NPM's modified_on has second resolution, so two edits inside one second are
    indistinguishable, and domain_names is the field these commands overwrite.
    Domains are compared order-insensitively, since NPM returns them in
    whatever order it stored them and a reorder changes nothing about what the
    host serves — but sorted rather than as sets, so a name appearing twice
    still reads as a change.

    Re-reads through the client rather than trusting the caller's dict: the
    real get_host parses fresh JSON on every call, so the caller holds an
    independent copy and only a genuine round trip can see what NPM holds now.
    """
    host_id = host.get("id")
    try:
        current = client.get_host(host_id)
    except requests.HTTPError:
        return "it no longer exists"

    before = [str(d) for d in host.get("domain_names") or []]
    after = [str(d) for d in current.get("domain_names") or []]
    if sorted(before) != sorted(after):
        return (f"its domains are now {', '.join(after) or '(none)'}, not "
                f"{', '.join(before) or '(none)'}")

    was, now = host.get("modified_on"), current.get("modified_on")
    if was != now:
        return f"it was modified at {now}, not {was}"
    return None


def apply_domain_changes(client: NPMClient, changes: List[Dict],
                         describe) -> None:
    """Write each change's resulting_domains back, then summarise.

    Shared by the bulk domain commands, which previously each carried their
    own copy of this loop and a summary that exited 0 even after failures.
    """
    success_count = 0
    error_count = 0

    with console.status("[bold green]Applying changes...") as status:
        for change in changes:
            host_id = change["host_id"]

            # The change was computed from a read taken before the
            # confirmation prompt, and resulting_domains is written as a
            # full replacement, so anything added to the host meanwhile
            # would be silently dropped. Skip rather than overwrite.
            changed = host_changed_since(client, change["host"])
            if changed:
                console.print(f"  [yellow]⚠️  Host {host_id}: skipped — {changed} "
                              f"since this change was worked out[/yellow]")
                error_count += 1
                continue

            try:
                status.update(f"[bold green]Updating host {host_id}...")
                client.update_host(host_id, {"domain_names": change["resulting_domains"]})
                console.print(f"  [green]✅ Host {host_id}: {describe(change)}[/green]")
                success_count += 1
            except requests.HTTPError as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {format_http_error(e)}[/red]")
                error_count += 1

    print_bulk_summary(success_count, error_count)


def print_bulk_summary(success: int, errors: int, skipped: int = 0):
    """Print the shared bulk summary, exiting non-zero when anything failed"""
    console.print(f"\n[cyan]📊 Summary:[/cyan]")
    console.print(f"   [green]✅ Successful: {success}[/green]")
    if skipped:
        console.print(f"   [yellow]⚠️  Skipped: {skipped}[/yellow]")
    if errors:
        console.print(f"   [red]❌ Failed: {errors}[/red]")
        raise typer.Exit(1)


def validate_certificate_assignment(client: NPMClient, cert_id: Optional[int],
                                    hosts: List[Dict]) -> bool:
    """Report on a certificate about to be assigned to `hosts`.

    Returns False when the certificate does not exist. NPM wraps the whole
    `listen 443 ssl` block in a conditional on the linked certificate, so
    pointing a host at a deleted ID silently drops it to HTTP-only rather than
    failing loudly.
    """
    if cert_id is None:
        return True

    try:
        cert = client.get_certificate(cert_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Certificate {cert_id} does not exist — assigning it would "
                      f"leave these hosts with no TLS listener at all[/red]")
        return False

    # `or []` rather than a get() default: NPM sends domain_names as an
    # explicit null on some certificates, and a default only applies when the
    # key is absent. Joining None raised here, inside the guard meant to stop a
    # host being pointed at a certificate that cannot serve it.
    recorded = ", ".join(cert.get("domain_names") or []) or "empty"
    # "expiry:" because the label only ever describes the expiry date. Printed
    # bare, a green "✅ VALID" sits directly above the coverage warnings and
    # reads as an endorsement of the whole assignment.
    console.print(f"\n[cyan]🔒 Certificate {cert_id}[/cyan] ({recorded}) "
                  f"— expiry: {cert_status_label(cert)}")

    unknown = False
    for host in hosts:
        uncovered = []
        for domain in host.get("domain_names", []):
            covered = cert_covers_domain(cert, domain)
            if covered is False:
                uncovered.append(domain)
            elif covered is None:
                unknown = True
        if uncovered:
            console.print(f"   [yellow]⚠️  Host {host.get('id')}: not covered — "
                          f"{', '.join(uncovered)}[/yellow]")

    if unknown:
        console.print(f"   [dim]note: certificate {cert_id} records no usable domain list "
                      f"({recorded}); coverage not verified[/dim]")
    return True


def _parse_cert_option(value: str) -> Optional[int]:
    """Parse a --cert value into a certificate ID, or None for no certificate"""
    if value.strip().lower() in ("none", "null", "0", ""):
        return None
    try:
        return int(value)
    except ValueError:
        console.print(f"[red]❌ --cert must be a certificate ID or 'none', got '{value}'[/red]")
        raise typer.Exit(1)


def _domain_conflicts(client: NPMClient, domains: List[str],
                      ignore_host_id: Optional[int] = None) -> Dict[str, int]:
    """Map each requested domain to the ID of a host already claiming it.

    NPM rejects duplicate domains, and were one to slip through nginx would
    silently serve whichever server block it saw first.
    """
    wanted = {d.strip().lower(): d for d in domains}
    taken: Dict[str, int] = {}

    for host in client.list_hosts():
        if host.get("id") == ignore_host_id:
            continue
        for existing in host.get("domain_names", []):
            key = str(existing).strip().lower()
            if key in wanted:
                taken[wanted[key]] = host.get("id")
    return taken


@host_app.command("clone")
def host_clone(
    host_id: int = typer.Argument(..., help="Host ID to copy"),
    domains: List[str] = typer.Option(..., "--domain",
                                      help="Domain for the new host (repeatable)"),
    cert: Optional[str] = typer.Option(None, "--cert",
                                       help="Certificate ID, or 'none'. Defaults to the source's"),
    forward_host: Optional[str] = typer.Option(None, "--forward-host",
                                               help="Override the forward host"),
    forward_port: Optional[int] = typer.Option(None, "--forward-port",
                                               help="Override the forward port"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
):
    """Copy a proxy host to new domains, leaving the source untouched

    Every other setting is copied verbatim, including websockets, force SSL,
    HSTS, custom locations and advanced config.
    """
    client = get_client()

    try:
        source = client.get_host(host_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Host ID {host_id} not found[/red]")
        raise typer.Exit(1)

    overrides: Dict[str, Any] = {"domain_names": list(domains)}

    if cert is None:
        cert_id = source.get("certificate_id") or None
        cert_note = f"{cert_id or 'none'} (inherited)"
    else:
        cert_id = _parse_cert_option(cert)
        overrides["certificate_id"] = cert_id
        cert_note = str(cert_id or "none")

    if forward_host:
        overrides["forward_host"] = forward_host
    if forward_port:
        overrides["forward_port"] = forward_port

    wildcards = [d for d in domains if "*" in d]
    if wildcards:
        console.print(f"[red]❌ Wildcard domains are not supported for proxy hosts: "
                      f"{', '.join(wildcards)}[/red]")
        raise typer.Exit(1)

    conflicts = _domain_conflicts(client, domains)
    if conflicts:
        for domain, other in conflicts.items():
            console.print(f"[red]❌ {domain} is already on host {other}[/red]")
        raise typer.Exit(1)

    target = host_config_payload(source, overrides)

    if preview:
        console.print(f"\n[cyan]📋 Clone Preview[/cyan]")
        console.print(f"[cyan]   Source: [yellow]host {host_id}[/yellow] "
                      f"({display_domains(source.get('domain_names', []))})[/cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Setting", style="white")
        table.add_column("New host", style="green")
        table.add_row("Domain(s)", display_domains(domains))
        table.add_row("Forwards to", f"{target.get('forward_scheme')}://"
                                     f"{target.get('forward_host')}:{target.get('forward_port')}")
        table.add_row("Certificate", cert_note)
        table.add_row("Force SSL", str(target.get("ssl_forced")))
        table.add_row("Websockets", str(target.get("allow_websocket_upgrade")))
        table.add_row("Custom locations", str(len(target.get("locations") or [])))
        table.add_row("Advanced config", "yes" if target.get("advanced_config") else "no")
        console.print(table)

    if not validate_certificate_assignment(
            client, cert_id, [{"id": "new", "domain_names": domains}]):
        console.print("[red]   Refusing to clone: the new host would be rendered with no "
                      "TLS listener. Name an existing certificate with --cert, or "
                      "--cert none to create it HTTP-only.[/red]")
        raise typer.Exit(1)

    if cert_id:
        warn_on_mixed_bases(domains, "The new host")

    # Outside the cert_id guard above, unlike the mixed-base warning: that one
    # is about a certificate having to cover every name, this one is about the
    # conflict check just above having compared these names as plain text, and
    # an HTTP-only host can collide with another host just as easily.
    warn_on_idn_domains(domains, "The new host")

    confirm_bulk(yes, "Create this host?")

    try:
        new_host = client.create_host_from(source, overrides)
    except requests.HTTPError as exc:
        console.print(f"[red]❌ Create failed: {format_http_error(exc)}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]✅ Created host {new_host.get('id')}[/green]")


@host_app.command("split")
def host_split(
    match: str = typer.Argument(..., help="Glob selecting domains to move out, e.g. '*.internal.lan'"),
    cert: str = typer.Option(..., "--cert",
                             help="Certificate ID for the new hosts, or 'none'"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to split"),
    pattern: str = typer.Option(None, "--pattern", "-p",
                                help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I",
                                     help="Interactively select hosts"),
):
    """
    Move matching domains out of hosts into new hosts of their own.

    The source keeps its unmatched domains and its existing certificate; only
    the moved domains land on the new host. Hosts with fewer than two domains,
    or where the pattern matches all or none of them, are skipped so a whole
    batch can be selected at once.

    Examples:
        host split '*.internal.lan' --cert 3 --ids 1,2,3
        host split '*.internal.lan' --cert 3 --pattern internal.lan
    """
    client = get_client()
    cert_id = _parse_cert_option(cert)

    hosts = select_hosts(client, host_ids, pattern, interactive)
    if not hosts:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return

    # Worked out up front so the preview and the apply pass agree, and so the
    # conflict check reads the host list once rather than once per host
    existing_domains: Dict[str, int] = {}
    for host in client.list_hosts():
        for domain in host.get("domain_names", []):
            existing_domains[str(domain).strip().lower()] = host.get("id")

    plans: List[Dict] = []
    skipped = 0

    for source in hosts:
        host_id = source.get("id")
        domains = [str(d) for d in source.get("domain_names", [])]
        label = display_domains(domains, "(no domains)")

        if len(domains) < 2:
            console.print(f"[yellow]⚠️  Host {host_id} ({label}): needs at least two "
                          f"domains to split — skipped[/yellow]")
            skipped += 1
            continue

        moving = [d for d in domains if fnmatch(d.lower(), match.lower())]
        staying = [d for d in domains if d not in moving]

        if not moving:
            console.print(f"[yellow]⚠️  Host {host_id} ({label}): nothing matches "
                          f"'{match}' — skipped[/yellow]")
            skipped += 1
            continue

        if not staying:
            console.print(f"[yellow]⚠️  Host {host_id} ({label}): '{match}' matches every "
                          f"domain and would leave the source empty — skipped[/yellow]")
            skipped += 1
            continue

        clashes = {d: existing_domains[d.lower()] for d in moving
                   if existing_domains.get(d.lower(), host_id) != host_id}
        if clashes:
            for domain, other in clashes.items():
                console.print(f"[red]❌ Host {host_id}: {domain} is already on "
                              f"host {other} — skipped[/red]")
            skipped += 1
            continue

        plans.append({"source": source, "id": host_id, "all": domains,
                      "moving": moving, "staying": staying,
                      # The source as it was when the plan was worked out, for
                      # host_changed_since to compare a fresh read against
                      # after the confirmation prompt.
                      "before": dict(source, domain_names=domains)})

    if not plans:
        console.print("[yellow]Nothing to split[/yellow]")
        return

    if preview:
        console.print(f"\n[cyan]📋 Host Split Preview[/cyan]")
        console.print(f"[cyan]   Moving domains matching [yellow]{match}[/yellow] onto new "
                      f"hosts with certificate [yellow]{cert_id or 'none'}[/yellow][/cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host ID", style="yellow", justify="right")
        table.add_column("Stays on source", style="white")
        table.add_column("Moves to new host", style="green")
        table.add_column("Source cert (kept)", style="cyan", justify="right")

        for plan in plans:
            table.add_row(
                str(plan["id"]),
                display_domains(plan["staying"]),
                display_domains(plan["moving"]),
                str(plan["source"].get("certificate_id") or "none"),
            )

        console.print(table)
        console.print(f"\n[cyan]Total hosts to split: [yellow]{len(plans)}[/yellow][/cyan]")

    if not validate_certificate_assignment(
            client, cert_id,
            [{"id": p["id"], "domain_names": p["moving"]} for p in plans]):
        console.print("[red]   Refusing to split: the new hosts would be rendered with no "
                      "TLS listener. Name an existing certificate with --cert, or "
                      "--cert none to create them HTTP-only.[/red]")
        raise typer.Exit(1)

    # A split only fixes the half that moves. Warn when the half left behind
    # keeps a certificate that no longer exists, since NPM renders those hosts
    # with no TLS listener at all rather than reporting an error.
    dangling: Dict[int, List[str]] = {}
    for source_cert in {p["source"].get("certificate_id") for p in plans}:
        if not source_cert:
            continue
        try:
            client.get_certificate(source_cert)
        except requests.HTTPError:
            dangling[source_cert] = [str(p["id"]) for p in plans
                                     if p["source"].get("certificate_id") == source_cert]

    for source_cert, affected in sorted(dangling.items()):
        console.print(f"\n[yellow]⚠️  Host(s) {', '.join(affected)} keep certificate "
                      f"{source_cert}, which no longer exists — their remaining domains "
                      f"stay HTTP-only until repointed:[/yellow]")
        console.print(f"     [dim]host bulk-update certificate_id <cert> "
                      f"--ids {','.join(affected)}[/dim]")

    confirm_bulk(yes)

    # One snapshot for the whole run, before the first trim rather than one per
    # host: split frees the moving domains off a source before the new host
    # exists to take them, so a process killed in between leaves the original
    # domain list nowhere but the operator's scrollback. Whole source records,
    # not just their names, so every one of them can be rebuilt from the file.
    try:
        snapshot = write_state_snapshot(client.config, "pre_split",
                                        {"sources": [p["source"] for p in plans]})
    except OSError as exc:
        console.print(f"[red]❌ Could not write the pre-split snapshot: {exc}[/red]")
        console.print("[red]   Refusing to trim hosts whose configuration was not "
                      "recorded first[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]Pre-split snapshot: {snapshot}[/dim]")

    success_count = 0
    error_count = 0

    with console.status("[bold green]Splitting hosts...") as status:
        for plan in plans:
            host_id = plan["id"]
            status.update(f"[bold green]Splitting host {host_id}...")

            # staying and moving were computed before the confirmation prompt,
            # and the trim writes domain_names as a full replacement, so a name
            # added meanwhile is on neither list: it would be wiped off the
            # source and never land on the new host.
            changed = host_changed_since(client, plan["before"])
            if changed:
                console.print(f"  [yellow]⚠️  Host {host_id}: skipped — {changed} "
                              f"since this split was planned[/yellow]")
                error_count += 1
                continue

            # Free the domains before creating the new host so the two never
            # overlap: NPM rejects duplicates, and nginx would otherwise end up
            # with two server blocks answering to the same name.
            try:
                client.update_host(host_id, {"domain_names": plan["staying"]})
            except requests.HTTPError as exc:
                console.print(f"  [red]❌ Host {host_id}: could not trim source — "
                              f"{format_http_error(exc)}[/red]")
                error_count += 1
                continue

            try:
                new_host = client.create_host_from(
                    plan["source"],
                    {"domain_names": plan["moving"], "certificate_id": cert_id})
            except requests.HTTPError as exc:
                console.print(f"  [red]❌ Host {host_id}: create failed — "
                              f"{format_http_error(exc)}[/red]")
                try:
                    client.update_host(host_id, {"domain_names": plan["all"]})
                    console.print(f"     [green]↩ Host {host_id} restored[/green]")
                except requests.HTTPError as restore_exc:
                    # format_http_error matters most here of anywhere: the
                    # rollback has already failed, the domain list has to be
                    # repaired by hand, and why NPM refused the restoring write
                    # is exactly what a bare repr throws away.
                    console.print(f"     [red]‼ ROLLBACK FAILED: "
                                  f"{format_http_error(restore_exc)}[/red]")
                    console.print(f"     [red]‼ Host {host_id} now holds "
                                  f"{plan['staying']}; it originally held "
                                  f"{plan['all']}[/red]")
                error_count += 1
                continue

            console.print(f"  [green]✅ Host {host_id} → new host "
                          f"{new_host.get('id')} ({display_domains(plan['moving'])})[/green]")
            success_count += 1

    print_bulk_summary(success_count, error_count, skipped)


# Where a merged host's traffic ends up. A source pointing somewhere else would
# have its domains silently repointed at the target's backend, so a difference
# here stops the merge rather than merely warning about it.
MERGE_TARGET_FIELDS = (
    ("forward_scheme", "scheme"),
    ("forward_host", "host"),
    ("forward_port", "port"),
)

# Settings that belong to a source and are discarded when its domains move onto
# the target. Each is a real behaviour change for those domains, so the preview
# names them — but none is refused over, because adopting the target's
# configuration is precisely what --into asks for.
MERGE_NOTABLE_FIELDS = (
    ("enabled", "enabled"),
    ("certificate_id", "certificate"),
    ("ssl_forced", "force SSL"),
    ("hsts_enabled", "HSTS"),
    ("http2_support", "HTTP/2"),
    ("allow_websocket_upgrade", "websockets"),
    ("block_exploits", "block exploits"),
    ("caching_enabled", "caching"),
    ("access_list_id", "access list"),
    ("advanced_config", "advanced config"),
    ("locations", "custom locations"),
)


def _comparable(value: Any) -> Any:
    """Fold NPM's mix of 0/1, true/false and null into comparable values.

    The API returns booleans for some flags and integers for others, and uses
    both 0 and null to mean "nothing linked", so a plain != reports differences
    that are not really there.
    """
    if isinstance(value, bool):
        return int(value)
    return 0 if value is None else value


def _forward_label(host: Dict) -> str:
    """Render a host's upstream as scheme://host:port"""
    return (f"{host.get('forward_scheme')}://{host.get('forward_host')}"
            f":{host.get('forward_port')}")


def describe_host_differences(target: Dict, source: Dict) -> List[str]:
    """Name the settings a source's domains lose by moving onto the target"""
    differences = []
    for key, label in MERGE_NOTABLE_FIELDS:
        if key == "locations":
            left, right = len(target.get(key) or []), len(source.get(key) or [])
        elif key == "advanced_config":
            left = str(target.get(key) or "").strip()
            right = str(source.get(key) or "").strip()
        else:
            left, right = _comparable(target.get(key)), _comparable(source.get(key))
        if left != right:
            differences.append(label)
    return differences


def _restore_merge_source(client: NPMClient, source: Dict, snapshot: Path) -> None:
    """Recreate a source host whose domains the target then refused.

    It comes back under a *new* ID: NPM assigns IDs on create and offers no way
    to ask for a particular one. Anything referring to the old ID — a script, a
    note, the snapshot file — needs updating by hand.
    """
    source_id = source.get("id")
    try:
        restored = client.create_host_from(source, {})
    except (requests.HTTPError, NPMError) as exc:
        console.print(f"     [red]‼ ROLLBACK FAILED: {format_http_error(exc)}[/red]")
        console.print(f"     [red]‼ Host {source_id} is gone and its domains "
                      f"({display_domains(source.get('domain_names', []))}) "
                      f"are not being served. Its full configuration is in "
                      f"{snapshot}[/red]")
        return
    console.print(f"     [green]↩ Host {source_id} recreated as host "
                  f"{restored.get('id')} — NPM assigns a new ID on create[/green]")


@host_app.command("merge")
def host_merge(
    into: int = typer.Option(..., "--into",
                             help="Host ID to keep; it supplies every setting"),
    host_ids: str = typer.Option(None, "--ids", "-i",
                                 help="Comma-separated IDs of hosts to merge in and delete"),
    pattern: str = typer.Option(None, "--pattern", "-p",
                                help="Merge in every host matching this domain pattern"),
    cert: Optional[str] = typer.Option(None, "--cert",
                                       help="Certificate for the merged host, or 'none'. "
                                            "Defaults to the --into host's"),
    allow_different_targets: bool = typer.Option(
        False, "--allow-different-targets",
        help="Merge even when a source forwards somewhere else"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I",
                                     help="Interactively select hosts"),
):
    """
    Fold several proxy hosts into one, deleting the sources.

    The --into host is kept whole and supplies every setting; the others
    contribute their domain names and nothing else. This is the inverse of
    split, and it carries the opposite risk: one NPM host is one nginx server
    block with one certificate, so every domain in the result has to be covered
    by that single certificate or it will not serve over HTTPS.

    Examples:
        host merge --into 12 --ids 13,14
        host merge --into 12 --pattern old.example.com
    """
    client = get_client()

    try:
        target = client.get_host(into)
    except requests.HTTPError:
        console.print(f"[red]❌ Host ID {into} not found[/red]")
        raise typer.Exit(1)

    sources = select_hosts(client, host_ids, pattern, interactive)
    if not sources:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return

    # Dropped rather than refused: --pattern will routinely match the target as
    # well, and deleting the host that was meant to survive is the one outcome
    # merge must never produce.
    if any(h.get("id") == into for h in sources):
        console.print(f"[dim]Host {into} is the merge target; not merging it "
                      f"into itself[/dim]")
        sources = [h for h in sources if h.get("id") != into]

    if not sources:
        console.print(f"[yellow]Nothing left to merge into host {into}[/yellow]")
        return

    mismatched = [(s, [label for key, label in MERGE_TARGET_FIELDS
                       if _comparable(target.get(key)) != _comparable(s.get(key))])
                  for s in sources]
    mismatched = [(s, d) for s, d in mismatched if d]

    if mismatched:
        colour = "yellow" if allow_different_targets else "red"
        mark = "⚠️ " if allow_different_targets else "❌"
        for source, differing in mismatched:
            console.print(f"[{colour}]{mark} Host {source.get('id')} forwards to "
                          f"{_forward_label(source)}, host {into} forwards to "
                          f"{_forward_label(target)} — differs on "
                          f"{', '.join(differing)}[/{colour}]")
        if not allow_different_targets:
            console.print("[red]Refusing to merge: those domains would start reaching a "
                          "different backend. Pass --allow-different-targets if that is "
                          "what you want.[/red]")
            raise typer.Exit(1)

    merged_domains = dedupe_domains(
        [str(d) for d in target.get("domain_names", [])]
        + [str(d) for source in sources for d in source.get("domain_names", [])])

    if cert is None:
        cert_id = target.get("certificate_id") or None
        cert_note = f"{cert_id or 'none'} (inherited from host {into})"
    else:
        cert_id = _parse_cert_option(cert)
        cert_note = str(cert_id or "none")

    if preview:
        console.print(f"\n[cyan]📋 Merge Preview[/cyan]")
        console.print(f"[cyan]   Keeping [yellow]host {into}[/yellow] "
                      f"({display_domains(target.get('domain_names', []))}) → "
                      f"{_forward_label(target)}[/cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Merging in", style="yellow", justify="right")
        table.add_column("Domains", style="green")
        table.add_column("Forwards to", style="white")
        table.add_column("Settings discarded", style="magenta")

        for source in sources:
            table.add_row(
                str(source.get("id")),
                display_domains(source.get("domain_names", []), "(none)"),
                _forward_label(source),
                ", ".join(describe_host_differences(target, source)) or "—",
            )
        console.print(table)

        console.print(f"\n[cyan]   Resulting domains on host {into} "
                      f"([yellow]{len(merged_domains)}[/yellow]): "
                      f"{display_domains(merged_domains)}[/cyan]")
        console.print(f"[cyan]   Certificate: [yellow]{cert_note}[/yellow][/cyan]")
        console.print(f"\n[red]   Deleting host(s) "
                      f"{', '.join(str(s.get('id')) for s in sources)} — NPM offers no "
                      f"way to undo that[/red]")

    # Checked, not just reported. Merge is the only command here that deletes a
    # host, so pointing the survivor at a certificate NPM no longer has would
    # take the sources' domains off the air with nothing left to restore them
    # from but the snapshot file.
    if not validate_certificate_assignment(
            client, cert_id, [{"id": into, "domain_names": merged_domains}]):
        console.print(f"[red]   Refusing to merge: host {into} would be left serving "
                      f"{len(merged_domains)} domain(s) with no TLS listener. Name an "
                      f"existing certificate with --cert, or --cert none to merge them "
                      f"HTTP-only.[/red]")
        raise typer.Exit(1)

    if cert_id:
        warn_on_mixed_bases(merged_domains, f"Host {into}")

    # The merged list is exactly what dedupe_domains just folded by lowercased
    # string, so two spellings of one name survive it as two entries.
    warn_on_idn_domains(merged_domains, f"Host {into}")

    if cert_id is None:
        losing_tls = [str(s.get("id")) for s in sources if s.get("certificate_id")]
        if losing_tls:
            console.print(f"[yellow]⚠️  Host(s) {', '.join(losing_tls)} serve HTTPS today; "
                          f"host {into} has no certificate, so their domains become "
                          f"HTTP-only[/yellow]")

    confirm_bulk(yes, f"Merge {len(sources)} host(s) into host {into} and delete them?")

    # Once, here, and never inside the loop below: the loop writes to the
    # target on every iteration, so its modified_on legitimately moves as a
    # result of our own writes. Every source's domains land on this one host,
    # so a target that moved under us fails the whole merge rather than one
    # source of it — the merged list was built from the stale copy.
    changed = host_changed_since(client, target)
    if changed:
        console.print(f"[red]❌ Host {into} changed while the confirmation prompt was "
                      f"up — {changed}[/red]")
        console.print(f"[red]   Refusing to merge: the domain list was worked out "
                      f"before that change and writing it would undo it. Re-run to "
                      f"see the current state.[/red]")
        raise typer.Exit(1)

    try:
        snapshot = write_state_snapshot(client.config, f"pre_merge_{into}",
                                        {"target": target, "sources": sources})
    except OSError as exc:
        console.print(f"[red]❌ Could not write the pre-merge snapshot: {exc}[/red]")
        console.print("[red]   Refusing to delete hosts whose configuration was not "
                      "recorded first[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]Pre-merge snapshot: {snapshot}[/dim]")

    updates: Dict[str, Any] = {"certificate_id": cert_id}
    if cert_id is None:
        # An SSL-forced host with no certificate redirects to an HTTPS listener
        # that NPM never renders — strictly worse than plain HTTP.
        updates["ssl_forced"] = False
        updates["hsts_enabled"] = False

    applied = [str(d) for d in target.get("domain_names", [])]
    success_count = 0
    error_count = 0

    with console.status("[bold green]Merging hosts...") as status:
        for source in sources:
            source_id = source.get("id")
            incoming = [str(d) for d in source.get("domain_names", [])]
            status.update(f"[bold green]Merging host {source_id}...")

            # A source is deleted outright, so a domain added to it while the
            # prompt was up would go with it — and it is not in `incoming`
            # either, so the target never claims it.
            changed = host_changed_since(client, source)
            if changed:
                console.print(f"  [yellow]⚠️  Host {source_id}: skipped — {changed} "
                              f"since this merge was planned[/yellow]")
                error_count += 1
                continue

            # Delete before extending the target, not after: NPM will not let
            # two hosts hold the same domain, so the name has to be free before
            # the target can claim it. One source at a time, so a failure
            # strands a single host rather than all of them.
            try:
                deleted = client.delete_host(source_id)
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Host {source_id}: could not delete — "
                              f"{format_http_error(exc)}[/red]")
                error_count += 1
                continue
            if not deleted:
                console.print(f"  [red]❌ Host {source_id}: NPM refused the delete[/red]")
                error_count += 1
                continue

            candidate = dedupe_domains(applied + incoming)
            try:
                client.update_host(into, dict(updates, domain_names=candidate))
            except (requests.HTTPError, NPMError) as exc:
                console.print(f"  [red]❌ Host {source_id}: deleted, but host {into} would "
                              f"not take its domains — {format_http_error(exc)}[/red]")
                _restore_merge_source(client, source, snapshot)
                error_count += 1
                continue

            applied = candidate
            console.print(f"  [green]✅ Host {source_id} merged into {into} "
                          f"({display_domains(incoming)})[/green]")
            success_count += 1

    if success_count:
        console.print(f"\n[green]Host {into} now serves: {display_domains(applied)}[/green]")
        if cert_id:
            console.print(f"[dim]All of them via certificate {cert_id} alone; any domain "
                          f"it does not cover will fail TLS.[/dim]")
        # Repeated here, not only before the apply. NPM enforces domain
        # uniqueness at the API layer regardless of whether a host is enabled
        # ("<domain> is already in use", HTTP 400), so a source has to be
        # deleted rather than disabled and this file is the only way back.
        console.print(f"[dim]Deleted host(s) are in {snapshot} — recreate one with "
                      f"`host create` if you need it back.[/dim]")

    print_bulk_summary(success_count, error_count)


@host_app.command("cert-assign")
def host_cert_assign(
    cert_id: int = typer.Argument(..., help="Certificate ID to assign"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs"),
    pattern: str = typer.Option(None, "--pattern", "-p",
                                help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I",
                                     help="Interactively select hosts"),
):
    """
    Assign a certificate to many proxy hosts at once.

    The same command as `host ssl-enable`, under the name people reach for
    when they are looking to bulk-update certificates rather than to toggle
    something on. Only certificate_id is written; ssl_forced and http2_support
    are left as they are.

        host cert-assign 14 --pattern example.com
        host cert-assign 14 --ids 3,7,12
    """
    host_ssl_enable(cert_id=cert_id, host_ids=host_ids, pattern=pattern,
                    preview=preview, yes=yes, interactive=interactive)


@host_app.command("ssl-enable")
def host_ssl_enable(
    cert_id: int = typer.Argument(..., help="Certificate ID to assign"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs"),
    pattern: str = typer.Option(None, "--pattern", "-p",
                                help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview",
                                 help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I",
                                     help="Interactively select hosts"),
):
    """
    Assign a certificate to proxy hosts.

    A convenience alias for `host bulk-update certificate_id <id>`, which is
    where the cert validation and coverage warnings actually live. Only the
    certificate changes; ssl_forced and http2_support are left alone.
    """
    host_bulk_update(
        field="certificate_id",
        value=str(cert_id),
        host_ids=host_ids,
        pattern=pattern,
        preview=preview,
        yes=yes,
        interactive=interactive,
    )


@host_app.command("ssl-disable")
def host_ssl_disable(host_id: int = typer.Argument(..., help="Host ID")):
    """Disable SSL for a proxy host"""
    client = get_client()
    
    if client.disable_host_ssl(host_id):
        console.print(f"[green]✅ SSL disabled for host {host_id}[/green]")
    else:
        console.print(f"[red]❌ Failed to disable SSL[/red]")
        raise typer.Exit(1)


@host_app.command("acl-enable")
def host_acl_enable(
    host_id: int = typer.Argument(..., help="Host ID"),
    access_list_id: int = typer.Argument(..., help="Access List ID")
):
    """Enable ACL for a proxy host"""
    client = get_client()
    
    if client.enable_host_acl(host_id, access_list_id):
        console.print(f"[green]✅ ACL enabled for host {host_id} with access list {access_list_id}[/green]")
    else:
        console.print(f"[red]❌ Failed to enable ACL[/red]")
        raise typer.Exit(1)


@host_app.command("acl-disable")
def host_acl_disable(host_id: int = typer.Argument(..., help="Host ID")):
    """Disable ACL for a proxy host"""
    client = get_client()
    
    if client.disable_host_acl(host_id):
        console.print(f"[green]✅ ACL disabled for host {host_id}[/green]")
    else:
        console.print(f"[red]❌ Failed to disable ACL[/red]")
        raise typer.Exit(1)


# =============================================================================
# CLI Commands - Certificates
# =============================================================================

@cert_app.command("list")
def cert_list(
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON instead of a table")
):
    """List all SSL certificates"""
    client = get_client()

    certs = client.list_certificates()

    if as_json:
        print_json(certs)
        return

    if not certs:
        console.print("[yellow]No certificates found[/yellow]")
        return
    
    table = Table(title="SSL Certificates", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="yellow", justify="right")
    table.add_column("Domain(s)", style="green")
    table.add_column("Provider", style="cyan")
    table.add_column("Expires", style="white")
    table.add_column("Status", justify="center")
    
    for cert in certs:
        cert_id = str(cert.get("id", "?"))
        domains = ", ".join(cert.get("domain_names", ["?"]))
        provider = cert.get("provider", "unknown")
        expires = cert.get("expires_on", "N/A")
        status = cert_status_label(cert)

        table.add_row(cert_id, domains, provider, expires, status)
    
    out_console.print(table)


@cert_app.command("show")
def cert_show(
    identifier: str = typer.Argument(..., help="Certificate ID or domain name"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON instead of a summary")
):
    """Show certificate details"""
    client = get_client()

    # Check if it's an ID
    if identifier.isdigit():
        try:
            cert = client.get_certificate(int(identifier))
            certs = [cert]
        except requests.HTTPError:
            console.print(f"[red]❌ Certificate ID {identifier} not found[/red]")
            raise typer.Exit(1)
    else:
        # Search by domain
        certs = [c for c in client.list_certificates() 
                 if any(identifier.lower() in d.lower() for d in c.get("domain_names", []))]
        
        if not certs:
            console.print(f"[yellow]No certificates found for '{identifier}'[/yellow]")
            return

    if as_json:
        print_json(certs[0] if identifier.isdigit() else certs)
        return

    for cert in certs:
        out_console.print(f"\n[cyan]🔒 Certificate ID: {cert.get('id')}[/cyan]")
        out_console.print(f"   Domains: {', '.join(cert.get('domain_names', []))}")
        out_console.print(f"   Provider: {cert.get('provider')}")
        out_console.print(f"   Created: {cert.get('created_on', 'N/A')}")
        out_console.print(f"   Expires: {cert.get('expires_on', 'N/A')}")
        out_console.print(f"   Status: {cert_status_label(cert)}")


@cert_app.command("generate")
def cert_generate(
    domain: str = typer.Argument(..., help="Domain name (use *.domain.com for wildcard)"),
    email: str = typer.Option(None, "--email", "-e", help="Email for Let's Encrypt"),
    dns_provider: str = typer.Option(None, "--dns-provider", help="DNS provider for wildcard certs"),
    dns_credentials: str = typer.Option(None, "--dns-credentials", help="DNS credentials as JSON"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation")
):
    """Generate a Let's Encrypt certificate"""
    client = get_client()
    
    # Use default email if not provided
    if not email:
        email = client.config.api_user
        console.print(f"[yellow]📧 Using default email: {email}[/yellow]")
    
    # Check for wildcard requirements
    is_wildcard = domain.startswith("*.")
    if is_wildcard:
        if not dns_provider or not dns_credentials:
            console.print("[red]❌ Wildcard certificates require --dns-provider and --dns-credentials[/red]")
            console.print("\nExample:")
            console.print('  --dns-provider cloudflare --dns-credentials \'{"dns_cloudflare_email":"...", "dns_cloudflare_api_key":"..."}\'')
            raise typer.Exit(1)
    
    # Check for existing certificate
    existing = client.find_certificate(domain)
    if existing:
        # Only refuse when the existing cert is demonstrably still valid; an
        # unreadable expiry falls through and regenerates rather than blocking
        days = cert_days_remaining(existing)
        if days is not None and days >= 0:
            console.print(f"[yellow]🔔 Valid certificate already exists for {domain}[/yellow]")
            console.print(f"   Certificate ID: {existing.get('id')} — {cert_status_label(existing)}")
            return
        console.print(f"[yellow]♻️  Replacing certificate {existing.get('id')} for {domain} "
                      f"— {cert_status_label(existing)}[/yellow]")
    
    if not yes:
        console.print(f"\n[yellow]📝 Certificate generation parameters:[/yellow]")
        console.print(f"   Domain: {domain}")
        console.print(f"   Email: {email}")
        if dns_provider:
            console.print(f"   DNS Provider: {dns_provider}")
        
        if not typer.confirm("Generate certificate?", err=True):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    console.print(f"\n[cyan]🔐 Generating certificate for {domain}...[/cyan]")
    console.print("[yellow]⏳ This may take a few minutes...[/yellow]")
    
    try:
        creds = json.loads(dns_credentials) if dns_credentials else None
        result = client.generate_certificate(domain, email, dns_provider, creds)
        
        console.print(f"\n[green]✅ Certificate generated successfully![/green]")
        console.print(f"   Certificate ID: {result.get('id')}")
    except ValueError as e:
        console.print(f"[red]❌ {e}[/red]")
        raise typer.Exit(1)
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to generate certificate: {format_http_error(e)}[/red]")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_data = e.response.json()
                console.print(f"   Error: {error_data.get('error', {}).get('message', 'Unknown')}")
            except Exception:
                pass
        raise typer.Exit(1)


@cert_app.command("delete")
def cert_delete(
    identifier: str = typer.Argument(..., help="Certificate ID or domain"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation")
):
    """Delete a certificate"""
    client = get_client()
    
    # Resolve certificate ID
    if identifier.isdigit():
        cert_id = int(identifier)
        try:
            cert = client.get_certificate(cert_id)
        except requests.HTTPError:
            console.print(f"[red]❌ Certificate ID {cert_id} not found[/red]")
            raise typer.Exit(1)
    else:
        # Search by domain
        certs = [c for c in client.list_certificates() 
                 if identifier in c.get("domain_names", [])]
        
        if not certs:
            console.print(f"[red]❌ No certificate found for domain: {identifier}[/red]")
            raise typer.Exit(1)
        
        if len(certs) > 1:
            console.print(f"[yellow]Multiple certificates found for {identifier}:[/yellow]")
            for c in certs:
                console.print(f"  ID: {c['id']} - Domains: {', '.join(c['domain_names'])}")
            console.print("[yellow]Please specify the certificate ID[/yellow]")
            raise typer.Exit(1)
        
        cert = certs[0]
        cert_id = cert["id"]
    
    domains = ", ".join(cert.get("domain_names", []))
    
    if not yes:
        console.print(f"\n[yellow]⚠️ About to delete certificate:[/yellow]")
        console.print(f"   ID: {cert_id}")
        console.print(f"   Domains: {domains}")
        
        if not typer.confirm("Are you sure?", err=True):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    if client.delete_certificate(cert_id):
        console.print(f"[green]✅ Certificate {cert_id} deleted successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to delete certificate[/red]")
        raise typer.Exit(1)


@cert_app.command("download")
def cert_download(
    cert_id: int = typer.Argument(..., help="Certificate ID"),
    output_dir: str = typer.Option("./certificates", "-o", "--output", help="Output directory"),
    name: str = typer.Option(None, "-n", "--name", help="Certificate name (default: certificate_<id>)")
):
    """Download certificate files"""
    client = get_client()
    
    cert_name = name or f"certificate_{cert_id}"
    # Sanitize cert_name to prevent path traversal
    cert_name = re.sub(r'[^a-zA-Z0-9._-]', '_', cert_name)
    
    console.print(f"\n[cyan]🔒 Downloading certificate ID: {cert_id}[/cyan]")
    console.print(f"   Output: {output_dir}")

    try:
        written = client.download_certificate(cert_id, output_dir, cert_name)
    except CertificateDownloadError as e:
        console.print(f"[red]❌ Failed to download certificate: {e}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]✅ Certificate downloaded successfully![/green]")
    for path in written:
        console.print(f"   {path}")
    console.print("[red]🔐 The private key is unencrypted at mode 600. "
                  "Keep it off shared storage and out of version control.[/red]")


# =============================================================================
# CLI Commands - Users
# =============================================================================

@user_app.command("list")
def user_list(as_json: bool = typer.Option(False, "--json", help="Emit raw JSON on stdout")):
    """List all users"""
    client = get_client()

    users = client.list_users()

    if as_json:
        print_json(users)
        return

    if not users:
        console.print("[yellow]No users found[/yellow]")
        return
    
    table = Table(title="Users", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="yellow", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Email", style="cyan")
    table.add_column("Roles", style="white")
    table.add_column("Status", justify="center")
    
    for user in users:
        user_id = str(user.get("id", "?"))
        name = user.get("name", "?")
        email = user.get("email", "?")
        roles = ", ".join(user.get("roles", []))
        disabled = user.get("is_disabled", False)
        status = "[red]disabled[/red]" if disabled else "[green]active[/green]"
        
        table.add_row(user_id, name, email, roles, status)
    
    out_console.print(table)


@user_app.command("create")
def user_create(
    username: str = typer.Argument(..., help="Username"),
    email: str = typer.Argument(..., help="Email address"),
    password: str = typer.Option(None, "--password", prompt=True, hide_input=True,
                                 confirmation_prompt=True,
                                 help="Password (prompted for if omitted, so it stays "
                                      "out of shell history and `ps` output)")
):
    """Create a new user.

    The password is prompted for rather than taken as an argument: a positional
    password lands in shell history and is visible in the process list to every
    other user on the machine for as long as the command runs.
    """
    client = get_client()
    
    # Check if user already exists
    existing = client.list_users()
    if any(u.get("email") == email for u in existing):
        console.print(f"[red]❌ User with email {email} already exists[/red]")
        raise typer.Exit(1)
    
    if any(u.get("name") == username for u in existing):
        console.print(f"[red]❌ User with name {username} already exists[/red]")
        raise typer.Exit(1)
    
    try:
        result = client.create_user(username, email, password)
        console.print(f"[green]✅ User created successfully![/green]")
        console.print(f"   Name: {username}")
        console.print(f"   Email: {email}")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to create user: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


@user_app.command("delete")
def user_delete(
    user_id: int = typer.Argument(..., help="User ID to delete"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation")
):
    """Delete a user"""
    client = get_client()
    
    # Get user info
    users = client.list_users()
    user = next((u for u in users if u.get("id") == user_id), None)
    
    if not user:
        console.print(f"[red]❌ User ID {user_id} not found[/red]")
        raise typer.Exit(1)
    
    if not yes:
        console.print(f"\n[yellow]⚠️ About to delete user:[/yellow]")
        console.print(f"   ID: {user_id}")
        console.print(f"   Name: {user.get('name')}")
        console.print(f"   Email: {user.get('email')}")
        
        if not typer.confirm("Are you sure?", err=True):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    if client.delete_user(user_id):
        console.print(f"[green]✅ User {user.get('name')} deleted successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to delete user[/red]")
        raise typer.Exit(1)


# =============================================================================
# CLI Commands - Access Lists
# =============================================================================

@host_app.command("bulk-add-domain")
def host_bulk_add_domain(
    new_domain: str = typer.Argument(..., help="New base domain to add (e.g., my3rddomain.com)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk add a new domain to existing hosts based on subdomain pattern.
    
    Example: If host has [ex.domain1.com, ex.domain2.com] and you run:
        host bulk-add-domain domain3.com
    It will add ex.domain3.com to that host.

    The subdomain prefix is extracted from existing domains and combined
    with the new base domain. Hosts whose only names are apex domains are
    skipped, since they carry no prefix to reuse.
    """
    require_domain_argument(new_domain, "The new base domain")

    client = get_client()

    hosts_to_process = select_hosts(client, host_ids, pattern, interactive)
    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return

    # Calculate changes
    changes = []
    for host in hosts_to_process:
        host_id = host.get("id")
        current_domains = host.get("domain_names", [])

        # Collect subdomain prefixes, sorted so the output is stable rather
        # than following set iteration order
        prefixes = sorted({p for p in (domain_prefix(d) for d in current_domains) if p})

        existing = {d.lower() for d in current_domains}
        new_domains_to_add = []
        for prefix in prefixes:
            candidate = f"{prefix}.{new_domain}"
            if candidate.lower() not in existing:
                new_domains_to_add.append(candidate)
                existing.add(candidate.lower())

        if new_domains_to_add:
            changes.append({
                "host_id": host_id,
                # Snapshotted, not referenced: host_changed_since compares this
                # against a fresh read after the confirmation prompt, so it has
                # to be the host as it was when the change was worked out.
                "host": dict(host, domain_names=list(current_domains)),
                "current_domains": current_domains,
                "new_domains": new_domains_to_add,
                "resulting_domains": current_domains + new_domains_to_add
            })

    if not changes:
        console.print("[yellow]No changes to make - all domains already exist or no valid prefixes found[/yellow]")
        return
    
    # Preview changes
    if preview:
        console.print(f"\n[cyan]📋 Bulk Domain Addition Preview[/cyan]")
        console.print(f"[cyan]   New base domain: [yellow]{new_domain}[/yellow][/cyan]\n")
        
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host ID", style="yellow", justify="right")
        table.add_column("Current Domains", style="white")
        table.add_column("Domains to Add", style="green")
        
        for change in changes:
            table.add_row(
                str(change["host_id"]),
                "\n".join(display_domain(d) for d in change["current_domains"]),
                "\n".join(display_domain(d) for d in change["new_domains"])
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to add: [yellow]{sum(len(c['new_domains']) for c in changes)}[/yellow][/cyan]\n")

    # Every name the run touches, not just the new ones: the collision this
    # warns about is between a name being written and one already stored.
    warn_on_idn_domains([d for c in changes for d in c["resulting_domains"]],
                        "This bulk-add-domain run")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: f"Added {display_domains(c['new_domains'])}"
    )


@host_app.command("bulk-remove-domain")
def host_bulk_remove_domain(
    domain_pattern: str = typer.Argument(..., help="Domain pattern to remove (e.g., my3rddomain.com or full domain)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk remove domains matching a pattern from existing hosts.

    Example: Remove all domains containing 'my3rddomain.com':
        host bulk-remove-domain my3rddomain.com --pattern my3rddomain.com

    Hosts left with no domains at all are skipped rather than emptied.
    """
    client = get_client()

    hosts_to_process = select_hosts(client, host_ids, pattern, interactive)
    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return

    # Calculate changes
    changes = []
    for host in hosts_to_process:
        host_id = host.get("id")
        current_domains = host.get("domain_names", [])
        
        # Find domains to remove
        domains_to_remove = [d for d in current_domains if domain_pattern.lower() in d.lower()]
        remaining_domains = [d for d in current_domains if domain_pattern.lower() not in d.lower()]
        
        if domains_to_remove and remaining_domains:  # Must have at least one domain remaining
            changes.append({
                "host_id": host_id,
                "host": dict(host, domain_names=list(current_domains)),
                "current_domains": current_domains,
                "domains_to_remove": domains_to_remove,
                "resulting_domains": remaining_domains
            })
        elif domains_to_remove and not remaining_domains:
            console.print(f"[yellow]⚠️ Skipping host {host_id}: Cannot remove all domains[/yellow]")
    
    if not changes:
        console.print("[yellow]No changes to make[/yellow]")
        return
    
    # Preview changes
    if preview:
        console.print(f"\n[cyan]📋 Bulk Domain Removal Preview[/cyan]")
        console.print(f"[cyan]   Pattern to remove: [red]{domain_pattern}[/red][/cyan]\n")
        
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host ID", style="yellow", justify="right")
        table.add_column("Current Domains", style="white")
        table.add_column("Domains to Remove", style="red")
        table.add_column("Remaining", style="green")
        
        for change in changes:
            table.add_row(
                str(change["host_id"]),
                "\n".join(display_domain(d) for d in change["current_domains"]),
                "\n".join(display_domain(d) for d in change["domains_to_remove"]),
                "\n".join(display_domain(d) for d in change["resulting_domains"])
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to remove: [red]{sum(len(c['domains_to_remove']) for c in changes)}[/red][/cyan]\n")

    # The removal itself is a plain substring test against these names, so a
    # pattern typed in one spelling silently spares the other.
    warn_on_idn_domains([d for c in changes for d in c["current_domains"]],
                        "This bulk-remove-domain run")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: f"Removed {display_domains(c['domains_to_remove'])}"
    )


@host_app.command("bulk-replace-domain")
def host_bulk_replace_domain(
    old_domain: str = typer.Argument(..., help="Old base domain to replace (e.g., olddomain.com)"),
    new_domain: str = typer.Argument(..., help="New base domain (e.g., newdomain.com)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="Narrow to hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk replace one base domain with another in existing hosts.

    Example: Replace olddomain.com with newdomain.com everywhere it appears:
        host bulk-replace-domain olddomain.com newdomain.com

    This will change ex.olddomain.com to ex.newdomain.com. A host selector is
    optional: with none given, every host carrying the old base is rebased,
    since that argument already names the hosts that are meant. Pass --ids,
    --pattern or --interactive to narrow it further.

    Matching is by whole label, so renaming 'old.com' leaves 'myold.com'
    alone, and the old base must be at least two labels — 'com' is refused
    rather than quietly matching most of the estate.
    """
    require_domain_argument(old_domain, "The old base domain")
    require_domain_argument(new_domain, "The new base domain")

    if len(domain_labels(old_domain)) < 2:
        console.print(f"[red]❌ The old base domain must be a full domain such "
                      f"as 'example.com', not {escape(repr(old_domain))}[/red]")
        console.print("[red]   A single label would match most of the estate "
                      "at once[/red]")
        raise typer.Exit(1)

    client = get_client()

    hosts_to_process = select_hosts(
        client, host_ids, pattern, interactive,
        default_filter=lambda h: any(domain_is_under(d, old_domain)
                                     for d in h.get("domain_names") or []),
        default_label="the base domain", default_given=old_domain)
    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return

    # Calculate changes
    changes = []
    collisions = []
    for host in hosts_to_process:
        host_id = host.get("id")
        current_domains = host.get("domain_names", [])

        new_domains = []
        replaced = []
        for domain in current_domains:
            rebased = replace_domain_base(domain, old_domain, new_domain)
            # A rebase onto the same base, or one that lands on the spelling
            # already stored, is not a change. Recording it anyway would send
            # NPM a write that alters nothing and report it as work done.
            if rebased is not None and rebased != domain:
                new_domains.append(rebased)
                replaced.append((domain, rebased))
            else:
                new_domains.append(domain)

        if not replaced:
            continue

        # A rewrite can land on a name the host already carries, e.g. a host
        # holding both ex.old.com and ex.new.com. NPM would then store the
        # same name twice.
        deduped = dedupe_domains(new_domains)
        if len(deduped) != len(new_domains):
            collisions.append(host_id)

        changes.append({
            "host_id": host_id,
            "host": dict(host, domain_names=list(current_domains)),
            "current_domains": current_domains,
            "replacements": replaced,
            "resulting_domains": deduped
        })

    if not changes:
        console.print("[yellow]No changes to make[/yellow]")
        return
    
    # Preview changes
    if preview:
        console.print(f"\n[cyan]📋 Bulk Domain Replacement Preview[/cyan]")
        console.print(f"[cyan]   Replace: [red]{old_domain}[/red] → [green]{new_domain}[/green][/cyan]\n")
        
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host ID", style="yellow", justify="right")
        table.add_column("Old Domain", style="red")
        table.add_column("New Domain", style="green")
        
        for change in changes:
            for old, new in change["replacements"]:
                table.add_row(str(change["host_id"]),
                              display_domain(old), display_domain(new))
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to replace: [yellow]{sum(len(c['replacements']) for c in changes)}[/yellow][/cyan]\n")

    if collisions:
        console.print(f"[yellow]⚠️  Host(s) {', '.join(str(h) for h in collisions)} already "
                      f"carry a name the rewrite produces; the duplicate will be "
                      f"dropped rather than stored twice[/yellow]\n")

    warn_on_idn_domains([d for c in changes for d in c["resulting_domains"]],
                        "This bulk-replace-domain run")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: ", ".join(f"{display_domain(old)}→{display_domain(new)}"
                            for old, new in c["replacements"])
    )


@host_app.command("bulk-update")
def host_bulk_update(
    field: str = typer.Argument(..., help="Field to update (e.g., forward_host, forward_port, forward_scheme)"),
    value: str = typer.Argument(..., help="New value for the field"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk update a field across multiple hosts.
    
    Examples:
        host bulk-update forward_host 192.168.1.100 --ids 1,2,3
        host bulk-update forward_port 8080 --pattern mydomain.com
        host bulk-update block_exploits true --interactive
    """
    client = get_client()
    
    try:
        typed_value = coerce_field_value(field, value)
    except ValueError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        raise typer.Exit(1)

    hosts_to_process = select_hosts(client, host_ids, pattern, interactive,
                                    detail_field=field)

    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return
    
    # Preview changes
    if preview:
        console.print(f"\n[cyan]📋 Bulk Update Preview[/cyan]")
        console.print(f"[cyan]   Field: [yellow]{field}[/yellow] → [green]{typed_value}[/green][/cyan]\n")
        
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host ID", style="yellow", justify="right")
        table.add_column("Domain(s)", style="white")
        table.add_column(f"Current {field}", style="red")
        table.add_column(f"New {field}", style="green")
        
        for host in hosts_to_process:
            table.add_row(
                str(host.get("id")),
                display_domains(host.get("domain_names", [])),
                str(host.get(field, "N/A")),
                str(typed_value)
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(hosts_to_process)}[/yellow][/cyan]")

    # Field-aware validation. Pointing a host at a deleted certificate makes
    # NPM render it with no TLS listener at all rather than failing loudly, and
    # an empty domain_names takes every selected host off the air at once.
    require_nonempty_domain_names(field, typed_value)
    if field == "certificate_id":
        if not validate_certificate_assignment(client, typed_value, hosts_to_process):
            raise typer.Exit(1)

    confirm_bulk(yes)

    success_count = 0
    error_count = 0

    with console.status("[bold green]Applying changes...") as status:
        for host in hosts_to_process:
            host_id = host.get("id")

            # The preview the operator approved described this host as it was
            # read before the prompt; writing `field` onto a host that has
            # since moved is a change they never saw.
            changed = host_changed_since(client, host)
            if changed:
                console.print(f"  [yellow]⚠️  Host {host_id}: skipped — {changed} "
                              f"since the preview was taken[/yellow]")
                error_count += 1
                continue

            try:
                status.update(f"[bold green]Updating host {host_id}...")
                client.update_host(host_id, {field: typed_value})
                console.print(f"  [green]✅ Host {host_id}: {field}={typed_value}[/green]")
                success_count += 1
            except requests.HTTPError as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {format_http_error(e)}[/red]")
                error_count += 1

    print_bulk_summary(success_count, error_count)


@acl_app.command("list")
def acl_list(as_json: bool = typer.Option(False, "--json", help="Emit raw JSON on stdout")):
    """List all access lists"""
    client = get_client()

    access_lists = client.list_access_lists()

    if as_json:
        print_json(access_lists)
        return

    if not access_lists:
        console.print("[yellow]No access lists found[/yellow]")
        return
    
    table = Table(title="Access Lists", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="yellow", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Users", justify="center")
    table.add_column("Rules", justify="center")
    table.add_column("Satisfy", style="cyan")
    table.add_column("Proxy Hosts", justify="center")
    
    for al in access_lists:
        al_id = str(al.get("id", "?"))
        name = al.get("name", "?")
        items_count = len(al.get("items", []))
        clients_count = len(al.get("clients", []))
        satisfy = "Any" if al.get("satisfy_any") else "All"
        proxy_count = str(al.get("proxy_host_count", 0))
        
        table.add_row(al_id, name, str(items_count), str(clients_count), satisfy, proxy_count)
    
    out_console.print(table)


@acl_app.command("show")
def acl_show(
    list_id: int = typer.Argument(..., help="Access List ID"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON on stdout")
):
    """Show access list details"""
    client = get_client()

    try:
        al = client.get_access_list(list_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Access list ID {list_id} not found[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(al)
        return

    out_console.print(f"\n[cyan]🔑 Access List Details:[/cyan]")
    out_console.print(f"   ID: {al.get('id')}")
    out_console.print(f"   Name: {al.get('name')}")
    out_console.print(f"   Satisfy: {'Any' if al.get('satisfy_any') else 'All'}")
    out_console.print(f"   Pass Auth: {'Yes' if al.get('pass_auth') else 'No'}")
    
    items = al.get("items", [])
    if items:
        out_console.print(f"\n   [cyan]Authorized Users:[/cyan]")
        for item in items:
            out_console.print(f"      • {item.get('username')}")
    
    clients = al.get("clients", [])
    if clients:
        out_console.print(f"\n   [cyan]IP Rules:[/cyan]")
        for client_item in clients:
            directive = client_item.get("directive", "allow")
            color = "green" if directive == "allow" else "red"
            out_console.print(f"      • [{color}]{directive}[/{color}] {client_item.get('address')}")


@acl_app.command("create")
def acl_create(
    name: str = typer.Argument(..., help="Access list name"),
    satisfy: str = typer.Option("all", "--satisfy", help="Satisfy mode: any/all"),
    pass_auth: bool = typer.Option(False, "--pass-auth", help="Enable pass auth"),
    users: str = typer.Option(None, "--users", help="Comma-separated users"),
    allow: str = typer.Option(None, "--allow", help="Comma-separated allowed IPs"),
    deny: str = typer.Option(None, "--deny", help="Comma-separated denied IPs")
):
    """Create a new access list"""
    client = get_client()
    
    satisfy_any = satisfy.lower() == "any"
    
    items = []
    if users:
        for user in users.split(","):
            user = user.strip()
            password = typer.prompt(f"Password for {user}", hide_input=True, err=True)
            items.append({"username": user, "password": password})
    
    clients = []
    if allow:
        for ip in allow.split(","):
            clients.append({"address": ip.strip(), "directive": "allow"})
    if deny:
        for ip in deny.split(","):
            clients.append({"address": ip.strip(), "directive": "deny"})
    
    try:
        result = client.create_access_list(name, satisfy_any, pass_auth, items, clients)
        console.print(f"[green]✅ Access list created successfully![/green]")
        console.print(f"   ID: {result.get('id')}")
        console.print(f"   Name: {name}")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to create access list: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


@acl_app.command("delete")
def acl_delete(
    list_id: int = typer.Argument(..., help="Access List ID to delete"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation")
):
    """Delete an access list"""
    client = get_client()
    
    # Get access list info
    try:
        al = client.get_access_list(list_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Access list ID {list_id} not found[/red]")
        raise typer.Exit(1)
    
    if not yes:
        console.print(f"\n[yellow]⚠️ About to delete access list:[/yellow]")
        console.print(f"   ID: {list_id}")
        console.print(f"   Name: {al.get('name')}")
        
        if not typer.confirm("Are you sure?", err=True):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    if client.delete_access_list(list_id):
        console.print(f"[green]✅ Access list {al.get('name')} deleted successfully![/green]")
    else:
        console.print(f"[red]❌ Failed to delete access list[/red]")
        raise typer.Exit(1)


@acl_app.command("update")
def acl_update(
    list_id: int = typer.Argument(..., help="Access List ID to update"),
    name: str = typer.Option(None, "--name", help="New name"),
    satisfy: str = typer.Option(None, "--satisfy", help="Satisfy mode: any/all"),
    pass_auth: bool = typer.Option(None, "--pass-auth", help="Enable pass auth"),
    allow: str = typer.Option(None, "--allow", help="Comma-separated allowed IPs (replaces existing)"),
    deny: str = typer.Option(None, "--deny", help="Comma-separated denied IPs (replaces existing)")
):
    """Update an access list"""
    client = get_client()
    
    updates = {}
    
    if name is not None:
        updates["name"] = name
    
    if satisfy is not None:
        updates["satisfy_any"] = satisfy.lower() == "any"
    
    if pass_auth is not None:
        updates["pass_auth"] = pass_auth
    
    if allow is not None or deny is not None:
        clients = []
        if allow:
            for ip in allow.split(","):
                clients.append({"address": ip.strip(), "directive": "allow"})
        if deny:
            for ip in deny.split(","):
                clients.append({"address": ip.strip(), "directive": "deny"})
        updates["clients"] = clients
    
    if not updates:
        console.print("[yellow]No updates specified[/yellow]")
        return
    
    try:
        result = client.update_access_list(list_id, updates)
        console.print(f"[green]✅ Access list {list_id} updated successfully![/green]")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to update access list: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Main entry point.

    Typer re-raises whatever a command lets escape, so an unreachable server or
    a rejected password used to print a full Rich traceback. Those are the
    user's environment, not our bug: report each in one line on stderr and exit
    non-zero. Anything not listed here is a real defect and keeps its traceback.
    """
    try:
        app()
    except NPMError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        sys.exit(1)
    except requests.HTTPError as exc:
        console.print(f"[red]❌ {format_http_error(exc)}[/red]")
        sys.exit(1)
    except requests.RequestException as exc:
        console.print(f"[red]❌ NPM request failed — "
                      f"{describe_connection_error(exc)}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
