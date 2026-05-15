#!/bin/bash
# Novel_Processor Quick Install
# Run this inside any terminal session to ensure all deps are present.
# Safe to run multiple times - skips already-installed packages.
#
# Usage: source quick_install.sh
#   or:  bash quick_install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
MARKER_FILE="${SCRIPT_DIR}/.deps_installed"

echo "=== Novel_Processor Quick Install ==="

# Check if already installed in this environment
if [ -f "$MARKER_FILE" ]; then
    INSTALLED_AT=$(cat "$MARKER_FILE" 2>/dev/null || echo "unknown")
    echo "Dependencies were already installed at: ${INSTALLED_AT}"
    echo "To force reinstall, remove ${MARKER_FILE} and run again."
    echo ""
else
    if [ ! -f "$REQUIREMENTS" ]; then
        echo "ERROR: requirements.txt not found at ${REQUIREMENTS}"
        return 1 2>/dev/null || exit 1
    fi

    echo "Installing Python packages from requirements.txt..."
    pip install -r "$REQUIREMENTS"
    echo ""

    # Check if Playwright system deps are available
    echo "Checking Playwright system dependencies..."
    MISSING_LIBS=()
    for lib in libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
               libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
               libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0; do
        if ! dpkg -l "$lib" 2>/dev/null | grep -q "^ii"; then
            MISSING_LIBS+=("$lib")
        fi
    done

    if [ ${#MISSING_LIBS[@]} -gt 0 ]; then
        echo "WARNING: Missing system libraries (Playwright may not work):"
        printf '  - %s\n' "${MISSING_LIBS[@]}"
        echo "Install with: sudo apt-get install -y ${MISSING_LIBS[*]}"
    else
        echo "All system dependencies present."
    fi

    # Install Playwright browsers if playwright is available
    if command -v playwright &>/dev/null; then
        echo ""
        echo "Installing Playwright Chromium browser..."
        playwright install chromium 2>/dev/null || echo "WARNING: Playwright browser install failed (may need system deps)"
    fi

    # Mark as installed
    date '+%Y-%m-%d %H:%M:%S' > "$MARKER_FILE"
    echo ""
    echo "=== Installation complete ==="
fi

# Verify
echo ""
echo "Verifying key packages..."
python3 -c "
import sys
deps = ['playwright', 'bs4', 'requests', 'curl_cffi', 'lxml', 'ebooklib', 'fastapi', 'rapidfuzz']
ok = 0
fail = 0
for d in deps:
    try:
        __import__(d)
        ok += 1
    except ImportError:
        print(f'  MISSING: {d}')
        fail += 1
print(f'{ok}/{ok+fail} packages available')
sys.exit(1 if fail else 0)
" || true
