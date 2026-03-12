#!/bin/bash
set -e

# ──────────────────────────────────────────────────────────────────────
# VRCT Linux Setup Script
# Detects distro and installs all system + project dependencies needed
# to build VRCT from source on Linux.
#
# Supported: Debian, Ubuntu, Arch Linux, Fedora
# ──────────────────────────────────────────────────────────────────────

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ── Colors ───────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Distro Detection ────────────────────────────────────────────────

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO_ID="${ID}"
        DISTRO_ID_LIKE="${ID_LIKE:-}"
    else
        err "Cannot detect distro: /etc/os-release not found."
        exit 1
    fi

    # Normalize — Ubuntu and derivatives report ID_LIKE=debian
    case "$DISTRO_ID" in
        ubuntu|debian|linuxmint|pop)
            DISTRO_FAMILY="debian"
            ;;
        arch|manjaro|endeavouros|cachyos|garuda)
            DISTRO_FAMILY="arch"
            ;;
        fedora|nobara)
            DISTRO_FAMILY="fedora"
            ;;
        *)
            # Fall back to ID_LIKE
            case "$DISTRO_ID_LIKE" in
                *debian*|*ubuntu*)  DISTRO_FAMILY="debian" ;;
                *arch*)            DISTRO_FAMILY="arch" ;;
                *fedora*|*rhel*)   DISTRO_FAMILY="fedora" ;;
                *)
                    err "Unsupported distro: $DISTRO_ID (ID_LIKE=$DISTRO_ID_LIKE)"
                    err "Supported: Debian, Ubuntu, Arch Linux, Fedora (and derivatives)"
                    exit 1
                    ;;
            esac
            ;;
    esac

    info "Detected distro: $DISTRO_ID (family: $DISTRO_FAMILY)"
}

# ── System Package Installation ─────────────────────────────────────

install_system_deps() {
    info "Installing system dependencies for $DISTRO_FAMILY..."

    case "$DISTRO_FAMILY" in
        debian)
            sudo apt-get update
            sudo apt-get install -y \
                build-essential \
                pkg-config \
                curl \
                wget \
                git \
                libwebkit2gtk-4.1-dev \
                libgtk-3-dev \
                libayatana-appindicator3-dev \
                librsvg2-dev \
                libsoup-3.0-dev \
                libssl-dev \
                libcairo2-dev \
                libpango1.0-dev \
                libgdk-pixbuf-2.0-dev \
                libatk1.0-dev \
                libfreetype6-dev \
                libfontconfig1-dev \
                libasound2-dev \
                portaudio19-dev \
                python3 \
                python3-venv \
                python3-dev \
                patchelf
            ;;
        arch)
            sudo pacman -Syu --needed --noconfirm \
                base-devel \
                pkgconf \
                curl \
                wget \
                git \
                webkit2gtk-4.1 \
                gtk3 \
                libayatana-appindicator \
                librsvg \
                libsoup3 \
                openssl \
                cairo \
                pango \
                gdk-pixbuf2 \
                atk \
                freetype2 \
                fontconfig \
                alsa-lib \
                portaudio \
                python \
                python-pip \
                patchelf
            # Arch ships the latest Python; check if python3.12 is available
            # for venv compatibility with torch
            if ! command -v python3.12 &>/dev/null; then
                warn "python3.12 not found. Checking if system python works..."
                # On Arch the default python3 may be 3.12+ or 3.13+
                # We'll detect and handle this in the venv step
            fi
            ;;
        fedora)
            sudo dnf install -y \
                @development-tools \
                pkg-config \
                curl \
                wget \
                git \
                webkit2gtk4.1-devel \
                gtk3-devel \
                libappindicator-gtk3-devel \
                librsvg2-devel \
                libsoup3-devel \
                openssl-devel \
                cairo-devel \
                pango-devel \
                gdk-pixbuf2-devel \
                atk-devel \
                freetype-devel \
                fontconfig-devel \
                alsa-lib-devel \
                portaudio-devel \
                python3 \
                python3-devel \
                python3-pip \
                patchelf
            ;;
    esac

    ok "System dependencies installed."
}

# ── Rust Toolchain ──────────────────────────────────────────────────

install_rust() {
    if command -v rustc &>/dev/null; then
        local rust_ver
        rust_ver="$(rustc --version | awk '{print $2}')"
        ok "Rust already installed: $rust_ver"
    else
        info "Installing Rust via rustup..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
        source "$HOME/.cargo/env"
        ok "Rust installed: $(rustc --version | awk '{print $2}')"
    fi
}

# ── Node.js ─────────────────────────────────────────────────────────

install_node() {
    if command -v node &>/dev/null; then
        local node_ver
        node_ver="$(node --version)"
        ok "Node.js already installed: $node_ver"
    else
        info "Node.js not found. Installing via nvm..."
        curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
        export NVM_DIR="$HOME/.nvm"
        [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
        nvm install --lts
        ok "Node.js installed: $(node --version)"
    fi

    if ! command -v npm &>/dev/null; then
        err "npm not found after Node.js install. Check your PATH."
        exit 1
    fi
}

# ── Python venv ─────────────────────────────────────────────────────

find_python() {
    # Prefer 3.12 for best compatibility with torch and all deps.
    # Fall back to 3.11, then 3.13, then generic python3.
    for candidate in python3.12 python3.11 python3.13 python3; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON_BIN="$candidate"
            break
        fi
    done

    if [ -z "${PYTHON_BIN:-}" ]; then
        err "No suitable Python 3 found. Install python3 (3.11-3.13 recommended)."
        exit 1
    fi

    local py_ver
    py_ver="$($PYTHON_BIN --version 2>&1)"
    info "Using Python: $PYTHON_BIN ($py_ver)"

    # Verify version is in acceptable range
    local py_minor
    py_minor="$($PYTHON_BIN -c 'import sys; print(sys.version_info.minor)')"
    if [ "$py_minor" -lt 11 ] || [ "$py_minor" -gt 13 ]; then
        warn "Python 3.$py_minor detected. Versions 3.11-3.13 are recommended."
        warn "torch and other deps may not have wheels for this version."
    fi
}

setup_python_venv() {
    info "Setting up Python virtual environment..."

    cd "$PROJECT_ROOT"

    if [ -d .venv ]; then
        warn "Existing .venv found — removing it."
        rm -rf .venv
    fi

    $PYTHON_BIN -m venv .venv
    source .venv/bin/activate

    python -m pip install --upgrade pip

    info "Installing Python dependencies (this may take a while — torch is large)..."
    pip install --no-cache-dir -r requirements_linux.txt

    ok "Python venv ready."
}

# ── npm dependencies ────────────────────────────────────────────────

install_npm_deps() {
    info "Installing npm dependencies..."
    cd "$PROJECT_ROOT"
    npm install
    ok "npm dependencies installed."
}

# ── Summary ─────────────────────────────────────────────────────────

print_summary() {
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  VRCT Linux setup complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  To build VRCT, run:"
    echo ""
    echo "    bash scripts/build.sh"
    echo ""
    echo "  Or step by step:"
    echo ""
    echo "    bash scripts/build_python.sh   # Build Python sidecar"
    echo "    npx vite build                 # Build frontend"
    echo "    npx tauri build                # Build Tauri app"
    echo ""
    echo "  Output will be in:"
    echo "    src-tauri/target/release/VRCT"
    echo "    src-tauri/target/release/bundle/deb/"
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────

main() {
    echo ""
    echo -e "${CYAN}VRCT Linux Setup${NC}"
    echo -e "${CYAN}═══════════════════════════════════════${NC}"
    echo ""

    detect_distro
    install_system_deps
    install_rust
    install_node
    find_python
    setup_python_venv
    install_npm_deps
    print_summary
}

main "$@"
