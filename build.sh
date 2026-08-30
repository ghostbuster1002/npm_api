#!/bin/bash
# Build script for npm-api CLI tool
# Creates a standalone binary using PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="npm-api"
PYTHON_FILE="npm_api.py"

echo "=========================================="
echo "  NPM-API Binary Builder"
echo "=========================================="

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 is required but not installed."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python version: $PYTHON_VERSION"

# Create virtual environment if it doesn't exist
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv "$SCRIPT_DIR/venv"
fi

# Activate virtual environment
echo "📦 Activating virtual environment..."
source "$SCRIPT_DIR/venv/bin/activate"

# Install dependencies. Read from requirements-build.txt, which includes
# requirements.txt, rather than repeating the list here: this script and the
# Makefile drifted apart once already.
echo "📦 Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r "$SCRIPT_DIR/requirements-build.txt" --quiet

# Check if the Python script exists
if [ ! -f "$SCRIPT_DIR/$PYTHON_FILE" ]; then
    echo "❌ $PYTHON_FILE not found in $SCRIPT_DIR"
    exit 1
fi

cd "$SCRIPT_DIR"

# Run the tests before building. `make build` has always done this; this
# script did not, so the no-make path could ship a binary nothing had run.
# Scoped to test_npm_api.py for the same reason the Makefile is: a QA sweep
# leaves deliberately-red scratch suites beside the code.
echo "🧪 Running tests..."
python3 -m unittest discover -b -p 'test_npm_api.py'

# Build the binary
echo "🔨 Building binary..."

pyinstaller \
    --onefile \
    --name "$SCRIPT_NAME" \
    --clean \
    --noconfirm \
    --console \
    --strip \
    --exclude-module tkinter \
    --exclude-module matplotlib \
    --exclude-module numpy \
    --exclude-module scipy \
    --exclude-module PIL \
    --exclude-module cv2 \
    "$PYTHON_FILE"

# Check if build was successful
if [ -f "$SCRIPT_DIR/dist/$SCRIPT_NAME" ]; then
    echo ""
    echo "=========================================="
    echo "  ✅ Build Successful!"
    echo "=========================================="
    echo ""
    echo "Binary location: $SCRIPT_DIR/dist/$SCRIPT_NAME"
    echo "Binary size: $(du -h "$SCRIPT_DIR/dist/$SCRIPT_NAME" | cut -f1)"
    echo ""
    echo "To install system-wide, run:"
    echo "  sudo cp $SCRIPT_DIR/dist/$SCRIPT_NAME /usr/local/bin/"
    echo ""
    echo "Then you can use it directly:"
    echo "  npm-api --help"
    echo ""
else
    echo "❌ Build failed!"
    exit 1
fi

# Deactivate virtual environment
deactivate

echo "Done! 🎉"
