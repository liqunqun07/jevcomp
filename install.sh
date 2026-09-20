#!/usr/bin/env bash
# jevcomp one-command installer.
#
#   bash install.sh            # everything: CLI + skill + Claude Code plugin
#   bash install.sh --no-pip   # skip pip install (use python3 -m jevcomp instead)
#   bash install.sh --cli-only # CLI + skill only, no Claude Code plugin
#
# Requires: python3 >= 3.9, git clone of this repo. A TypeSafe API key is
# prompted for (or reuse TYPESAFE_API_KEY / ~/.jevcomp.json if present).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NO_PIP=0; CLI_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-pip) NO_PIP=1 ;;
    --cli-only) CLI_ONLY=1 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  # pick the newest python3 >= 3.9; skip pyenv shims that resolve too old
  for cand in /usr/bin/python3 python3.13 python3.12 python3.11 python3.10 python3; do
    p="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$p" ] || continue
    if [ "$("$p" -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')" = "1" ]; then
      PYTHON="$p"; break
    fi
  done
fi
[ -n "$PYTHON" ] || { echo "no python3 >= 3.9 found (set PYTHON=/path/to/python3)"; exit 1; }
echo "    using $($PYTHON --version) at $PYTHON"

echo "==> [1/4] API key"
if [ -f "$HOME/.jevcomp.json" ] || [ -n "${TYPESAFE_API_KEY:-}" ]; then
  echo "    already configured (~/.jevcomp.json or TYPESAFE_API_KEY)"
else
  printf "    TypeSafe API key: "
  read -r KEY
  [ -n "$KEY" ] || { echo "empty key"; exit 1; }
  printf '{"api_key": "%s"}\n' "$KEY" > "$HOME/.jevcomp.json"
  chmod 600 "$HOME/.jevcomp.json"
  echo "    wrote ~/.jevcomp.json (600)"
fi

echo "==> [2/4] CLI"
if [ "$NO_PIP" = "0" ] && "$PYTHON" -m pip --version >/dev/null 2>&1; then
  "$PYTHON" -m pip install --quiet "$HERE" && echo "    pip install ok -> command: jevcomp"
else
  chmod +x "$HERE/bin/jevcomp"
  echo "    skipped pip; run CLI via: PYTHONPATH=$HERE python3 -m jevcomp"
fi

echo "==> [3/5] skills (all agents that read ~/.agents/skills or ~/.claude/skills)"
mkdir -p "$HOME/.agents/skills"
rm -rf "$HOME/.agents/skills/jev-compaction"
cp -R "$HERE/skill" "$HOME/.agents/skills/jev-compaction"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$HOME/.agents/skills/jev-compaction" "$HOME/.claude/skills/jev-compaction"
echo "    installed to ~/.agents/skills/jev-compaction (+ symlink for Claude Code)"

echo "==> [4/5] MCP server registration (works in zcode, Codex, Claude Code, ...)"
HERE_JSON=$(printf '%s' "$HERE" | sed 's/"/\\"/g')
"$PYTHON" - "$HERE_JSON" <<'PYEOF'
import json, os, sys
here = sys.argv[1]
server = {
    "type": "stdio",
    "command": "/usr/bin/python3",  # launchd-safe absolute path
    "args": ["-m", "jevcomp", "mcp"],
    "env": {"PYTHONPATH": here},
}
# zcode (user config, strict schema)
zpath = os.path.expanduser("~/.zcode/cli/config.json")
if os.path.isfile(zpath):
    d = json.load(open(zpath))
    d.setdefault("mcp", {}).setdefault("servers", {})["jevcomp"] = server
    json.dump(d, open(zpath, "w"), ensure_ascii=False, indent=2)
    print("    zcode: registered in ~/.zcode/cli/config.json (mcp.servers)")
# codex (config.toml)
cpath = os.path.expanduser("~/.codex/config.toml")
if os.path.isfile(cpath):
    content = open(cpath).read()
    if "mcp_servers.jevcomp" not in content:
        with open(cpath, "a") as fh:
            fh.write(
                '\n[mcp_servers.jevcomp]\ncommand = "/usr/bin/python3"\n'
                f'args = ["-m", "jevcomp", "mcp"]\nenv = {{ "PYTHONPATH" = "{here}" }}\n'
            )
        print("    codex: registered in ~/.codex/config.toml ([mcp_servers.jevcomp])")
    else:
        print("    codex: already registered")
PYEOF
# claude (claude mcp add, user scope)
CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
if [ -x "$CLAUDE_BIN" ]; then
  if "$CLAUDE_BIN" mcp list 2>/dev/null | grep -q jevcomp; then
    echo "    claude: already registered (claude mcp list)"
  elif "$CLAUDE_BIN" mcp add jevcomp --scope user -- /usr/bin/python3 -m jevcomp mcp >/dev/null 2>&1; then
    echo "    claude: registered via claude mcp add (user scope)"
  else
    echo "    claude: mcp add failed (register manually: claude mcp add jevcomp --scope user)"
  fi
fi

echo "==> [5/5] scheduled autopilot (LaunchAgent, daily 03:00 & 15:00)"
PLIST_DST="$HOME/Library/LaunchAgents/com.jevcomp.autopilot.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__JEVCOMP_HOME__#$HERE#g" "$HERE/launchd/com.jevcomp.autopilot.plist" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST" && echo "    loaded com.jevcomp.autopilot"

if [ "$CLI_ONLY" = "0" ]; then
  echo "==> [4/4] Claude Code plugin"
  CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
  if [ ! -x "$CLAUDE_BIN" ]; then
    echo "    claude not found; skipped (plugin folder remains usable via --plugin-dir)"
  else
    mkdir -p "$HOME/.claude/settings.json.jevcomp-backup"
    cp "$HOME/.claude/settings.json" "$HOME/.claude/settings.json.jevcomp-backup/settings.json.$(date +%Y%m%d-%H%M%S).bak" 2>/dev/null || true
    (cd "$HERE" && "$CLAUDE_BIN" plugin marketplace add "$HERE" && "$CLAUDE_BIN" plugin install jevcomp@jevcomp)
    "$PYTHON" - <<'PYEOF'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
d = json.load(open(p))
env = d.setdefault("env", {})
env.setdefault("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", "1")
if os.path.isfile(os.path.expanduser("~/.jevcomp.json")):
    env.setdefault("TYPESAFE_API_KEY", json.load(open(os.path.expanduser("~/.jevcomp.json")))["api_key"])
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print("    settings.json: CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 (+ TYPESAFE_API_KEY if new)")
PYEOF
    echo "    installed. Restart Claude Code (2.1.274+) -> /compact and auto-compaction go through Jev."
  fi
fi

echo "==> done. Try:  jevcomp zcode --list    |    jevcomp claude <session.jsonl>"
