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
import zipfile
from fnmatch import fnmatch
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

try:
    import requests
    import typer
    from rich.console import Console
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

# Version
VERSION = "3.0.7-py"

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
            response = self.get("/settings")
            settings = response.json()
            full_config["settings"] = settings
            (backup_path / ".settings" / f"settings_{timestamp}.json").write_text(
                json.dumps(settings, indent=2)
            )
            console.print("[green]✅ Backed up settings[/green]")
        except Exception as e:
            result.failures.append(f"settings: {e}")
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
            console.print(f"[yellow]⚠️ Failed to backup certificates: {e}[/yellow]")
        
        # Save full config
        full_config_path = backup_path / f"full_config_{timestamp}.json"
        full_config_path.write_text(json.dumps(full_config, indent=2))
        
        # Create latest symlink. exists() follows the link, so a symlink left
        # pointing at a pruned backup reads as absent and symlink_to() then
        # fails with FileExistsError; is_symlink() catches that case.
        latest_path = backup_path / "full_config_latest.json"
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
        domain = ", ".join(host.get("domain_names", ["?"]))
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
    out_console.print(f"  [cyan]Domains:[/cyan] {', '.join(host.get('domain_names', []))}")
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
        out_console.print(f"  [yellow]{host.get('id'):4}[/yellow] [green]{', '.join(host.get('domain_names', []))}[/green]")


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
    
    domain = ", ".join(host.get("domain_names", ["unknown"]))
    
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

    try:
        result = client.update_host(host_id, {field_name: value})
        console.print(f"[green]✅ Host {host_id} updated successfully![/green]")
        console.print(f"   {field_name} = {result.get(field_name)}")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to update host: {format_http_error(e)}[/red]")
        raise typer.Exit(1)


def select_hosts(client: NPMClient, host_ids: Optional[str], pattern: Optional[str],
                 interactive: bool, *, detail_field: Optional[str] = None) -> List[Dict]:
    """Resolve --ids / --pattern / --interactive into a list of hosts.

    Refuses to act when no filter is given. bulk-add-domain and
    bulk-remove-domain previously fell through to every host, so a bare
    `bulk-remove-domain com` would have rewritten the entire estate.
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
        return selected

    if pattern:
        # Accepts a glob or a plain substring, so '*.example.com' and 'example.com'
        # both work and the option means the same thing across every command
        needle = pattern.lower()
        return [
            h for h in all_hosts
            if any(fnmatch(d.lower(), needle) or needle in d.lower()
                   for d in h.get("domain_names", []))
        ]

    if interactive:
        console.print("\n[cyan]Select hosts:[/cyan]\n")
        for idx, host in enumerate(all_hosts):
            domains = ", ".join(host.get("domain_names", []))
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
        return [all_hosts[i] for i in indices if 0 <= i < len(all_hosts)]

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
    console.print(f"\n[cyan]🔒 Certificate {cert_id}[/cyan] ({recorded}) "
                  f"— {cert_status_label(cert)}")

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
                      f"({', '.join(source.get('domain_names', []))})[/cyan]\n")

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Setting", style="white")
        table.add_column("New host", style="green")
        table.add_row("Domain(s)", ", ".join(domains))
        table.add_row("Forwards to", f"{target.get('forward_scheme')}://"
                                     f"{target.get('forward_host')}:{target.get('forward_port')}")
        table.add_row("Certificate", cert_note)
        table.add_row("Force SSL", str(target.get("ssl_forced")))
        table.add_row("Websockets", str(target.get("allow_websocket_upgrade")))
        table.add_row("Custom locations", str(len(target.get("locations") or [])))
        table.add_row("Advanced config", "yes" if target.get("advanced_config") else "no")
        console.print(table)

    validate_certificate_assignment(client, cert_id, [{"id": "new", "domain_names": domains}])

    confirm_bulk(yes, "Create this host?")

    try:
        new_host = client.create_host_from(source, overrides)
    except requests.HTTPError as exc:
        console.print(f"[red]❌ Create failed: {exc}[/red]")
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
        label = ", ".join(domains) or "(no domains)"

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
                      "moving": moving, "staying": staying})

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
                ", ".join(plan["staying"]),
                ", ".join(plan["moving"]),
                str(plan["source"].get("certificate_id") or "none"),
            )

        console.print(table)
        console.print(f"\n[cyan]Total hosts to split: [yellow]{len(plans)}[/yellow][/cyan]")

    validate_certificate_assignment(
        client, cert_id,
        [{"id": p["id"], "domain_names": p["moving"]} for p in plans])

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

    success_count = 0
    error_count = 0

    with console.status("[bold green]Splitting hosts...") as status:
        for plan in plans:
            host_id = plan["id"]
            status.update(f"[bold green]Splitting host {host_id}...")

            # Free the domains before creating the new host so the two never
            # overlap: NPM rejects duplicates, and nginx would otherwise end up
            # with two server blocks answering to the same name.
            try:
                client.update_host(host_id, {"domain_names": plan["staying"]})
            except requests.HTTPError as exc:
                console.print(f"  [red]❌ Host {host_id}: could not trim source - {exc}[/red]")
                error_count += 1
                continue

            try:
                new_host = client.create_host_from(
                    plan["source"],
                    {"domain_names": plan["moving"], "certificate_id": cert_id})
            except requests.HTTPError as exc:
                console.print(f"  [red]❌ Host {host_id}: create failed - {exc}[/red]")
                try:
                    client.update_host(host_id, {"domain_names": plan["all"]})
                    console.print(f"     [green]↩ Host {host_id} restored[/green]")
                except requests.HTTPError as restore_exc:
                    console.print(f"     [red]‼ ROLLBACK FAILED: {restore_exc}[/red]")
                    console.print(f"     [red]‼ Host {host_id} now holds "
                                  f"{plan['staying']}; it originally held "
                                  f"{plan['all']}[/red]")
                error_count += 1
                continue

            console.print(f"  [green]✅ Host {host_id} → new host "
                          f"{new_host.get('id')} ({', '.join(plan['moving'])})[/green]")
            success_count += 1

    print_bulk_summary(success_count, error_count, skipped)


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
                "\n".join(change["current_domains"]),
                "\n".join(change["new_domains"])
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to add: [yellow]{sum(len(c['new_domains']) for c in changes)}[/yellow][/cyan]\n")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: f"Added {', '.join(c['new_domains'])}"
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
                "\n".join(change["current_domains"]),
                "\n".join(change["domains_to_remove"]),
                "\n".join(change["resulting_domains"])
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to remove: [red]{sum(len(c['domains_to_remove']) for c in changes)}[/red][/cyan]\n")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: f"Removed {', '.join(c['domains_to_remove'])}"
    )


@host_app.command("bulk-replace-domain")
def host_bulk_replace_domain(
    old_domain: str = typer.Argument(..., help="Old base domain to replace (e.g., olddomain.com)"),
    new_domain: str = typer.Argument(..., help="New base domain (e.g., newdomain.com)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    pattern: str = typer.Option(None, "--pattern", "-p", help="Only process hosts matching this domain pattern"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk replace one base domain with another in existing hosts.

    Example: Replace olddomain.com with newdomain.com everywhere it appears:
        host bulk-replace-domain olddomain.com newdomain.com -p olddomain.com

    This will change ex.olddomain.com to ex.newdomain.com. A host selector is
    required; the command no longer defaults to every host carrying the old
    domain, since a short argument like 'com' would match the whole estate.
    """
    client = get_client()

    hosts_to_process = select_hosts(client, host_ids, pattern, interactive)
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
            if old_domain.lower() in domain.lower():
                new_domain_full = domain.lower().replace(old_domain.lower(), new_domain.lower())
                new_domains.append(new_domain_full)
                replaced.append((domain, new_domain_full))
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
                table.add_row(str(change["host_id"]), old, new)
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(changes)}[/yellow][/cyan]")
        console.print(f"[cyan]Total domains to replace: [yellow]{sum(len(c['replacements']) for c in changes)}[/yellow][/cyan]\n")

    if collisions:
        console.print(f"[yellow]⚠️  Host(s) {', '.join(str(h) for h in collisions)} already "
                      f"carry a name the rewrite produces; the duplicate will be "
                      f"dropped rather than stored twice[/yellow]\n")

    confirm_bulk(yes)
    apply_domain_changes(
        client, changes,
        lambda c: ", ".join(f"{old}→{new}" for old, new in c["replacements"])
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
                ", ".join(host.get("domain_names", [])),
                str(host.get(field, "N/A")),
                str(typed_value)
            )
        
        console.print(table)
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(hosts_to_process)}[/yellow][/cyan]")

    # Field-aware validation. Pointing a host at a deleted certificate makes
    # NPM render it with no TLS listener at all rather than failing loudly.
    if field == "certificate_id":
        if not validate_certificate_assignment(client, typed_value, hosts_to_process):
            raise typer.Exit(1)

    confirm_bulk(yes)

    success_count = 0
    error_count = 0

    with console.status("[bold green]Applying changes...") as status:
        for host in hosts_to_process:
            host_id = host.get("id")

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
