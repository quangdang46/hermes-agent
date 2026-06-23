#!/bin/bash
# ============================================================================
# Hermes Agent Local Installer
# ============================================================================
# Install Hermes Agent from a local checkout (no need to clone from origin).
# Designed for personal forks — avoids fixing URLs or depending on origin.
#
# Usage:
#   cd ~/Projects/hermes-agent
#   bash scripts/install-local.sh
#
# Or from anywhere:
#   bash /path/to/hermes-agent/scripts/install-local.sh
#
# Options:
#   --dir PATH       Installation directory (default: $HERMES_HOME/hermes-agent)
#   --no-venv        Don't create virtual environment
#   --skip-setup     Skip interactive setup wizard
#   --venv-only      Only create venv + install deps (skip everything else)
#   -h, --help       Show this help
# ============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
USE_VENV=true
RUN_SETUP=true
VENV_ONLY=false
NON_INTERACTIVE=false

# ── Detect SOURCE directory (where this script lives) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"  # repo root

# ── Options ──
while [[ $# -gt 0 ]]; do
    case $1 in
        --dir)
            INSTALL_DIR="$2"
            shift 2
            ;;
        --no-venv)
            USE_VENV=false
            shift
            ;;
        --skip-setup)
            RUN_SETUP=false
            shift
            ;;
        --venv-only)
            VENV_ONLY=true
            shift
            ;;
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        -h|--help)
            echo "Hermes Agent Local Installer"
            echo ""
            echo "Install Hermes from local checkout: $SOURCE_DIR"
            echo ""
            echo "Usage: bash $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --dir PATH       Installation directory"
            echo "                   (default: \$HERMES_HOME/hermes-agent)"
            echo "  --no-venv        Don't create virtual environment"
            echo "  --skip-setup     Skip interactive setup wizard"
            echo "  --venv-only      Only create venv + install deps"
            echo "  --non-interactive  Skip prompts"
            echo "  -h, --help       Show this help"
            echo ""
            echo "Environment:"
            echo "  HERMES_HOME  Data directory (default: ~/.hermes)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ── Detect OS ──
case "$(uname -s)" in
    Linux*) OS="linux" ;;
    Darwin*) OS="macos" ;;
    *) OS="unknown" ;;
esac

# ── Resolve install layout ──
if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR="$HERMES_HOME/hermes-agent"
fi
INSTALL_DIR="$( (realpath "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR") )"

# ── Print banner ──
echo ""
echo -e "${BLUE}${BOLD}"
echo "┌─────────────────────────────────────────────────────────┐"
echo "│         ⚕  Hermes Agent — Local Install                 │"
echo "├─────────────────────────────────────────────────────────┤"
echo -e "│  Source: ${CYAN}$SOURCE_DIR${BLUE}"
echo -e "│  Target: ${CYAN}$INSTALL_DIR${BLUE}"
echo -e "│  Data:   ${CYAN}$HERMES_HOME${BLUE}"
echo "└─────────────────────────────────────────────────────────┘"
echo -e "${NC}"

# ── Step 1: Verify source ──
echo -e "\n${CYAN}→${NC} Verifying source checkout..."
if [ ! -f "$SOURCE_DIR/pyproject.toml" ]; then
    echo -e "${RED}✗${NC} No pyproject.toml found at $SOURCE_DIR"
    echo "  Are you in the hermes-agent repo root?"
    exit 1
fi
if [ ! -d "$SOURCE_DIR/.git" ]; then
    echo -e "${YELLOW}⚠${NC} No .git directory — not a git checkout (ok, but updates won't work)"
fi
echo -e "${GREEN}✓${NC} Source: $SOURCE_DIR"

# ── Step 2: Check Python ──
echo -e "\n${CYAN}→${NC} Checking Python..."

# Prefer uv for Python management
UV_CMD=""
if command -v uv >/dev/null 2>&1; then
    UV_CMD="$(command -v uv)"
    echo -e "${GREEN}✓${NC} uv found: $(uv --version)"
else
    echo -e "${YELLOW}⚠${NC} uv not found — will try system Python"
fi

PYTHON_MIN="3.11"
PYTHON_PATH=""
if [ -n "$UV_CMD" ]; then
    if PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_MIN" 2>/dev/null)"; then
        echo -e "${GREEN}✓${NC} Python $("$PYTHON_PATH" --version) via uv"
    else
        echo -e "${YELLOW}⚠${NC} Python $PYTHON_MIN not found, installing via uv..."
        "$UV_CMD" python install "$PYTHON_MIN"
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_MIN")"
        echo -e "${GREEN}✓${NC} Python $("$PYTHON_PATH" --version) installed"
    fi
else
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_PATH="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_PATH="$(command -v python)"
    fi
    if [ -n "$PYTHON_PATH" ]; then
        echo -e "${GREEN}✓${NC} Python: $("$PYTHON_PATH" --version)"
    else
        echo -e "${RED}✗${NC} Python not found. Install Python $PYTHON_MIN+ first."
        exit 1
    fi
fi

# ── Step 3: Link source to INSTALL_DIR ──
if [ "$VENV_ONLY" = false ]; then
    echo -e "\n${CYAN}→${NC} Setting up installation at $INSTALL_DIR..."

    # Create HERMES_HOME structure
    mkdir -p "$HERMES_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    # Symlink the repo (zero copy, update-in-place)
    if [ ! -e "$INSTALL_DIR" ]; then
        ln -sf "$SOURCE_DIR" "$INSTALL_DIR"
        echo -e "${GREEN}✓${NC} Symlinked: $INSTALL_DIR → $SOURCE_DIR"
    else
        CURRENT_TARGET="$(readlink "$INSTALL_DIR" 2>/dev/null || echo "")"
        if [ "$CURRENT_TARGET" = "$SOURCE_DIR" ]; then
            echo -e "${GREEN}✓${NC} Already linked: $INSTALL_DIR → $SOURCE_DIR"
        elif [ -d "$INSTALL_DIR/.git" ]; then
            echo -e "${YELLOW}⚠${NC} $INSTALL_DIR already exists as git checkout (keeping it)"
        elif [ -f "$INSTALL_DIR/pyproject.toml" ]; then
            echo -e "${YELLOW}⚠${NC} $INSTALL_DIR already exists (keeping it)"
        else
            echo -e "${YELLOW}⚠${NC} $INSTALL_DIR exists but not a repo — overwriting symlink"
            rm -rf "$INSTALL_DIR"
            ln -sf "$SOURCE_DIR" "$INSTALL_DIR"
        fi
    fi
fi

# ── Step 4: Create venv + install deps ──
cd "$SOURCE_DIR"

if [ "$USE_VENV" = true ]; then
    echo -e "\n${CYAN}→${NC} Creating virtual environment..."

    if [ -d "venv" ]; then
        echo -e "${YELLOW}⚠${NC} venv already exists — recreating..."
        rm -rf venv
    fi

    if [ -n "$UV_CMD" ]; then
        "$UV_CMD" venv venv --python "$PYTHON_MIN"
        export UV_PYTHON="$SOURCE_DIR/venv/bin/python"
    else
        "$PYTHON_PATH" -m venv venv
    fi

    VENV_PYTHON="$SOURCE_DIR/venv/bin/python"
    echo -e "${GREEN}✓${NC} venv created: $("$VENV_PYTHON" --version)"
else
    echo -e "${YELLOW}⚠${NC} Skipping venv (--no-venv)"
    VENV_PYTHON="$PYTHON_PATH"
fi

echo -e "\n${CYAN}→${NC} Installing Python dependencies..."

if [ -n "$UV_CMD" ] && [ "$USE_VENV" = true ]; then
    export VIRTUAL_ENV="$SOURCE_DIR/venv"
    # Try hash-verified sync first, fall back to pip install
    if [ -f "uv.lock" ]; then
        if UV_PROJECT_ENVIRONMENT="$SOURCE_DIR/venv" "$UV_CMD" sync --extra all --locked 2>/dev/null; then
            echo -e "${GREEN}✓${NC} Dependencies installed (hash-verified via uv.lock)"
        else
            echo -e "${YELLOW}⚠${NC} uv.lock sync failed — falling back to PyPI resolve..."
            "$UV_CMD" pip install -e ".[all]" 2>/dev/null || "$UV_CMD" pip install -e .
        fi
    else
        "$UV_CMD" pip install -e ".[all]" 2>/dev/null || "$UV_CMD" pip install -e .
    fi
else
    "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
    "$VENV_PYTHON" -m pip install -e ".[all]" 2>/dev/null || "$VENV_PYTHON" -m pip install -e .
fi

echo -e "${GREEN}✓${NC} Dependencies installed"

# ── Step 5: Install hermes command ──
if [ "$VENV_ONLY" = false ]; then
    echo -e "\n${CYAN}→${NC} Installing hermes command..."

    # Determine command directory
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        CMD_DIR="/usr/local/bin"
    elif [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ]; then  # Termux
        CMD_DIR="$PREFIX/bin"
    else
        CMD_DIR="$HOME/.local/bin"
    fi

    mkdir -p "$CMD_DIR"

    if [ "$USE_VENV" = true ]; then
        HERMES_BIN="$SOURCE_DIR/venv/bin/hermes"
    else
        HERMES_BIN="$(command -v hermes 2>/dev/null || echo "")"
        if [ -z "$HERMES_BIN" ]; then
            echo -e "${YELLOW}⚠${NC} hermes entry point not found"
            echo -e "  Try: cd $SOURCE_DIR && python -m pip install -e ."
        fi
    fi

    if [ -x "$HERMES_BIN" ]; then
        rm -f "$CMD_DIR/hermes"
        cat > "$CMD_DIR/hermes" <<HERMES_SHIM
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" "\$@"
HERMES_SHIM
        chmod +x "$CMD_DIR/hermes"
        echo -e "${GREEN}✓${NC} Installed hermes → $CMD_DIR/hermes"
    fi

    # ── Step 6: Config templates ──
    echo -e "\n${CYAN}→${NC} Setting up configuration files..."

    if [ ! -f "$HERMES_HOME/.env" ]; then
        if [ -f "$SOURCE_DIR/.env.example" ]; then
            cp "$SOURCE_DIR/.env.example" "$HERMES_HOME/.env"
            chmod 600 "$HERMES_HOME/.env"
            echo -e "${GREEN}✓${NC} Created $HERMES_HOME/.env from template"
        else
            touch "$HERMES_HOME/.env"
            echo -e "${GREEN}✓${NC} Created $HERMES_HOME/.env"
        fi
    else
        echo -e "${YELLOW}⚠${NC} $HERMES_HOME/.env already exists (keeping)"
    fi

    if [ ! -f "$HERMES_HOME/config.yaml" ]; then
        if [ -f "$SOURCE_DIR/cli-config.yaml.example" ]; then
            cp "$SOURCE_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
            echo -e "${GREEN}✓${NC} Created $HERMES_HOME/config.yaml from template"
        fi
    else
        echo -e "${YELLOW}⚠${NC} $HERMES_HOME/config.yaml already exists (keeping)"
    fi

    # Ensure PATH includes CMD_DIR
    case ":$PATH:" in
        *":$CMD_DIR:"*) ;;
        *)
            export PATH="$CMD_DIR:$PATH"
            echo -e "${YELLOW}⚠${NC} $CMD_DIR not in PATH — added for current session"
            ;;
    esac
fi

# ── Step 7: Run setup wizard (optional) ──
if [ "$RUN_SETUP" = true ] && [ "$VENV_ONLY" = false ]; then
    if command -v hermes >/dev/null 2>&1; then
        echo -e "\n${CYAN}→${NC} Running setup wizard..."
        hermes setup
    else
        echo -e "${YELLOW}⚠${NC} 'hermes' not on PATH — run setup manually:"
        echo "  $CMD_DIR/hermes setup"
    fi
fi

# ── Done ──
echo ""
echo -e "${GREEN}${BOLD}✓ Hermes Agent installed from local checkout${NC}"
echo ""
echo "  Source: $SOURCE_DIR"
echo "  Config: $HERMES_HOME/"
echo "  Command: $(command -v hermes 2>/dev/null || echo "$CMD_DIR/hermes")"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo "  1. Edit .env:             $HERMES_HOME/.env"
echo "  2. Edit config:           $HERMES_HOME/config.yaml"
echo "  3. Run hermes:            hermes"
echo "  4. Update (git pull):     cd $SOURCE_DIR && git pull"
echo "     (no reinstall needed after pull)"
echo ""
echo -e "${YELLOW}Note:${NC} Because of the symlink, code in $SOURCE_DIR is used directly."
echo "  A simple git pull is enough — no need to re-run this script."
echo ""
