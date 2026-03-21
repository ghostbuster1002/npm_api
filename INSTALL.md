# Installation Guide

## Quick Start

### Option 1: Build Binary (Recommended)

```bash
# Build
make build

# Install system-wide
sudo make install

# Verify
npm-api --help
```

### Option 2: Manual Build

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install requests "typer[all]" rich pyinstaller

# Build binary
pyinstaller --onefile --name npm-api --clean --strip npm_api.py

# Install
sudo cp dist/npm-api /usr/local/bin/
sudo chmod +x /usr/local/bin/npm-api
```

## Configuration

After installation, configure credentials using ONE of these methods:

### Method 1: Environment Variables (Recommended)

```bash
# Add to ~/.bashrc or ~/.zshrc
export NPM_API_HOST="192.168.1.100"
export NPM_API_PORT="81"
export NPM_API_USER="admin@example.com"
export NPM_API_PASS="your_password"
```

### Method 2: Config File

```bash
# Create user config
mkdir -p ~/.config/npm-api
cat > ~/.config/npm-api/npm-api.conf << 'EOF'
NGINX_IP="192.168.1.100"
NGINX_PORT="81"
API_USER="admin@example.com"
API_PASS="your_password"
EOF
chmod 600 ~/.config/npm-api/npm-api.conf
```

## Uninstall

```bash
sudo make uninstall
# or
sudo rm /usr/local/bin/npm-api
```

## Troubleshooting

### Build fails

```bash
# Ensure Python dev tools are installed
sudo apt install python3-dev python3-venv  # Debian/Ubuntu
sudo dnf install python3-devel             # Fedora
```

### Binary not found after install

```bash
# Check if /usr/local/bin is in PATH
echo $PATH | grep -q "/usr/local/bin" && echo "OK" || echo "Add to PATH"

# Add to PATH if needed
echo 'export PATH="/usr/local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```
