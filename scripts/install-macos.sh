#!/usr/bin/env bash
# One-shot installer for the residence-search Telegram bot on macOS.
# Uses launchd (macOS native) so the bot survives reboots and crashes.
#
# Usage (from any terminal on the Mac):
#   TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy \
#     bash <(curl -fsSL https://raw.githubusercontent.com/charleskot/sell_analysis/main/scripts/install-macos.sh)

set -euo pipefail

REPO_URL="https://github.com/charleskot/sell_analysis.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/sell_analysis}"
BRANCH="${BRANCH:-main}"
LABEL="com.charleskot.pisos-bot"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Installing Pisos bot at $INSTALL_DIR"

# ── 1. Check prerequisites ───────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ python3 not found. Install with: brew install python@3.11"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "==> Python version: $PY_VERSION"

# Numeric version check (bash lex-compare would call 3.9 >= 3.10)
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    echo "❌ Need Python 3.10+. Installing python@3.11 via Homebrew…"
    if ! command -v brew >/dev/null 2>&1; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || eval "$(/usr/local/bin/brew shellenv)" 2>/dev/null
    fi
    brew install python@3.11
    # Use the new python3.11 for the venv
    PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
else
    PYTHON_BIN="$(command -v python3)"
fi

if ! command -v git >/dev/null 2>&1; then
    echo "❌ git not found. Install Xcode Command Line Tools: xcode-select --install"
    exit 1
fi

# ── 2. Clone or update repo ──────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> Updating existing checkout"
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    echo "==> Cloning $REPO_URL"
    git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── 3. Python venv + deps ────────────────────────────────────────────────
if [ ! -d "$INSTALL_DIR/venv" ]; then
    echo "==> Creating venv with $PYTHON_BIN"
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── 4. .env (env vars for the bot) ───────────────────────────────────────
if [ -z "${TELEGRAM_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo ""
    echo "⚠️  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID env vars not set."
    echo "    Re-run this script with them defined, e.g.:"
    echo "    TELEGRAM_TOKEN=xxx TELEGRAM_CHAT_ID=yyy bash <(...)"
    exit 1
fi

cat > "$INSTALL_DIR/.env" <<EOF
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
SCRAPER_PROXY=${SCRAPER_PROXY:-}
EOF
chmod 600 "$INSTALL_DIR/.env"

mkdir -p "$INSTALL_DIR/data"

# ── 5. launchd plist (auto-start, auto-restart on crash) ─────────────────
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/venv/bin/python</string>
        <string>$INSTALL_DIR/main.py</string>
        <string>scrape</string>
    </array>
    <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$INSTALL_DIR/data/bot.log</string>
    <key>StandardErrorPath</key><string>$INSTALL_DIR/data/bot.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TELEGRAM_TOKEN</key><string>$TELEGRAM_TOKEN</string>
        <key>TELEGRAM_CHAT_ID</key><string>$TELEGRAM_CHAT_ID</string>
        <key>SCRAPER_PROXY</key><string>${SCRAPER_PROXY:-}</string>
        <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

# ── 6. (Re)load launchd job ─────────────────────────────────────────────
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

sleep 2

echo ""
echo "==> Status:"
if launchctl list | grep -q "$LABEL"; then
    echo "   ✅ launchd job loaded: $LABEL"
else
    echo "   ❌ launchd job NOT loaded — check $INSTALL_DIR/data/bot.err.log"
fi

echo ""
echo "==> Last log lines:"
sleep 3
tail -30 "$INSTALL_DIR/data/bot.log" 2>/dev/null || echo "(no logs yet — bot arrancando…)"

echo ""
echo "==> Done."
echo "   Logs:      tail -f $INSTALL_DIR/data/bot.log"
echo "   Errors:    tail -f $INSTALL_DIR/data/bot.err.log"
echo "   Stop:      launchctl unload $PLIST_PATH"
echo "   Restart:   launchctl unload $PLIST_PATH && launchctl load $PLIST_PATH"
