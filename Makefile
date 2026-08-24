# Makefile for npm-api CLI tool

SCRIPT_NAME = npm-api
PYTHON_FILE = npm_api.py
INSTALL_DIR = /usr/local/bin

.PHONY: all build install uninstall clean venv deps help test

all: build

help:
	@echo "NPM-API CLI Build System"
	@echo ""
	@echo "Usage:"
	@echo "  make test      - Run the test suite"
	@echo "  make build     - Run tests, then build the standalone binary"
	@echo "  make install   - Install to $(INSTALL_DIR) (requires sudo)"
	@echo "  make uninstall - Remove from $(INSTALL_DIR) (requires sudo)"
	@echo "  make clean     - Remove build artifacts"
	@echo "  make venv      - Create virtual environment"
	@echo "  make deps      - Install dependencies"
	@echo ""

# Runs against whatever python3 is on PATH rather than ./venv, so the suite is
# usable without a build. The tests import npm_api directly and need no network.
test:
	@echo "Running tests..."
	python3 -m pytest -q

venv:
	@echo "Creating virtual environment..."
	python3 -m venv venv
	@echo "Done! Activate with: source venv/bin/activate"

deps: venv
	@echo "Installing dependencies..."
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install requests "typer[all]" rich pyinstaller
	@echo "Done!"

build: test deps
	@echo "Building binary..."
	./venv/bin/pyinstaller \
		--onefile \
		--name $(SCRIPT_NAME) \
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
		$(PYTHON_FILE)
	@echo ""
	@echo "✅ Binary created: dist/$(SCRIPT_NAME)"
	@echo "   Size: $$(du -h dist/$(SCRIPT_NAME) | cut -f1)"

install: dist/$(SCRIPT_NAME)
	@echo "Installing to $(INSTALL_DIR)..."
	sudo cp dist/$(SCRIPT_NAME) $(INSTALL_DIR)/
	sudo chmod +x $(INSTALL_DIR)/$(SCRIPT_NAME)
	@echo "✅ Installed! Run with: $(SCRIPT_NAME) --help"

uninstall:
	@echo "Removing $(INSTALL_DIR)/$(SCRIPT_NAME)..."
	sudo rm -f $(INSTALL_DIR)/$(SCRIPT_NAME)
	@echo "✅ Uninstalled!"

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build/ dist/ __pycache__/ *.spec
	@echo "Done!"

distclean: clean
	@echo "Removing virtual environment..."
	rm -rf venv/
	@echo "Done!"

dist/$(SCRIPT_NAME): $(PYTHON_FILE)
	$(MAKE) build
