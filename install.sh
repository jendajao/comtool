#!/usr/bin/env bash
# comtool 安裝腳本：檢查環境、建立 comd / comsend 指令捷徑、註冊 Claude Code MCP server。
# 可重複執行；--uninstall 只移除指向本 repo 的捷徑與 MCP 註冊，不刪除 repo 與 log/。
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN_DIR=""
DO_MCP=1
UNINSTALL=0
MCP_NAME="comtool"

usage() {
    cat <<EOF
用法: ./install.sh [--bin-dir DIR] [--no-mcp] [--uninstall]

  --bin-dir DIR   comd / comsend 捷徑放置的目錄
                  （預設: root 為 /usr/local/bin，其他使用者為 ~/.local/bin）
  --no-mcp        不註冊（或解除安裝時不移除）Claude Code MCP server
  --uninstall     移除指向本 repo 的捷徑與 MCP 註冊（不刪除 repo 與 log/）
  -h, --help      顯示這個說明
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bin-dir)   [ $# -ge 2 ] || { usage; exit 2; }; BIN_DIR="$2"; shift 2 ;;
        --bin-dir=*) BIN_DIR="${1#*=}"; shift ;;
        --no-mcp)    DO_MCP=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "未知的選項: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$BIN_DIR" ]; then
    if [ "$(id -u)" = 0 ]; then BIN_DIR=/usr/local/bin; else BIN_DIR="$HOME/.local/bin"; fi
fi
BIN_DIR="${BIN_DIR%/}"

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
info() { printf '  - %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
die()  { printf '  \033[31m✘\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
WARNINGS=0

# 找 claude 執行檔：PATH 優先，其次是 VS Code 擴充套件內附的最新版本
find_claude() {
    if command -v claude >/dev/null 2>&1; then
        command -v claude
        return
    fi
    ls -d "$HOME"/.vscode-server*/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null \
        | sort -V | tail -n 1
}

LINKS=("comd:comd.py" "comsend:comsend")

# ------------------------------------------------------------------ 解除安裝
if [ "$UNINSTALL" = 1 ]; then
    step "移除指令捷徑（$BIN_DIR）"
    for pair in "${LINKS[@]}"; do
        name="${pair%%:*}"; target="$REPO/${pair#*:}"; dest="$BIN_DIR/$name"
        if [ -L "$dest" ] && [ "$(readlink -f "$dest")" = "$target" ]; then
            rm -f "$dest" && ok "已移除 $dest"
        elif [ -e "$dest" ] || [ -L "$dest" ]; then
            warn "$dest 不是指向本 repo 的捷徑，略過"
        else
            info "$dest 不存在"
        fi
    done
    if [ "$DO_MCP" = 1 ]; then
        step "移除 MCP server 註冊"
        CLAUDE="$(find_claude)"
        if [ -z "$CLAUDE" ]; then
            warn "找不到 claude 執行檔，請手動執行: claude mcp remove -s user $MCP_NAME"
        elif "$CLAUDE" mcp remove -s user "$MCP_NAME" >/dev/null 2>&1; then
            ok "已移除 MCP server「$MCP_NAME」（重開 Claude Code session 後生效）"
        else
            info "MCP server「$MCP_NAME」未註冊"
        fi
    fi
    step "完成"
    info "repo 與 log/ 沒有刪除：$REPO"
    exit 0
fi

# ------------------------------------------------------------------ 安裝
echo "安裝 comtool：$REPO"

step "1. 檢查環境"
grep -qi microsoft /proc/version 2>/dev/null || die "不是在 WSL 中執行"
ok "WSL"
command -v wslpath >/dev/null 2>&1 || die "找不到 wslpath"
ok "wslpath"
command -v powershell.exe >/dev/null 2>&1 \
    || die "找不到 powershell.exe：請確認 WSL interop 已開啟（/etc/wsl.conf 的 [interop] enabled 與 appendWindowsPath）"
if timeout 60 powershell.exe -NoProfile -NonInteractive -Command "exit 0" >/dev/null 2>&1; then
    ok "powershell.exe 可執行"
else
    die "powershell.exe 無法執行，請確認 WSL interop 已開啟"
fi
command -v python3 >/dev/null 2>&1 || die "找不到 python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || die "需要 Python 3.8 以上（目前 $(python3 -V 2>&1)）"
ok "$(python3 -V 2>&1)"

step "2. 設定執行權限"
chmod +x "$REPO/comd.py" "$REPO/comsend" "$REPO/comtool_mcp.py" && ok "comd.py、comsend、comtool_mcp.py"

step "3. 建立指令捷徑（$BIN_DIR）"
mkdir -p "$BIN_DIR" 2>/dev/null || die "無法建立 $BIN_DIR（權限不足？可用 --bin-dir 指定其他目錄）"
[ -w "$BIN_DIR" ] || die "$BIN_DIR 沒有寫入權限（可用 sudo 執行，或用 --bin-dir 指定其他目錄）"
for pair in "${LINKS[@]}"; do
    name="${pair%%:*}"; target="$REPO/${pair#*:}"; dest="$BIN_DIR/$name"
    if [ -L "$dest" ]; then
        if [ "$(readlink -f "$dest")" = "$target" ]; then
            ok "$dest 已存在"
        else
            warn "$dest 已指向 $(readlink -f "$dest")，略過（要改用本 repo 請先刪除它）"
        fi
    elif [ -e "$dest" ]; then
        warn "$dest 已有同名檔案，略過"
    else
        ln -s "$target" "$dest" && ok "$dest → $target"
    fi
done
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR 不在 PATH 中，請在 ~/.bashrc 加入: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

MCP_STATUS="略過（--no-mcp）"
if [ "$DO_MCP" = 1 ]; then
    step "4. 註冊 Claude Code MCP server"
    CLAUDE="$(find_claude)"
    if [ -z "$CLAUDE" ]; then
        warn "找不到 claude 執行檔，請安裝 Claude Code 後手動執行:"
        info "claude mcp add -s user $MCP_NAME -- python3 $REPO/comtool_mcp.py"
        MCP_STATUS="未註冊（找不到 claude）"
    else
        info "claude: $CLAUDE"
        if "$CLAUDE" mcp remove -s user "$MCP_NAME" >/dev/null 2>&1; then
            info "已移除舊的「$MCP_NAME」註冊"
        fi
        if "$CLAUDE" mcp add -s user "$MCP_NAME" -- python3 "$REPO/comtool_mcp.py" >/dev/null; then
            ok "已註冊 MCP server「$MCP_NAME」（使用者層級）"
            MCP_STATUS="已註冊（重開 Claude Code session 後生效）"
        else
            warn "MCP server 註冊失敗"
            MCP_STATUS="註冊失敗"
        fi
    fi
fi

step "5. 檢查 Windows 端連線設定"
WINHOME="$(timeout 60 powershell.exe -NoProfile -NonInteractive -Command '$env:USERPROFILE' 2>/dev/null | tr -d '\r')"
WSLCONFIG=""
[ -n "$WINHOME" ] && WSLCONFIG="$(wslpath "$WINHOME" 2>/dev/null)/.wslconfig"
if [ -n "$WSLCONFIG" ] && [ -f "$WSLCONFIG" ] \
    && grep -Eiq '^[[:space:]]*localhostForwarding[[:space:]]*=[[:space:]]*false' "$WSLCONFIG"; then
    warn ".wslconfig 設定 localhostForwarding=false，Windows 端（PuTTY/telnet）會連不到 127.0.0.1:700N"
else
    ok "Windows 端可用 PuTTY（Telnet）或 telnet 連 127.0.0.1 7000+N"
fi

step "完成"
info "指令捷徑：$BIN_DIR/comd、$BIN_DIR/comsend"
info "MCP server：$MCP_STATUS"
info "試用：comd COM3            # 預設 115200 baud，看到 READY 表示成功"
info "      comsend COM3 \"\" 2    # 另開終端機，按 Enter 看設備回應"
[ "$WARNINGS" -gt 0 ] && printf '\n  有 %d 個警告，請檢查上面的 ⚠ 項目。\n' "$WARNINGS"
exit 0
