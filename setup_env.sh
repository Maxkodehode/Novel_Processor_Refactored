#!/bin/bash
# Novel_Processor Environment Setup Script
# Run this once to build the persistent Docker image with all dependencies
#
# Usage: bash setup_env.sh [--rebuild]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="novel-processor-env"
CONTAINER_NAME="novel-processor"
DOCKERFILE="${SCRIPT_DIR}/Dockerfile"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"

# Parse args
REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=true ;;
        --help|-h)
            echo "Usage: bash setup_env.sh [--rebuild]"
            echo "  --rebuild  Force rebuild of the Docker image"
            exit 0
            ;;
    esac
done

echo "=== Novel_Processor Environment Setup ==="
echo ""

# Check if Docker is available
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker is not installed or not in PATH"
    exit 1
fi

# Check if requirements.txt exists
if [ ! -f "$REQUIREMENTS" ]; then
    echo "ERROR: requirements.txt not found at ${REQUIREMENTS}"
    exit 1
fi

# Check if image already exists
IMAGE_EXISTS=$(docker images -q "$IMAGE_NAME" 2>/dev/null || true)

if [ -n "$IMAGE_EXISTS" ] && [ "$REBUILD" = false ]; then
    echo "Image '${IMAGE_NAME}' already exists. Use --rebuild to recreate."
    echo "Image ID: ${IMAGE_EXISTS}"
else
    if [ -n "$IMAGE_EXISTS" ]; then
        echo "Removing old image..."
        docker rmi "$IMAGE_NAME" 2>/dev/null || true
    fi

    echo "Building Docker image '${IMAGE_NAME}'..."
    echo "  Dockerfile: ${DOCKERFILE}"
    echo "  Requirements: ${REQUIREMENTS}"
    echo ""

    docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

    echo ""
    echo "=== Build complete ==="
fi

# Verify the image
echo ""
echo "Verifying installation..."
docker run --rm "$IMAGE_NAME" python3 -c "
import sys
deps = [
    'playwright', 'bs4', 'requests', 'httpx', 'curl_cffi',
    'lxml', 'EbookLib', 'fastapi', 'uvicorn', 'rapidfuzz',
    'autoscraper', 'playwright_stealth'
]
failed = []
for dep in deps:
    try:
        __import__(dep)
        print(f'  OK: {dep}')
    except ImportError:
        failed.append(dep)
        print(f'  MISSING: {dep}')
if failed:
    print(f'\n{len(failed)} package(s) missing!')
    sys.exit(1)
else:
    print(f'\nAll {len(deps)} packages verified!')
"

echo ""
echo "=== Setup complete ==="
echo ""
echo "To use this image with Hermes, update config.yaml:"
echo "  terminal:"
echo "    docker_image: ${IMAGE_NAME}"
echo ""
echo "Or run manually:"
echo "  docker run -it --rm -v \$(pwd):/workspace ${IMAGE_NAME} bash"
