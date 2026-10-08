#!/usr/bin/env bash
# Install native tools outside the shareable source directory.
set -euo pipefail
umask 077
cd "${1:?Usage: bootstrap.sh PROJECT_ROOT}"

UV_VERSION=0.12.23
YC_VERSION=1.40.0
PYTHON_VERSION=$(cat .python-version)

case "$(uname -s)" in
    Darwin) system=darwin; uv_system=apple-darwin ;;
    Linux) system=linux; uv_system=unknown-linux-musl ;;
    *) echo "Use bootstrap.ps1 on Windows." >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64|amd64) architecture=amd64; uv_arch=x86_64 ;;
    arm64|aarch64) architecture=arm64; uv_arch=aarch64 ;;
    *) echo "Supported architectures: x86-64 and ARM64." >&2; exit 1 ;;
esac

project_name="${PWD##*/}"
runtime_dir="${DATALENS_RUNTIME_DIR:-$PWD/../.local/${project_name// /}}"
mkdir -p "$runtime_dir"
runtime_dir=$(cd "$runtime_dir" && pwd -P)
case "$runtime_dir/" in
    "$PWD/"*) echo "DATALENS_RUNTIME_DIR must be outside the project." >&2; exit 1 ;;
esac
export DATALENS_RUNTIME_DIR="$runtime_dir"
export PYTHONDONTWRITEBYTECODE=1
unset VIRTUAL_ENV
tool_dir="$runtime_dir/.tools/$system-$architecture"
mkdir -p "$tool_dir/bin" "$runtime_dir/.cache/uv"
stage=$(mktemp -d "${TMPDIR:-/tmp}/datalens-bootstrap.XXXXXX")
trap 'rm -rf "$stage"' EXIT

download() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 300 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --timeout=300 --tries=3 "$1" -O "$2"
    else
        echo "Install curl or wget, then rerun bootstrap." >&2
        exit 1
    fi
}

uv="$tool_dir/bin/uv"
if [ ! -x "$uv" ] || ! "$uv" --version | grep -q "^uv $UV_VERSION "; then
    archive="uv-$uv_arch-$uv_system"
    download "https://releases.astral.sh/github/uv/releases/download/$UV_VERSION/$archive.tar.gz" "$stage/uv.tar.gz"
    tar -xzf "$stage/uv.tar.gz" -C "$stage"
    "$stage/$archive/uv" --version
    cp "$stage/$archive/uv" "$uv"
    chmod 700 "$uv"
fi

yc="$tool_dir/bin/yc"
if [ ! -x "$yc" ] || ! "$yc" version | grep -q "CLI $YC_VERSION "; then
    download "https://storage.yandexcloud.net/yandexcloud-yc/release/$YC_VERSION/$system/$architecture/yc" "$stage/yc"
    chmod 700 "$stage/yc"
    "$stage/yc" version
    cp "$stage/yc" "$yc"
fi

# These locations are computed from the current directory, never stored as settings.
export UV_CACHE_DIR="$runtime_dir/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$tool_dir/python"
export UV_PROJECT_ENVIRONMENT="$runtime_dir/.venv"
export UV_PYTHON_INSTALL_BIN=0
export UV_PYTHON_NO_REGISTRY=1
"$uv" --no-config python install "$PYTHON_VERSION"
# .venv contains generated dependencies only. Rebuilding repairs stale shebangs
# and interpreter links when the project moves or changes operating systems.
"$uv" --no-config venv --clear --relocatable --managed-python --python "$PYTHON_VERSION" "$runtime_dir/.venv"
"$uv" --no-config sync --frozen --managed-python --python "$PYTHON_VERSION"
"$uv" --no-config pip check --python "$runtime_dir/.venv/bin/python"
"$runtime_dir/.venv/bin/python" -B -c 'import importlib, os; importlib.import_module(os.environ.get("DATALENS_BOOTSTRAP_MODULE", "analytics_toolkit.datalens_utils.bootstrap")).check_environment(os.getcwd(), os.environ["DATALENS_RUNTIME_DIR"])'

echo "Setup complete. If this machine is not signed in, run:"
"$runtime_dir/.venv/bin/python" -B -c 'import importlib, os, shlex; setup = importlib.import_module(os.environ.get("DATALENS_BOOTSTRAP_MODULE", "analytics_toolkit.datalens_utils.bootstrap")); print(shlex.join([str(setup.managed_yc_binary(os.environ["DATALENS_RUNTIME_DIR"])), "init", "--profile", os.environ.get("DATALENS_YC_PROFILE") or "YOUR_PROFILE"]))'
echo "Then: python3 dashboard.py"
