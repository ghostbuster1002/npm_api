# Makefile for npm-api CLI tool

SCRIPT_NAME = npm-api
PYTHON_FILE = npm_api.py
INSTALL_DIR = /usr/local/bin

# The one suite that must always pass. Discovery is pinned to it by name
# rather than left to unittest's default test*.py glob: a QA sweep leaves
# scratch suites (test_qa_*.py) beside the code carrying one deliberately
# failing test per finding that is not fixed yet. .gitignore hides those from
# git, but unittest discovers over the filesystem and has never heard of
# .gitignore, so the default glob loads them and no build can ever go green
# while a sweep is open.
TEST_FILE = test_npm_api.py

.PHONY: all build install uninstall clean venv deps help test

all: build

help:
	@echo "NPM-API CLI Build System"
	@echo ""
	@echo "Usage:"
	@echo "  make test      - Run the test suite (stdlib unittest, no pytest)"
	@echo "  make build     - Install deps, run tests, then build the binary"
	@echo "  make install   - Install to $(INSTALL_DIR) (requires sudo)"
	@echo "  make uninstall - Remove from $(INSTALL_DIR) (requires sudo)"
	@echo "  make clean     - Remove build artifacts"
	@echo "  make venv      - Create virtual environment"
	@echo "  make deps      - Install dependencies"
	@echo ""

# Standard-library unittest only, so the suite runs with nothing installed
# beyond what npm_api.py itself needs. Importing npm_api pulls in requests,
# typer and rich, which may only be present in ./venv, so prefer the venv
# interpreter when it exists and fall back to whatever python3 is on PATH.
# The tests need no network and no live NPM. -b buffers the progress output
# npm_api prints as it works, so it only surfaces for a test that fails.
test:
	@echo "Running tests..."
	@if [ -x ./venv/bin/python3 ]; then PY=./venv/bin/python3; else PY=python3; fi; \
		$$PY -m unittest discover -v -b -p '$(TEST_FILE)'

venv:
	@echo "Creating virtual environment..."
	python3 -m venv venv
	@echo "Done! Activate with: source venv/bin/activate"

# requirements-build.txt pulls in requirements.txt, so this one line installs
# the runtime deps and PyInstaller together. The list used to be spelled out
# here as well as in build.sh, build.yml and requirements.txt — four copies,
# of which only requirements.txt carried versions, and none of which agreed
# after typer dropped its `all` extra.
deps: venv
	@echo "Installing dependencies..."
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements-build.txt
	@echo "Done!"

# deps first: the tests import npm_api, which needs requests/typer/rich, and
# the `test` target prefers ./venv/bin/python3 once the venv exists.
build: deps test
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
