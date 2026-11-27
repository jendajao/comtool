# comtool 安裝指南

repo 可以放在任何位置，Claude Code 的安裝方式與 MCP 設定位置也因人而異，所以這裡不提供一鍵安裝腳本，而是列出每個步驟該檢查什麼、遇到不同狀況怎麼處理。你可以自己照著做，也可以交給 Claude Code 代勞。

## 最快的方式：請 Claude 幫你安裝

1. 把 repo clone 到任意位置，例如：
   ```bash
   git clone <repo-url> ~/tools/comtool
   ```
2. 在 repo 目錄開啟 Claude Code（CLI 或 VS Code 擴充套件），輸入：
   ```
   請依照 INSTALL.md 幫我安裝 comtool
   ```

Claude 會依照下面的「安裝步驟」逐一檢查環境並安裝。遇到需要你決定的地方（MCP 要註冊在哪個 scope、是否覆蓋已存在的檔案或註冊），會先問你。

有些步驟 Claude 做不到（例如 SSH 模式下你本機 Windows 上的設定、重開 Claude Code 的 session），這些步驟標示 **👤 使用者手動**，Claude 會在最後逐項提醒你。README 的「安裝 → ⚠ 需要你手動做的事」也列了同一份清單。

> **給 Claude 的說明**：請依序執行下面每個步驟，每一步都先檢查、再動手。下列情況一律先詢問使用者，不要自行決定：
> - 要覆蓋或刪除已存在的檔案、symlink（包括已存在的 `comhub.ini`）
> - 要移除或取代已存在的 `comtool` MCP 註冊
> - MCP scope 的選擇（可以提出建議的預設值）
> - 需要 `sudo` 或修改 `~/.bashrc`、`/etc/wsl.conf`、`.wslconfig` 等系統或 shell 設定
>
> 標示 **👤 使用者手動** 的步驟你做不到或不該代做，不要嘗試；全部做完後，先列出實際做了什麼（使用的 `claude` 執行檔、MCP scope 與註冊的指令、產生的 `comhub.ini` 內容），再**逐項提醒使用者所有 👤 步驟**：
> - WSL 模式：重開 Claude Code 的 session；如果步驟 2 或 5 要修改 `/etc/wsl.conf`、`.wslconfig`，提醒在 Windows 執行 `wsl --shutdown`（會中斷 WSL 裡的你，所以要使用者自己做）。
> - SSH 模式：「SSH 模式：Windows 端」的 W1～W4 全部（安裝 Python、`scp -r` 複製 repo、金鑰登入與 host key、拿掉舊的 RemoteForward、雙擊 `start_comtool.bat`），以及重開 Claude Code 的 session。把其中的 `<帳號>`、`<build server IP>`、`<build server 上的 repo 路徑>` 換成實際的值（repo 路徑就是 `$REPO`）。
>
> 使用者只要求「請依照 INSTALL.md 幫我產生 comhub.ini」時，只做步驟 7，然後提醒使用者重做 W1（`scp -r`）把它帶到 Windows。

## 兩種模式

| 模式 | Claude Code 執行位置 | COM port 所在 | 要安裝的地方 |
|---|---|---|---|
| **WSL 模式** | 本機 Windows 的 WSL2 | 同一台 Windows | 只有 WSL |
| **SSH 模式** | build server（用 VSCode Remote-SSH 或一般 ssh 登入都可以） | 你的本機 Windows（不需要 WSL） | build server（下面的安裝步驟）＋ 本機 Windows（「SSH 模式：Windows 端」） |

兩種模式用同一份程式，MCP server 會自動判斷。SSH 模式的連線原理見 README 的「連線步驟與流程」。

## 需求

- **Python 3.8 以上**，只用標準函式庫，不需要安裝套件。
- WSL 模式：**WSL2**，而且要能在 WSL 裡執行 `powershell.exe`。這是 WSL 的 interop 功能，預設開啟。
- SSH 模式：build server 是 Linux；本機 Windows 要裝 **Python 3.8 以上**（Windows 版），以及 Windows 內建的 OpenSSH 用戶端（`ssh.exe`）。
- **Windows PowerShell 5.1**，Windows 內建就有。
- （選用）Windows 端要一起看時，需要 PuTTY，或在「開啟或關閉 Windows 功能」裡勾選「Telnet 用戶端」。
- （選用）要讓 Claude Code 使用時，需要 Claude Code（CLI 或 VS Code 擴充套件）。
- （選用）要傳 firmware 給設備時，本機 Windows 需要 TFTP server（例如 tftpd64）。

## 安裝步驟

以下指令都在 repo 目錄執行。**請用一般使用者身分執行，不要用 `sudo`**，否則 MCP server 會註冊到 root 的 Claude 設定，而不是你自己的。

### 1. 確認 repo 位置

```bash
cd <repo 路徑>
REPO="$(pwd -P)"
echo "$REPO"
```

之後的步驟都用 `$REPO` 指向 repo 的實際路徑（已解析 symlink）。

### 2. 判斷模式並檢查環境

```bash
grep -qi microsoft /proc/version && echo WSL || echo "不是 WSL"
echo "SSH_CONNECTION=$SSH_CONNECTION"
python3 -c 'import sys; print(sys.version); assert sys.version_info >= (3, 8)'
```

- 是 WSL → **WSL 模式**，繼續檢查：
  ```bash
  command -v wslpath                                     # WSL 內建工具
  powershell.exe -NoProfile -NonInteractive -Command 1   # 應輸出 1
  ```
- 不是 WSL，而且 `SSH_CONNECTION` 有值 → **SSH 模式**（Claude Code 跑在 build server 上）。這台機器不需要 `powershell.exe`；COM port 由本機 Windows 的 hub 處理；除了步驟 3～4，還要做步驟 7（產生 `comhub.ini`），以及最後的「SSH 模式：Windows 端」（👤 使用者手動）。
- 兩者都不是 → 不支援。

| 失敗的項目 | 處理方式 |
|---|---|
| WSL 模式：找不到或無法執行 `powershell.exe` | WSL interop 沒有開啟，或 Windows 路徑沒有加進 PATH。檢查 `/etc/wsl.conf` 的 `[interop]`：`enabled` 與 `appendWindowsPath` 不能是 `false`。修改後 **👤 使用者手動**在 Windows 執行 `wsl --shutdown` 重新啟動 WSL（會中斷 WSL 裡的 Claude）。 |
| Python 版本太舊或找不到 | 安裝 Python 3.8 以上，例如 `sudo apt install python3`。 |

### 3. 設定執行權限

```bash
chmod +x "$REPO/comd.py" "$REPO/comsend" "$REPO/comput" "$REPO/comhub.py" "$REPO/comtool_mcp.py"
```

### 4. 註冊 MCP server（給 Claude Code 使用，選用）

**4-1. 找到 `claude` 執行檔**

依序找，用第一個找到的：

```bash
command -v claude
ls ~/.local/bin/claude ~/.claude/local/claude 2>/dev/null
ls -d ~/.vscode-server*/extensions/anthropic.claude-code-*/resources/native-binary/claude \
      ~/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | sort -V | tail -n 1
```

- 擴充套件內附的版本有好幾個時，用版本號最新的那個。
- 如果你平常有設定 `CLAUDE_CONFIG_DIR`，執行下面的 `claude mcp` 指令時也要帶著同樣的環境變數，否則會寫到另一份設定。

以下用 `claude` 代表找到的執行檔。

**4-2. 檢查是否已經註冊過**

```bash
claude mcp list
claude mcp get comtool
```

- 已經有 `comtool`，而且指向目前的 `$REPO/comtool_mcp.py`：不用再註冊。
- 指向舊位置（例如 repo 搬過家）：用 `claude mcp remove -s <原本的 scope> comtool` 移除後再註冊。
- 某個專案的 `.mcp.json` 裡也有 `comtool` 時，在那個專案中會以 project scope 的設定為準。

**4-3. 選擇 scope 並註冊**

| scope | 適用情況 | 設定存放位置 |
|---|---|---|
| `user`（建議） | 所有專案都能用 | 使用者的 Claude 設定（`~/.claude.json`） |
| `local` | 只在目前這個專案目錄使用 | 使用者的 Claude 設定中，該專案的區塊 |
| `project` | 要跟團隊共用，寫進某個專案的 `.mcp.json` | 該專案的 `.mcp.json`（路徑是本機絕對路徑，不適合 commit 給別人用） |

Python 請用**絕對路徑**，避免 Claude Code 啟動 MCP server 時的 PATH 跟你的 shell 不同（pyenv、conda、VS Code 環境）而找到別的 Python：

```bash
claude mcp add -s user comtool -- "$(command -v python3)" "$REPO/comtool_mcp.py"
```

**4-4. 👤 使用者手動：重開 Claude Code 的 session**

註冊後（以及之後每次更新 `comtool_mcp.py`／`hubclient.py`），要重開 session，`com_*` 工具才會出現或更新。Claude 不能重開自己的 session，請提醒使用者。

### 5. 檢查 Windows 端連線設定（WSL 模式，選用）

如果要從 Windows 端用 PuTTY／telnet 連 `127.0.0.1:7000+N`，WSL 的 localhost 轉發必須開啟：

```bash
WINHOME="$(powershell.exe -NoProfile -NonInteractive -Command '$env:USERPROFILE' | tr -d '\r')"
grep -i localhostForwarding "$(wslpath "$WINHOME")/.wslconfig" 2>/dev/null
```

如果看到 `localhostForwarding=false`，Windows 端會連不到；要改的話，把它刪掉或改成 `true`，然後 **👤 使用者手動**在 Windows 執行 `wsl --shutdown`。

### 6. 設定 TFTP 根目錄（WSL 模式，選用）

要用 `com_put_file`／`comput` 傳檔時，hub 要知道 TFTP 根目錄。hub 會自動讀 tftpd64.ini 的 `BaseDirectory`；找不到時，在 `~/.bashrc` 設定環境變數（Windows 路徑也可以，會自動轉換），然後讓 hub 重新啟動（結束 `comhub.py` 的 process，MCP 下次會自動再啟動它）：

```bash
export COMTOOL_TFTP_ROOT='D:\tftp'
```

### 7. 產生 `comhub.ini`（SSH 模式，Claude 代勞）

`comhub.ini` 是 SSH 模式的個人設定（build server 的帳號、IP、repo 路徑），由本機 Windows 的 hub 讀取。它列在 `.gitignore`，不會進 repo；在 build server 的 repo 目錄產生後，使用者在 W1 用 `scp -r` 複製 repo 時會一起帶到 Windows。

這一步可以單獨執行（使用者輸入「請依照 INSTALL.md 幫我產生 comhub.ini」時）。

```bash
cd "$REPO"
ls -l comhub.ini 2>/dev/null && cat comhub.ini       # 已存在：給使用者看，問要不要覆蓋，不要自行覆蓋
echo "user=$USER host=$(echo "$SSH_CONNECTION" | awk '{print $3}') repo=$REPO"
```

- `host` 取 `$SSH_CONNECTION` 的第 3 欄（使用者連進來時 build server 的 IP）。`SSH_CONNECTION` 是空的（例如不是經由 ssh 登入）時，請問使用者 build server 的 IP。
- 確認後寫入（UTF-8）：

```ini
[ssh]
user = <$USER>
host = <build server IP>
repo = <$REPO>

[hub]
tftp_root =        ; TFTP 根目錄，留空表示自動讀 tftpd64.ini
```

- `tftp_root` 預設留空。使用者知道 tftpd64.ini 讀不到（或要用別的目錄）時，再填 Windows 路徑，例如 `D:\fw_image`。
- 產生後告訴使用者填了哪些值，並提醒重做 W1 把它帶到 Windows。

## SSH 模式：Windows 端（👤 使用者手動）

build server 上的 Claude 碰不到你的 Windows，以下步驟請自己在本機 Windows 的 PowerShell 執行。`<帳號>`、`<build server IP>`、`<build server 上的 repo 路徑>` 跟步驟 7 產生的 `comhub.ini` 相同（Claude 會告訴你實際的值）。

**W1. 👤 把 repo 複製到 Windows**

```powershell
scp -r jenda@192.168.2.201:~/tools/comtool comtool
```

步驟 7 產生的 `comhub.ini` 會一起複製過來。之後更新時（`comhub.py`、`comd.py`、`hubclient.py`、`bridge.ps1`、`start_comtool.bat` 有修改），重新執行同一個指令覆蓋，再重新啟動 hub（關閉 `start_comtool.bat` 的視窗後再雙擊）。

**W2. 👤 檢查 Python**

```powershell
python -c "import sys; print(sys.version)"     # 需要 3.8 以上（也可以用 py -3）
```

找不到 `python` 時，從 python.org 安裝 Windows 版 Python，安裝時勾選「Add python.exe to PATH」（或保留預設安裝的 `py` launcher，`start_comtool.bat` 會優先用 `py -3`）。

**W3. 👤 設定用金鑰登入 build server**

hub 在背景執行 ssh，不能輸入密碼或 passphrase，也不能回答 host key 的確認。

```powershell
ssh <帳號>@<build server IP>                          # 第一次：手動登入一次，接受 host key（寫進 known_hosts）
ssh -o BatchMode=yes <帳號>@<build server IP> true    # 不需要輸入任何東西、沒有錯誤訊息，就是成功
```

- 還沒有金鑰：`ssh-keygen -t ed25519`，再把 `~\.ssh\id_ed25519.pub` 的內容加到 build server 的 `~/.ssh/authorized_keys`。
- 金鑰有 passphrase：用系統管理員身分的 PowerShell 執行 `Get-Service ssh-agent | Set-Service -StartupType Automatic; Start-Service ssh-agent`，再用一般身分執行 `ssh-add`。
- **從舊版升級**：如果 `~\.ssh\config` 有 `RemoteForward .../comtool.sock ...`（或舊名稱 `hub.sock`）那一行，請刪掉。現在 tunnel 由 hub 建立，留著會跟 hub 搶同一個入口。

**W4. 👤 雙擊 `start_comtool.bat` 啟動 hub（每次要用 comtool 之前）**

在檔案總管打開 `%LOCALAPPDATA%\comtool`，雙擊 `start_comtool.bat`。視窗中看到下列訊息就表示成功：

```
comhub 啟動：windows，監聽 tcp:7000，TFTP 根目錄 ...
[tunnel <build server IP>] tunnel 已建立：...
```

- 使用 comtool 期間**請讓這個視窗保持開啟**；關閉視窗（或按 Ctrl+C）就會結束 hub 與 tunnel。
- 找不到 `comhub.ini` 時，視窗會顯示要怎麼產生它（回到步驟 7）。
- 連不上 build server 時，視窗會顯示 ssh 的錯誤訊息，hub 會每隔一段時間自動重試；同樣的紀錄也在 `%LOCALAPPDATA%\comtool\log\comhub.log`。

**W5. 確認 tunnel（可以請 Claude 在 build server 上做）**

```bash
ls -l <build server 上的 repo 路徑>/comtool.sock   # 應該是 srw-------，擁有者是你
<build server 上的 repo 路徑>/comput <任一個小檔案>   # 應該回 [uploaded ...]，檔案出現在 TFTP 根目錄
```

tunnel 跟 VSCode 無關：之後 VSCode 斷線、或改用一般 ssh 終端機登入 build server，都不影響 tunnel；SSH 斷線時 hub 會自動重建，並清除 build server 上殘留的 `comtool.sock`。

## 驗證

```bash
# 語法檢查
python3 -m py_compile "$REPO/comd.py" "$REPO/comhub.py" "$REPO/hubclient.py" "$REPO/comtool_mcp.py" "$REPO/comsend" "$REPO/comput"

# 不接硬體，直接測 MCP server（應回傳 initialize 結果與工具清單）
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 "$REPO/comtool_mcp.py"
```

- **重開 Claude Code 的 session**，在對話框輸入 `/mcp`，確認 `comtool` 是 connected。VS Code 內建的「MCP SERVERS」清單是 VS Code／Copilot 自己的設定，看不到 comtool。
- 在 Claude Code 中呼叫 `com_list`，第一行會顯示 hub 的位置（WSL，或本機 Windows 經由 SSH 轉送）與 TFTP 根目錄。
- 接硬體測試：在 Claude Code 中請 Claude 用 `com_open`／`com_send` 測試（SSH 模式要先雙擊 `start_comtool.bat`）。只測 comd 本身時（輔助用途），WSL 模式執行 `"$REPO/comd.py" COM3` 看到 `READY`，另開終端機 `"$REPO/comsend" COM3 "" 2`。用法詳見 [README.md](README.md)。

## 更新或搬移 repo

- 只是 `git pull` 更新：不用重新安裝；如果 `comtool_mcp.py`／`hubclient.py` 有更新，**👤 重開 Claude Code 的 session**。如果 `comhub.py`／`comd.py` 有更新，也要讓 hub 重新啟動（WSL 模式：結束 `comhub.py` 的 process，MCP 下次會自動啟動；SSH 模式：**👤 重做 W1**，再關閉並重新雙擊 `start_comtool.bat`）。
- 搬移 repo 位置：用 `claude mcp remove -s <scope> comtool` 移除舊註冊，再依步驟 4-3 重新註冊。SSH 模式還要更新 `comhub.ini` 的 `repo`（重做步驟 7），再 **👤 重做 W1**。

## 移除

```bash
claude mcp remove -s user comtool                    # 依當初註冊的 scope
```

移除前請先結束所有 comd（或在 Claude Code 中呼叫 `com_close`），否則 COM port 會一直被佔用。repo 與 `log/` 需要時再自行刪除。

SSH 模式的 Windows 端（👤 使用者手動）：關閉 `start_comtool.bat` 的視窗，再刪除 `%LOCALAPPDATA%\comtool`。
