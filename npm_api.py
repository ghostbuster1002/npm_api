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
    from rich import print as rprint
except ImportError as e:
    print("\n" + "=" * 60)
    print("ERROR: Required Python packages are not installed!")
    print("=" * 60)
    print(f"\nMissing module: {e.name}")
    print("\nPlease install the required packages:")
    print("\n  Option 1 - Using pip (recommended):")
    print('    pip install requests "typer[all]" rich')
    print("\n  Option 2 - Using a virtual environment:")
    print("    python3 -m venv venv")
    print("    source venv/bin/activate  # On Windows: venv\\Scripts\\activate")
    print('    pip install requests "typer[all]" rich')
    print("\n  Option 3 - Using pipx (for isolated installation):")
    print("    pipx install npm-api  # If packaged")
    print("\n" + "=" * 60)
    sys.exit(1)

# Version
VERSION = "3.0.7-py"

# Initialize Rich console and Typer app
console = Console()
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
            # Try to use a sensible default
            try:
                self.data_dir = str(Path(__file__).parent / "data")
            except NameError:
                # For frozen executables
                self.data_dir = str(Path.home() / ".npm-api" / "data")
    
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


# =============================================================================
# API Client
# =============================================================================

class NPMClient:
    """Nginx Proxy Manager API Client"""
    
    def __init__(self, config: Config):
        self.config = config
        self.token: Optional[str] = None
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
    
    def check_connection(self) -> bool:
        """Check if NPM is accessible"""
        try:
            response = requests.head(self.config.base_url, timeout=5)
            return response.status_code < 500
        except requests.RequestException:
            return False
    
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
        """Generate a new API token"""
        console.print("[yellow]🔄 Generating new API token...[/yellow]")
        
        # First get temporary token
        try:
            response = requests.post(
                f"{self.config.base_url}/tokens",
                json={"identity": self.config.api_user, "secret": self.config.api_pass},
                timeout=10
            )
            
            if response.status_code != 200:
                console.print(f"[red]❌ Failed to authenticate. Status: {response.status_code}[/red]")
                return False
            
            temp_token = response.json().get("token")
            
            # Get long-term token
            response = requests.get(
                f"{self.config.base_url}/tokens?expiry={self.config.token_expiry}",
                headers={"Authorization": f"Bearer {temp_token}"},
                timeout=10
            )
            
            if response.status_code != 200:
                console.print(f"[red]❌ Failed to generate long-term token. Status: {response.status_code}[/red]")
                return False
            
            data = response.json()
            self.token = data["token"]
            expiry = data["expires"]
            
            # Save token and expiry with secure permissions
            token_path = Path(self.config.token_file)
            expiry_path = Path(self.config.expiry_file)
            
            token_path.write_text(self.token)
            token_path.chmod(0o600)  # Owner read/write only
            
            expiry_path.write_text(expiry)
            expiry_path.chmod(0o600)  # Owner read/write only
            
            console.print("[green]✅ Token generated successfully![/green]")
            console.print(f"[yellow]📅 Expires: {expiry}[/yellow]")
            return True
            
        except requests.RequestException as e:
            console.print(f"[red]❌ Connection error: {e}[/red]")
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
            raise RuntimeError("Failed to obtain API token")
        
        url = f"{self.config.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        headers.update(kwargs.pop("headers", {}))
        
        response = requests.request(method, url, headers=headers, **kwargs)
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
            "access_list_id": None,
            "certificate_id": None,
            "meta": {"dns_challenge": None},
            "enabled": True
        }
        
        response = self.post("/nginx/proxy-hosts", json=data)
        response.raise_for_status()
        return response.json()
    
    def update_host(self, host_id: int, updates: Dict) -> Dict:
        """Update a proxy host"""
        # Get current config
        current = self.get_host(host_id)
        
        # Fields that can be updated
        updatable_fields = [
            "domain_names", "forward_host", "forward_port", "forward_scheme",
            "caching_enabled", "block_exploits", "allow_websocket_upgrade",
            "http2_support", "ssl_forced", "hsts_enabled", "hsts_subdomains",
            "advanced_config", "locations", "access_list_id", "certificate_id",
            "enabled", "meta"
        ]
        
        # Build update payload
        data = {k: current.get(k) for k in updatable_fields if k in current}
        data.update(updates)
        
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
    
    def enable_host_ssl(self, host_id: int, cert_id: int) -> bool:
        """Enable SSL for a proxy host"""
        data = {
            "certificate_id": cert_id,
            "ssl_forced": True,
            "http2_support": True
        }
        response = self.put(f"/nginx/proxy-hosts/{host_id}", json=data)
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
    
    def download_certificate(self, cert_id: int, output_dir: str, cert_name: str) -> bool:
        """Download certificate files"""
        # Sanitize cert_name to prevent path traversal (defense in depth)
        cert_name = re.sub(r'[^a-zA-Z0-9._-]', '_', cert_name)
        
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Try new API first
        try:
            response = self.get(f"/nginx/certificates/{cert_id}/certificates",
                               headers={"Accept": "application/json"})
            if response.status_code == 200:
                data = response.json()
                
                cert_path = output_path / f"{cert_name}.crt"
                key_path = output_path / f"{cert_name}.key"
                
                cert_path.write_text(data.get("certificate", ""))
                key_path.write_text(data.get("private", ""))
                key_path.chmod(0o600)
                
                if "intermediate" in data:
                    chain_path = output_path / f"{cert_name}.chain.crt"
                    chain_path.write_text(data["intermediate"])
                
                # Get metadata
                meta = self.get_certificate(cert_id)
                meta_path = output_path / f"{cert_name}_metadata.json"
                meta_path.write_text(json.dumps(meta, indent=2))
                
                return True
        except Exception:
            pass
        
        # Fallback to legacy ZIP download
        try:
            response = self.get(f"/nginx/certificates/{cert_id}/download")
            if response.status_code == 200:
                zip_path = output_path / "temp_cert.zip"
                zip_path.write_bytes(response.content)
                
                # Safe extraction - prevent zip slip attacks
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for member in zf.namelist():
                        # Ensure no path traversal in zip contents
                        member_path = output_path / member
                        if not str(member_path.resolve()).startswith(str(output_path)):
                            continue  # Skip potentially malicious paths
                        zf.extract(member, output_path)
                
                zip_path.unlink()
                return True
        except Exception:
            pass
        
        return False
    
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
        """Get dashboard statistics"""
        stats = {
            "proxy_hosts": {"total": 0, "enabled": 0, "disabled": 0},
            "certificates": {"total": 0, "valid": 0, "expired": 0},
            "redirections": 0,
            "streams": 0,
            "users": 0,
            "access_lists": 0
        }
        
        try:
            hosts = self.list_hosts()
            stats["proxy_hosts"]["total"] = len(hosts)
            stats["proxy_hosts"]["enabled"] = sum(1 for h in hosts if h.get("enabled"))
            stats["proxy_hosts"]["disabled"] = stats["proxy_hosts"]["total"] - stats["proxy_hosts"]["enabled"]
        except Exception:
            pass
        
        try:
            certs = self.list_certificates()
            stats["certificates"]["total"] = len(certs)
            stats["certificates"]["expired"] = sum(1 for c in certs if c.get("expired"))
            stats["certificates"]["valid"] = stats["certificates"]["total"] - stats["certificates"]["expired"]
        except Exception:
            pass
        
        try:
            response = self.get("/nginx/redirection-hosts")
            stats["redirections"] = len(response.json())
        except Exception:
            pass
        
        try:
            response = self.get("/nginx/streams")
            stats["streams"] = len(response.json())
        except Exception:
            pass
        
        try:
            stats["users"] = len(self.list_users())
        except Exception:
            pass
        
        try:
            stats["access_lists"] = len(self.list_access_lists())
        except Exception:
            pass
        
        return stats
    
    # =========================================================================
    # Backup Methods
    # =========================================================================
    
    def full_backup(self) -> str:
        """Perform a full backup of all configurations"""
        timestamp = datetime.now().strftime("%Y_%m_%d__%H_%M_%S")
        backup_path = Path(self.config.backup_dir)
        
        # Create directories
        for subdir in [".user", ".settings", ".access_lists", ".Proxy_Hosts", ".ssl"]:
            (backup_path / subdir).mkdir(parents=True, exist_ok=True)
        
        full_config = {}
        
        # Backup users
        try:
            users = self.list_users()
            full_config["users"] = users
            (backup_path / ".user" / f"users_{timestamp}.json").write_text(
                json.dumps(users, indent=2)
            )
            console.print(f"[green]✅ Backed up {len(users)} users[/green]")
        except Exception as e:
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
            console.print(f"[yellow]⚠️ Failed to backup proxy hosts: {e}[/yellow]")
        
        # Backup certificates
        try:
            certs = self.list_certificates()
            full_config["certificates"] = certs
            
            for cert in certs:
                cert_id = cert["id"]
                cert_name = cert.get("nice_name") or cert.get("domain_names", ["cert"])[0]
                cert_name_safe = re.sub(r'[^a-zA-Z0-9.]', '_', cert_name)
                cert_dir = backup_path / ".ssl" / cert_name_safe
                cert_dir.mkdir(parents=True, exist_ok=True)
                
                (cert_dir / "certificate_meta.json").write_text(
                    json.dumps(cert, indent=2)
                )
                
                # Try to download cert files
                self.download_certificate(cert_id, str(cert_dir), "cert")
            
            console.print(f"[green]✅ Backed up {len(certs)} certificates[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠️ Failed to backup certificates: {e}[/yellow]")
        
        # Save full config
        full_config_path = backup_path / f"full_config_{timestamp}.json"
        full_config_path.write_text(json.dumps(full_config, indent=2))
        
        # Create latest symlink
        latest_path = backup_path / "full_config_latest.json"
        if latest_path.exists():
            latest_path.unlink()
        latest_path.symlink_to(full_config_path.name)
        
        return str(backup_path)


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
# CLI Commands - Main
# =============================================================================

@app.command()
def info():
    """Display script variables and dashboard information"""
    client = get_client()
    config = client.config
    
    console.print(f"\n[yellow]Script Info: [green]{VERSION}[/green][/yellow]")
    console.print(f"[green]Config from[/green] : {config.get_config_info()}")
    console.print(f"[green]BASE URL[/green]   : {config.base_url}")
    console.print(f"[green]NGINX IP[/green]   : {config.nginx_ip}")
    console.print(f"[green]USER NPM[/green]   : {config.api_user}")
    console.print(f"[green]BACKUP DIR[/green] : {config.data_dir_id}")
    
    # Dashboard
    if not client.ensure_token():
        console.print("[red]❌ Failed to authenticate[/red]")
        return
    
    stats = client.get_dashboard_stats()
    
    console.print("\n[cyan]📊 NGINX Proxy Manager Dashboard 🔧[/cyan]")
    
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Component", style="white")
    table.add_column("Status", justify="right")
    
    table.add_row("🌐 Proxy Hosts", f"[yellow]{stats['proxy_hosts']['total']}[/yellow]")
    table.add_row("├─ Enabled", f"[green]{stats['proxy_hosts']['enabled']}[/green]")
    table.add_row("└─ Disabled", f"[red]{stats['proxy_hosts']['disabled']}[/red]")
    table.add_row("🔒 Certificates", f"[yellow]{stats['certificates']['total']}[/yellow]")
    table.add_row("├─ Valid", f"[green]{stats['certificates']['valid']}[/green]")
    table.add_row("└─ Expired", f"[red]{stats['certificates']['expired']}[/red]")
    table.add_row("🔄 Redirections", str(stats['redirections']))
    table.add_row("🔌 Stream Hosts", str(stats['streams']))
    table.add_row("🔒 Access Lists", str(stats['access_lists']))
    table.add_row("👥 Users", str(stats['users']))
    
    console.print(table)
    console.print("\n[yellow]💡 Use --help to see available commands[/yellow]")


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
            console.print("[red]❌ Failed to generate token[/red]")


@app.command()
def backup():
    """Backup all configurations"""
    client = get_client()
    
    console.print("\n[yellow]📦 Starting full backup...[/yellow]")
    
    try:
        backup_path = client.full_backup()
        console.print(f"\n[green]✅ Backup completed![/green]")
        console.print(f"[cyan]📂 Backup location: {backup_path}[/cyan]")
    except Exception as e:
        console.print(f"[red]❌ Backup failed: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def show_defaults():
    """Show default settings for host creation"""
    defaults = ProxyHostDefaults()
    
    console.print("\n[yellow]📝 Default Settings for Creating Hosts:[/yellow]")
    console.print("\n[green]Basic Settings:[/green]")
    console.print(f"  Forward Scheme:          [cyan]{defaults.forward_scheme}[/cyan]")
    console.print(f"  Caching Enabled:         {'[green]true[/green]' if defaults.caching_enabled else '[red]false[/red]'}")
    console.print(f"  Block Exploits:          {'[green]true[/green]' if defaults.block_exploits else '[red]false[/red]'}")
    console.print(f"  Allow Websocket Upgrade: {'[green]true[/green]' if defaults.allow_websocket_upgrade else '[red]false[/red]'}")
    
    console.print("\n[green]SSL Settings:[/green]")
    console.print(f"  HTTP/2 Support:          {'[green]true[/green]' if defaults.http2_support else '[red]false[/red]'}")
    console.print(f"  SSL Forced:              {'[green]true[/green]' if defaults.ssl_forced else '[red]false[/red]'}")
    console.print(f"  HSTS Enabled:            {'[green]true[/green]' if defaults.hsts_enabled else '[red]false[/red]'}")
    console.print(f"  HSTS Subdomains:         {'[green]true[/green]' if defaults.hsts_subdomains else '[red]false[/red]'}")


# =============================================================================
# CLI Commands - Hosts
# =============================================================================

@host_app.command("list")
def host_list():
    """List all proxy hosts"""
    client = get_client()
    
    hosts = client.list_hosts()
    
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
    
    console.print(table)


@host_app.command("show")
def host_show(host_id: int = typer.Argument(..., help="Host ID to show")):
    """Show details of a specific proxy host"""
    client = get_client()
    
    try:
        host = client.get_host(host_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Host ID {host_id} not found[/red]")
        raise typer.Exit(1)
    
    console.print(f"\n[yellow]📋 Host Details:[/yellow]")
    console.print(f"  [cyan]ID:[/cyan] {host.get('id')}")
    console.print(f"  [cyan]Domains:[/cyan] {', '.join(host.get('domain_names', []))}")
    console.print(f"  [cyan]Forward Host:[/cyan] {host.get('forward_host')}")
    console.print(f"  [cyan]Forward Port:[/cyan] {host.get('forward_port')}")
    console.print(f"  [cyan]Forward Scheme:[/cyan] {host.get('forward_scheme')}")
    console.print(f"  [cyan]Enabled:[/cyan] {'[green]Yes[/green]' if host.get('enabled') else '[red]No[/red]'}")
    console.print(f"  [cyan]Certificate ID:[/cyan] {host.get('certificate_id') or 'None'}")
    console.print(f"  [cyan]SSL Forced:[/cyan] {'[green]Yes[/green]' if host.get('ssl_forced') else '[red]No[/red]'}")
    console.print(f"  [cyan]HTTP/2:[/cyan] {'[green]Yes[/green]' if host.get('http2_support') else '[red]No[/red]'}")
    console.print(f"  [cyan]Block Exploits:[/cyan] {'[green]Yes[/green]' if host.get('block_exploits') else '[red]No[/red]'}")
    console.print(f"  [cyan]Caching:[/cyan] {'[green]Yes[/green]' if host.get('caching_enabled') else '[red]No[/red]'}")
    console.print(f"  [cyan]Websocket:[/cyan] {'[green]Yes[/green]' if host.get('allow_websocket_upgrade') else '[red]No[/red]'}")
    console.print(f"  [cyan]Access List ID:[/cyan] {host.get('access_list_id') or 'None'}")
    
    if host.get('advanced_config'):
        console.print(f"\n  [cyan]Advanced Config:[/cyan]")
        console.print(Syntax(host['advanced_config'], "nginx", theme="monokai"))


@host_app.command("search")
def host_search(search: str = typer.Argument(..., help="Domain name to search")):
    """Search proxy hosts by domain name"""
    client = get_client()
    
    hosts = client.search_hosts(search)
    
    if not hosts:
        console.print(f"[yellow]No hosts found matching '{search}'[/yellow]")
        return
    
    for host in hosts:
        console.print(f"  [yellow]{host.get('id'):4}[/yellow] [green]{', '.join(host.get('domain_names', []))}[/green]")


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
        console.print(f"[red]❌ Failed to create host: {e}[/red]")
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
        
        if not typer.confirm("Are you sure?"):
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
    
    field, value = field_value.split("=", 1)
    
    # Convert value types
    if value.lower() in ("true", "false"):
        value = value.lower() == "true"
    elif value.isdigit():
        value = int(value)
    
    try:
        result = client.update_host(host_id, {field: value})
        console.print(f"[green]✅ Host {host_id} updated successfully![/green]")
        console.print(f"   {field} = {result.get(field)}")
    except requests.HTTPError as e:
        console.print(f"[red]❌ Failed to update host: {e}[/red]")
        raise typer.Exit(1)


@host_app.command("ssl-enable")
def host_ssl_enable(
    host_id: int = typer.Argument(..., help="Host ID"),
    cert_id: int = typer.Argument(..., help="Certificate ID")
):
    """Enable SSL for a proxy host"""
    client = get_client()
    
    if client.enable_host_ssl(host_id, cert_id):
        console.print(f"[green]✅ SSL enabled for host {host_id} with certificate {cert_id}[/green]")
    else:
        console.print(f"[red]❌ Failed to enable SSL[/red]")
        raise typer.Exit(1)


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
def cert_list():
    """List all SSL certificates"""
    client = get_client()
    
    certs = client.list_certificates()
    
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
        expired = cert.get("expired", False)
        status = "[red]❌ EXPIRED[/red]" if expired else "[green]✅ VALID[/green]"
        
        table.add_row(cert_id, domains, provider, expires, status)
    
    console.print(table)


@cert_app.command("show")
def cert_show(identifier: str = typer.Argument(..., help="Certificate ID or domain name")):
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
    
    for cert in certs:
        console.print(f"\n[cyan]🔒 Certificate ID: {cert.get('id')}[/cyan]")
        console.print(f"   Domains: {', '.join(cert.get('domain_names', []))}")
        console.print(f"   Provider: {cert.get('provider')}")
        console.print(f"   Created: {cert.get('created_on', 'N/A')}")
        console.print(f"   Expires: {cert.get('expires_on', 'N/A')}")
        expired = cert.get("expired", False)
        console.print(f"   Status: {'[red]❌ EXPIRED[/red]' if expired else '[green]✅ VALID[/green]'}")


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
    if existing and not existing.get("expired"):
        console.print(f"[yellow]🔔 Valid certificate already exists for {domain}[/yellow]")
        console.print(f"   Certificate ID: {existing.get('id')}")
        return
    
    if not yes:
        console.print(f"\n[yellow]📝 Certificate generation parameters:[/yellow]")
        console.print(f"   Domain: {domain}")
        console.print(f"   Email: {email}")
        if dns_provider:
            console.print(f"   DNS Provider: {dns_provider}")
        
        if not typer.confirm("Generate certificate?"):
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
        console.print(f"[red]❌ Failed to generate certificate: {e}[/red]")
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
        
        if not typer.confirm("Are you sure?"):
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
    
    if client.download_certificate(cert_id, output_dir, cert_name):
        console.print(f"[green]✅ Certificate downloaded successfully![/green]")
        console.print(f"   Files saved to: {output_dir}")
    else:
        console.print(f"[red]❌ Failed to download certificate[/red]")
        raise typer.Exit(1)


# =============================================================================
# CLI Commands - Users
# =============================================================================

@user_app.command("list")
def user_list():
    """List all users"""
    client = get_client()
    
    users = client.list_users()
    
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
    
    console.print(table)


@user_app.command("create")
def user_create(
    username: str = typer.Argument(..., help="Username"),
    email: str = typer.Argument(..., help="Email address"),
    password: str = typer.Argument(..., help="Password")
):
    """Create a new user"""
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
        console.print(f"[red]❌ Failed to create user: {e}[/red]")
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
        
        if not typer.confirm("Are you sure?"):
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
    with the new base domain.
    """
    client = get_client()
    
    all_hosts = client.list_hosts()
    
    if not all_hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return
    
    # Filter hosts by IDs if specified
    if host_ids:
        selected_ids = [int(x.strip()) for x in host_ids.split(",")]
        hosts_to_process = [h for h in all_hosts if h.get("id") in selected_ids]
        if not hosts_to_process:
            console.print(f"[red]❌ No hosts found with IDs: {host_ids}[/red]")
            raise typer.Exit(1)
    elif pattern:
        # Filter by domain pattern
        hosts_to_process = [
            h for h in all_hosts 
            if any(pattern.lower() in d.lower() for d in h.get("domain_names", []))
        ]
        if not hosts_to_process:
            console.print(f"[red]❌ No hosts found matching pattern: {pattern}[/red]")
            raise typer.Exit(1)
    elif interactive:
        # Interactive selection
        console.print("\n[cyan]Select hosts to update:[/cyan]\n")
        
        for idx, host in enumerate(all_hosts):
            host_id = host.get("id")
            domains = ", ".join(host.get("domain_names", []))
            console.print(f"  [{idx + 1}] [yellow]ID {host_id}[/yellow]: [green]{domains}[/green]")
        
        console.print("\n[cyan]Enter host numbers (comma-separated) or 'all' for all hosts:[/cyan]")
        selection = typer.prompt("Selection")
        
        if selection.lower() == "all":
            hosts_to_process = all_hosts
        else:
            try:
                indices = [int(x.strip()) - 1 for x in selection.split(",")]
                hosts_to_process = [all_hosts[i] for i in indices if 0 <= i < len(all_hosts)]
            except (ValueError, IndexError):
                console.print("[red]❌ Invalid selection[/red]")
                raise typer.Exit(1)
    else:
        hosts_to_process = all_hosts
    
    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return
    
    # Calculate changes
    changes = []
    for host in hosts_to_process:
        host_id = host.get("id")
        current_domains = host.get("domain_names", [])
        
        # Extract unique subdomain prefixes
        prefixes = set()
        for domain in current_domains:
            parts = domain.split(".")
            if len(parts) >= 2:
                # Get the subdomain part (everything before the base domain)
                # e.g., "ex.mydomain.com" -> "ex"
                # e.g., "sub.ex.mydomain.com" -> "sub.ex"
                prefix = parts[0]
                prefixes.add(prefix)
        
        # Generate new domains
        new_domains_to_add = []
        for prefix in prefixes:
            new_domain_full = f"{prefix}.{new_domain}"
            if new_domain_full not in current_domains:
                new_domains_to_add.append(new_domain_full)
        
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
    if preview or not yes:
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
    
    # Confirm
    if not yes:
        if not typer.confirm("Apply these changes?"):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    # Apply changes
    success_count = 0
    error_count = 0
    
    with console.status("[bold green]Applying changes...") as status:
        for change in changes:
            host_id = change["host_id"]
            new_domain_list = change["resulting_domains"]
            
            try:
                status.update(f"[bold green]Updating host {host_id}...")
                client.update_host(host_id, {"domain_names": new_domain_list})
                console.print(f"  [green]✅ Host {host_id}: Added {', '.join(change['new_domains'])}[/green]")
                success_count += 1
            except Exception as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {e}[/red]")
                error_count += 1
    
    # Summary
    console.print(f"\n[cyan]📊 Summary:[/cyan]")
    console.print(f"   [green]✅ Successful: {success_count}[/green]")
    if error_count:
        console.print(f"   [red]❌ Failed: {error_count}[/red]")


@host_app.command("bulk-remove-domain")
def host_bulk_remove_domain(
    domain_pattern: str = typer.Argument(..., help="Domain pattern to remove (e.g., my3rddomain.com or full domain)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk remove domains matching a pattern from existing hosts.
    
    Example: Remove all domains containing 'my3rddomain.com':
        host bulk-remove-domain my3rddomain.com
    """
    client = get_client()
    
    all_hosts = client.list_hosts()
    
    if not all_hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return
    
    # Filter hosts by IDs if specified
    if host_ids:
        selected_ids = [int(x.strip()) for x in host_ids.split(",")]
        hosts_to_process = [h for h in all_hosts if h.get("id") in selected_ids]
    elif interactive:
        # Interactive selection
        console.print("\n[cyan]Select hosts to update:[/cyan]\n")
        
        for idx, host in enumerate(all_hosts):
            host_id = host.get("id")
            domains = ", ".join(host.get("domain_names", []))
            console.print(f"  [{idx + 1}] [yellow]ID {host_id}[/yellow]: [green]{domains}[/green]")
        
        console.print("\n[cyan]Enter host numbers (comma-separated) or 'all' for all hosts:[/cyan]")
        selection = typer.prompt("Selection")
        
        if selection.lower() == "all":
            hosts_to_process = all_hosts
        else:
            try:
                indices = [int(x.strip()) - 1 for x in selection.split(",")]
                hosts_to_process = [all_hosts[i] for i in indices if 0 <= i < len(all_hosts)]
            except (ValueError, IndexError):
                console.print("[red]❌ Invalid selection[/red]")
                raise typer.Exit(1)
    else:
        hosts_to_process = all_hosts
    
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
    if preview or not yes:
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
    
    # Confirm
    if not yes:
        if not typer.confirm("Apply these changes?"):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    # Apply changes
    success_count = 0
    error_count = 0
    
    with console.status("[bold green]Applying changes...") as status:
        for change in changes:
            host_id = change["host_id"]
            new_domain_list = change["resulting_domains"]
            
            try:
                status.update(f"[bold green]Updating host {host_id}...")
                client.update_host(host_id, {"domain_names": new_domain_list})
                console.print(f"  [green]✅ Host {host_id}: Removed {', '.join(change['domains_to_remove'])}[/green]")
                success_count += 1
            except Exception as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {e}[/red]")
                error_count += 1
    
    # Summary
    console.print(f"\n[cyan]📊 Summary:[/cyan]")
    console.print(f"   [green]✅ Successful: {success_count}[/green]")
    if error_count:
        console.print(f"   [red]❌ Failed: {error_count}[/red]")


@host_app.command("bulk-replace-domain")
def host_bulk_replace_domain(
    old_domain: str = typer.Argument(..., help="Old base domain to replace (e.g., olddomain.com)"),
    new_domain: str = typer.Argument(..., help="New base domain (e.g., newdomain.com)"),
    host_ids: str = typer.Option(None, "--ids", "-i", help="Comma-separated host IDs to update"),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Preview changes before applying"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip confirmation"),
    interactive: bool = typer.Option(False, "--interactive", "-I", help="Interactively select hosts")
):
    """
    Bulk replace one base domain with another in existing hosts.
    
    Example: Replace olddomain.com with newdomain.com:
        host bulk-replace-domain olddomain.com newdomain.com
    
    This will change ex.olddomain.com to ex.newdomain.com
    """
    client = get_client()
    
    all_hosts = client.list_hosts()
    
    if not all_hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return
    
    # Filter hosts by IDs if specified
    if host_ids:
        selected_ids = [int(x.strip()) for x in host_ids.split(",")]
        hosts_to_process = [h for h in all_hosts if h.get("id") in selected_ids]
    elif interactive:
        # Show only hosts that have the old domain
        matching_hosts = [
            h for h in all_hosts 
            if any(old_domain.lower() in d.lower() for d in h.get("domain_names", []))
        ]
        
        if not matching_hosts:
            console.print(f"[yellow]No hosts found with domain pattern: {old_domain}[/yellow]")
            return
        
        console.print("\n[cyan]Select hosts to update:[/cyan]\n")
        
        for idx, host in enumerate(matching_hosts):
            host_id = host.get("id")
            domains = ", ".join(host.get("domain_names", []))
            console.print(f"  [{idx + 1}] [yellow]ID {host_id}[/yellow]: [green]{domains}[/green]")
        
        console.print("\n[cyan]Enter host numbers (comma-separated) or 'all' for all hosts:[/cyan]")
        selection = typer.prompt("Selection")
        
        if selection.lower() == "all":
            hosts_to_process = matching_hosts
        else:
            try:
                indices = [int(x.strip()) - 1 for x in selection.split(",")]
                hosts_to_process = [matching_hosts[i] for i in indices if 0 <= i < len(matching_hosts)]
            except (ValueError, IndexError):
                console.print("[red]❌ Invalid selection[/red]")
                raise typer.Exit(1)
    else:
        hosts_to_process = [
            h for h in all_hosts 
            if any(old_domain.lower() in d.lower() for d in h.get("domain_names", []))
        ]
    
    if not hosts_to_process:
        console.print(f"[yellow]No hosts found with domain pattern: {old_domain}[/yellow]")
        return
    
    # Calculate changes
    changes = []
    for host in hosts_to_process:
        host_id = host.get("id")
        current_domains = host.get("domain_names", [])
        
        # Replace domains
        new_domains = []
        replaced = []
        for domain in current_domains:
            if old_domain.lower() in domain.lower():
                # Replace the old domain part with new domain
                new_domain_full = domain.lower().replace(old_domain.lower(), new_domain.lower())
                new_domains.append(new_domain_full)
                replaced.append((domain, new_domain_full))
            else:
                new_domains.append(domain)
        
        if replaced:
            changes.append({
                "host_id": host_id,
                "current_domains": current_domains,
                "replacements": replaced,
                "resulting_domains": new_domains
            })
    
    if not changes:
        console.print("[yellow]No changes to make[/yellow]")
        return
    
    # Preview changes
    if preview or not yes:
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
    
    # Confirm
    if not yes:
        if not typer.confirm("Apply these changes?"):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    # Apply changes
    success_count = 0
    error_count = 0
    
    with console.status("[bold green]Applying changes...") as status:
        for change in changes:
            host_id = change["host_id"]
            new_domain_list = change["resulting_domains"]
            
            try:
                status.update(f"[bold green]Updating host {host_id}...")
                client.update_host(host_id, {"domain_names": new_domain_list})
                replacements_str = ", ".join([f"{old}→{new}" for old, new in change["replacements"]])
                console.print(f"  [green]✅ Host {host_id}: {replacements_str}[/green]")
                success_count += 1
            except Exception as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {e}[/red]")
                error_count += 1
    
    # Summary
    console.print(f"\n[cyan]📊 Summary:[/cyan]")
    console.print(f"   [green]✅ Successful: {success_count}[/green]")
    if error_count:
        console.print(f"   [red]❌ Failed: {error_count}[/red]")


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
    
    # Convert value to appropriate type
    if value.lower() == "true":
        typed_value = True
    elif value.lower() == "false":
        typed_value = False
    elif value.isdigit():
        typed_value = int(value)
    else:
        typed_value = value
    
    all_hosts = client.list_hosts()
    
    if not all_hosts:
        console.print("[yellow]No proxy hosts found[/yellow]")
        return
    
    # Filter hosts
    if host_ids:
        selected_ids = [int(x.strip()) for x in host_ids.split(",")]
        hosts_to_process = [h for h in all_hosts if h.get("id") in selected_ids]
    elif pattern:
        hosts_to_process = [
            h for h in all_hosts 
            if any(pattern.lower() in d.lower() for d in h.get("domain_names", []))
        ]
    elif interactive:
        console.print("\n[cyan]Select hosts to update:[/cyan]\n")
        
        for idx, host in enumerate(all_hosts):
            host_id = host.get("id")
            domains = ", ".join(host.get("domain_names", []))
            current_val = host.get(field, "N/A")
            console.print(f"  [{idx + 1}] [yellow]ID {host_id}[/yellow]: [green]{domains}[/green] ({field}={current_val})")
        
        console.print("\n[cyan]Enter host numbers (comma-separated) or 'all' for all hosts:[/cyan]")
        selection = typer.prompt("Selection")
        
        if selection.lower() == "all":
            hosts_to_process = all_hosts
        else:
            try:
                indices = [int(x.strip()) - 1 for x in selection.split(",")]
                hosts_to_process = [all_hosts[i] for i in indices if 0 <= i < len(all_hosts)]
            except (ValueError, IndexError):
                console.print("[red]❌ Invalid selection[/red]")
                raise typer.Exit(1)
    else:
        console.print("[red]❌ Please specify --ids, --pattern, or --interactive[/red]")
        raise typer.Exit(1)
    
    if not hosts_to_process:
        console.print("[yellow]No hosts selected for processing[/yellow]")
        return
    
    # Preview changes
    if preview or not yes:
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
        console.print(f"\n[cyan]Total hosts to update: [yellow]{len(hosts_to_process)}[/yellow][/cyan]\n")
    
    # Confirm
    if not yes:
        if not typer.confirm("Apply these changes?"):
            console.print("[red]❌ Cancelled[/red]")
            raise typer.Exit(0)
    
    # Apply changes
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
            except Exception as e:
                console.print(f"  [red]❌ Host {host_id}: Failed - {e}[/red]")
                error_count += 1
    
    # Summary
    console.print(f"\n[cyan]📊 Summary:[/cyan]")
    console.print(f"   [green]✅ Successful: {success_count}[/green]")
    if error_count:
        console.print(f"   [red]❌ Failed: {error_count}[/red]")


@acl_app.command("list")
def acl_list():
    """List all access lists"""
    client = get_client()
    
    access_lists = client.list_access_lists()
    
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
    
    console.print(table)


@acl_app.command("show")
def acl_show(list_id: int = typer.Argument(..., help="Access List ID")):
    """Show access list details"""
    client = get_client()
    
    try:
        al = client.get_access_list(list_id)
    except requests.HTTPError:
        console.print(f"[red]❌ Access list ID {list_id} not found[/red]")
        raise typer.Exit(1)
    
    console.print(f"\n[cyan]🔑 Access List Details:[/cyan]")
    console.print(f"   ID: {al.get('id')}")
    console.print(f"   Name: {al.get('name')}")
    console.print(f"   Satisfy: {'Any' if al.get('satisfy_any') else 'All'}")
    console.print(f"   Pass Auth: {'Yes' if al.get('pass_auth') else 'No'}")
    
    items = al.get("items", [])
    if items:
        console.print(f"\n   [cyan]Authorized Users:[/cyan]")
        for item in items:
            console.print(f"      • {item.get('username')}")
    
    clients = al.get("clients", [])
    if clients:
        console.print(f"\n   [cyan]IP Rules:[/cyan]")
        for client_item in clients:
            directive = client_item.get("directive", "allow")
            color = "green" if directive == "allow" else "red"
            console.print(f"      • [{color}]{directive}[/{color}] {client_item.get('address')}")


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
            password = typer.prompt(f"Password for {user}", hide_input=True)
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
        console.print(f"[red]❌ Failed to create access list: {e}[/red]")
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
        
        if not typer.confirm("Are you sure?"):
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
        console.print(f"[red]❌ Failed to update access list: {e}[/red]")
        raise typer.Exit(1)


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Main entry point"""
    app()


if __name__ == "__main__":
    main()
