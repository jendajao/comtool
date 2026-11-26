# comtool：從 WSL 控制 Windows COM port

在 WSL2 裡收送 Windows 端 COM port（序列埠）的資料，並開放 TCP 連線，讓 PuTTY、Windows telnet、腳本可以**同時**連到同一個 console。另外提供 MCP server，讓 Claude Code 可以直接操作 COM port。

## 為什麼需要這個工具

- WSL2 裡的 `/dev/ttyS*` 是 Linux 核心自己的虛擬裝置，**不會**對應到 Windows 的 COMx（只有 WSL1 才有這種對應）。
- `usbipd-win` 只能把 USB 裝置掛進 WSL，主機板內建的 UART（例如 COM1）沒辦法；而且掛進去之後，Windows 端就不能再用這個裝置。

所以改由 Windows 的 `powershell.exe` 開啟 COM port，再把資料轉回 WSL。這種做法不用在 Windows 安裝任何東西，也不需要系統管理員權限。

## 架構

```
                 WSL                                          Windows
┌──────────────────────────────────────────────┐   ┌──────────────────────────────┐
│ Claude Code                                  │   │                              │
│   └─stdio─ comtool_mcp.py ─┐                 │   │                              │
│ comsend ───────────────────┤                 │   │                              │
│ PuTTY / telnet ────────────┼─ TCP 127.0.0.1:700N ─▶ comd.py ─stdio─▶ powershell.exe
│                  (多個 client 同時連線)      │   │   bridge.ps1 (.NET SerialPort) ─▶ COMx
│                                    │         │   │                              │
│                                    ▼         │   └──────────────────────────────┘
│                             log/COMx.log     │
└──────────────────────────────────────────────┘
```

| 檔案 | 說明 |
|---|---|
| `bridge.ps1` | Windows 端腳本。開啟 COM port，透過 stdin/stdout 收送原始位元組。 |
| `comd.py` | WSL 端 daemon。啟動 `bridge.ps1`，監聽 TCP，把 COM port 收到的資料廣播給所有 client 並寫入 log。 |
| `comsend` | 命令列小工具。送出一行指令，等幾秒後印出回應。 |
| `comtool_mcp.py` | MCP server，給 Claude Code 使用（見下方「MCP server」段落）。只用 Python 標準函式庫。 |
| `install.sh` | 安裝腳本：檢查環境、建立 `comd`／`comsend` 捷徑、註冊 MCP server（見下方「安裝」段落）。 |
| `log/COMx.log` | COM port 收到的所有原始資料（附加寫入）。 |
| `log/COMx.pid` | comd 執行中時存在，內容為 `<pid> <tcp_port> <baud>`；comd 結束時自動刪除。 |
| `log/COMx.comd.out` | 由 MCP 在背景啟動的 comd 的執行訊息（READY、client 連線紀錄、錯誤訊息）。 |

## 安裝

### 需求

- **WSL2**，而且要能在 WSL 裡執行 `powershell.exe`。這是 WSL 的 interop 功能，預設開啟；可以用 `powershell.exe -Command 1` 測試。
- **Python 3.8 以上**，只用標準函式庫，不需要安裝套件。
- **Windows PowerShell 5.1**，Windows 內建就有。
- （選用）Windows 端要一起看時，需要 PuTTY，或在「開啟或關閉 Windows 功能」裡勾選「Telnet 用戶端」。
- （選用）要讓 Claude Code 使用時，需要 Claude Code（CLI 或 VS Code 擴充套件）。

### 使用 install.sh 安裝

```bash
git clone <repo-url> ~/tools/comtool
cd ~/tools/comtool
./install.sh
```

`install.sh` 會依序執行：

1. **檢查環境**：WSL、`wslpath`、`powershell.exe`、Python 版本，任何一項不符就停止並說明原因。
2. **設定執行權限**：對 `comd.py`、`comsend`、`comtool_mcp.py` 執行 `chmod +x`。
3. **建立指令捷徑**：`comd`、`comsend` 的 symlink。
   - root 使用者放在 `/usr/local/bin`，其他使用者放在 `~/.local/bin`。
   - 同名檔案已經存在、而且不是指向本 repo 時，會跳過，不會覆蓋。
   - 目錄不在 `PATH` 上時，會提醒你加入。
4. **註冊 MCP server**（使用者層級，名稱 `comtool`）：
   - 先在 `PATH` 上找 `claude`，找不到就用 VS Code 擴充套件內附的最新版本。
   - 已經註冊過的話，會先移除再重新註冊，所以搬移 repo 後重跑一次就會更新路徑。
   - 找不到 `claude` 時，會印出手動註冊的指令。
5. **檢查 Windows 端連線**：如果 `.wslconfig` 設定了 `localhostForwarding=false`，會提醒 Windows 端連不到 `127.0.0.1:700N`（只提醒，不會修改設定）。

| 選項 | 說明 |
|---|---|
| `--bin-dir DIR` | 指定捷徑放置的目錄 |
| `--no-mcp` | 不註冊 MCP server；搭配 `--uninstall` 時則是不移除 |
| `--uninstall` | 移除指向本 repo 的捷徑與 MCP 註冊；不會刪除 repo 和 `log/` |

`install.sh` 可以重複執行。安裝或更新後，請**重開 Claude Code 的 session**，`com_*` 工具才會出現或更新。

### 手動安裝

不想用 `install.sh` 的話，可以手動執行：

```bash
cd ~/tools/comtool
mkdir -p ~/.local/bin
ln -sf "$PWD/comd.py" ~/.local/bin/comd
ln -sf "$PWD/comsend" ~/.local/bin/comsend
claude mcp add -s user comtool -- python3 "$PWD/comtool_mcp.py"   # 選用：給 Claude Code 使用
```

`log/` 目錄不在 repo 裡，第一次執行時會自動建立。

## 使用方式

### 1. 啟動 daemon

```bash
comd COM3                      # 預設 115200 baud，監聽 127.0.0.1:7003
comd COM1 --baud 9600          # 指定 baud rate
comd COM3 --port 8003          # 指定 TCP port（預設為 7000 + COM 編號）
```

daemon 會一直在前景執行，開啟成功時會印出：

```
READY COM3@115200 tcp://127.0.0.1:7003
```

之後 client 連線或離線時，也會印出一行訊息。要結束時按 `Ctrl+C`，或送 SIGTERM，COM port 會隨之釋放。

> **注意**：daemon 執行期間，COM port 會被佔用，Tera Term／PuTTY **不能**直接開啟該 COM port。要直接用 COM port 時，請先結束 daemon；改連 TCP port 的話，則可以跟 daemon 同時使用。

### 2. 用 comsend 送指令

```bash
comsend COM3 "show system-info" 2     # 送出指令並自動加 CR，等 2 秒後印出回應
comsend COM3 "" 1                     # 只按一次 Enter
comsend --raw COM3 '\x11' 1           # 原樣送出（不加 CR），可用 \xNN 表示控制字元
```

- 等待秒數預設為 1 秒，輸出較多的指令（例如開機、`show config`）請自行加長。
- 輸出保留設備送出的 ANSI 跳脫碼，只拿掉 `\r`。要看純文字的話，可以這樣過濾：
  ```bash
  comsend COM3 "show config" 3 | sed 's/\x1b\[[0-9;]*[A-Za-z]/\n/g'
  ```
- 要連到非預設的 TCP port 時，請設定環境變數 `COMSEND_PORT=8003`。

### 3. 從 Windows 端一起看、一起操作

**PuTTY**

| 設定位置 | 值 |
|---|---|
| Session → Host Name | `127.0.0.1` |
| Session → Port | `7003`（COM1 則是 `7001`） |
| Session → Connection type | **Telnet** |
| Terminal → Keyboard → The Backspace key | **Control-H** |

**Windows telnet（cmd）**

```
telnet 127.0.0.1 7003
```

注意事項：

- host 和 port 之間要用**空白**隔開，不能寫成 `localhost:7003`。
- 請用 `127.0.0.1`，不要用 `localhost`。`localhost` 會先解析成 IPv6 的 `::1`，但 WSL 只轉發 IPv4 到 Windows，所以會連不上。
- Backspace 請用 Control-H：這台設備的 CLI 不認 DEL（`0x7F`），會直接顯示 `^?`。

所有 client 看到的都是同一個 console：任何一方輸入的內容都會送到設備，設備的輸出也會送給每個 client。

### 4. 查看 log

```bash
tail -f /root/tools/comtool/log/COM3.log
```

不管當時有沒有 client 連線，daemon 都會把收到的資料寫進 log，所以設備重開機時的輸出不會漏掉。log 只會一直附加寫入，需要時請自行清理。

## MCP server（給 Claude Code 使用）

`comtool_mcp.py` 是一個 stdio MCP server。Claude Code 啟動它之後，它會對每個 COM port 維持一條連到 comd 的**持續 TCP 連線**，Claude 不用再透過 Bash 執行 `comsend`、固定等幾秒，而是送出指令後**等到提示字元出現就回傳**，回傳的內容也已經去掉 ANSI 碼。

### 註冊

`install.sh` 會自動在使用者層級註冊，所有專案都能用。手動註冊的方式如下：

```bash
claude mcp add -s user comtool -- python3 <repo 路徑>/comtool_mcp.py
```

- `claude` 不在 PATH 上時，請改用 VS Code 擴充套件內附的執行檔，例如 `/root/.vscode-server/extensions/anthropic.claude-code-<版本>-linux-x64/resources/native-binary/claude`。
- 註冊或修改 `comtool_mcp.py` 之後，要**重開 Claude Code 的 session** 才會生效。
- 查看狀態：在 Claude Code 對話框輸入 `/mcp`，或在終端機執行 `claude mcp list`。VS Code 內建的「MCP SERVERS」清單是 VS Code／Copilot 自己的設定，看不到 comtool。

### 工具

| 工具 | 主要參數 | 功能 |
|---|---|---|
| `com_list` | — | 列出 Windows 的 COM port，以及各 port 的 comd 是否執行中、MCP 是否已連線 |
| `com_open` | `port`、`baud`=115200、`tcp_port` | comd 沒在執行就在背景啟動（等到 READY，最多 15 秒），然後建立 MCP 連線；已在執行就直接連線 |
| `com_send` | `port`、`text`、`expect`、`timeout`=10、`idle`=0.5、`newline`=true、`raw`=false、`strip_ansi`=true | 送出文字（預設加 CR）。有 `expect`（regex）就等到符合或逾時；沒有就等到 `idle` 秒內沒有新資料 |
| `com_read` | `port`、`expect`、`timeout`=0 | 不送任何東西，讀取還沒讀過的輸出；可等待某段文字出現（例如看開機 log） |
| `com_log` | `port`、`bytes`=4000 | 讀 `log/COMx.log` 的最後一段，可看到 MCP 連線之前的輸出 |
| `com_close` | `port`、`stop_daemon`=true | 中斷 MCP 連線；預設一併結束 comd，釋放 COM port |

回傳內容的第一行是 `[狀態 秒數]`，狀態有四種：

| 狀態 | 意思 |
|---|---|
| `matched` | 符合 `expect`；符合後會再多收 0.15 秒內接著到的資料，避免輸出被切成兩段 |
| `timeout` | 逾時，`expect` 沒有符合 |
| `idle` | 沒有指定 `expect`，而且一段時間內沒有新資料 |
| `closed` | 與 comd 的連線中斷了 |

使用注意：

- `com_send` 的回傳會**包含送出前還沒讀過的輸出**（例如你從 telnet 操作的內容），不會漏掉；但 `expect` 只比對送出之後收到的資料。
- 使用 `raw=true` 可以送控制字元，例如 `\x03`（Ctrl+C）、`\x11`（Ctrl+Q），或分頁時要按的 `g`、空白鍵。
- 去掉 ANSI 碼的規則：游標定位碼換成換行，其他 CSI 序列刪除，`\r` 也刪除。這樣全螢幕的登入畫面也能讀成一行一行的文字。

### 生命週期

- MCP server 跟著 Claude Code 的 session 啟動和結束；結束時只會關閉自己的 TCP 連線。
- **comd 不會跟著 Claude Code 結束**。所以你的 PuTTY／telnet 不會斷線，log 也會繼續記錄；但用完要記得呼叫 `com_close`（或手動結束 comd），否則 COM port 會一直被佔用。
- 你手動執行的 `comd` 也會寫 pidfile，所以 `com_list`、`com_open`、`com_close` 一樣看得到、管得到它。

## 已知的設備

| Port | 介面 | 設備 | 設定 | 登入後提示字元 |
|---|---|---|---|---|
| COM1 | 主機板內建 UART | IE-SW-AL05LM-5TX | 9600 8N1 | `switch>` |
| COM3 | FTDI USB Serial（VID_0403 / PID_6015） | IGS-C9082GP | 115200 8N1 | `IGS-C9082GP#` |

- 預設 baud rate 是 115200。COM1 這台是 9600，開啟時要指定：`comd COM1 --baud 9600`，或 `com_open` 帶 `baud=9600`。
- COM3 接的設備會更換（之前接的是 IE-SW-AL08M-6TX-2GT，9600 baud）。開啟前請先確認設備與 baud rate；收到亂碼或只有 `0x00` 時，就換 baud rate 再試。
- 這幾台都是全螢幕的 CLI 登入畫面，用 `logout` 登出。
- 在 Username 欄位輸入帳號後，設備只會回顯帳號、把游標移到 Password 欄位，不會再印出 `Password` 字樣；用 `com_send` 時，`expect` 請直接比對帳號本身。
- IGS-C9082GP 的 CLI 會分頁，輸出停在 `-- more --` 時：空白鍵看下一頁、`g` 全部跑完、`^C` 中斷。

> 送 `logout` 前，請先確認設備停在提示字元。設備可能已經閒置逾時自動登出，或有人從 telnet 操作過；如果停在登入畫面，`logout` 會被當成帳號打進 Username 欄位。這時再按一次 Enter，讓這次登入以失敗結束即可。

## Telnet 協商說明

- client 一連上，`comd` 會主動送出 `IAC WILL ECHO`、`IAC WILL SGA`（`ff fb 01 ff fb 03`），請 client 關閉本機回顯，改由設備回顯，並逐字送出。
  - 沒有這一步的話，Windows telnet 會在本機回顯，加上設備自己的回顯，每個字都會出現兩次。
- client 送來的 Telnet 協商序列會被濾掉，不會送到設備。
- 送過 IAC 的 client 會被視為 telnet client：
  - 它送來的 CR LF／CR NUL 會轉成單一個 CR，避免一次 Enter 被算成兩次。
  - 設備輸出中的 `0xFF` 會跳脫成 `0xFF 0xFF`。
- 用 PuTTY **Raw** 模式或 `nc` 連線時，一開始會多收到上述 6 個位元組，畫面可能出現少許亂碼。建議一律用 Telnet 模式；`comsend` 已經會自動過濾。

## 疑難排解

| 現象 | 原因／處理 |
|---|---|
| 啟動時出現 `OPEN FAIL: ... 拒絕存取` | COM port 被其他程式（Tera Term、PuTTY 直連、另一個 comd）佔用，請先關掉該程式。 |
| 啟動時出現 `TCP port 700N 無法使用` | 已經有另一個 comd 在執行，或該 port 被其他程式佔用。 |
| 收到亂碼，或只收到 `0x00` | baud rate 不對（預設是 115200），請用 `--baud` 試試其他速率（9600、19200、38400、57600）。 |
| `install.sh` 顯示「找不到 powershell.exe」 | WSL interop 沒有開啟，請檢查 `/etc/wsl.conf` 的 `[interop]` 設定，然後在 Windows 執行 `wsl --shutdown` 重新啟動 WSL。 |
| `install.sh` 顯示捷徑「略過」 | 目標位置已經有同名檔案，或有指向其他地方的 symlink。確認不需要後刪除它，再重新執行 `install.sh`。 |
| `comsend` 顯示「無法連到 COMx」 | comd 沒有在執行，或 TCP port 不是預設值（請設定 `COMSEND_PORT`）。 |
| PuTTY 看不到自己輸入的字 | 請確認 Connection type 選 Telnet，且 Terminal → Local echo 維持 Auto。 |
| Windows telnet 連不上 | 請確認用的是 `telnet 127.0.0.1 7003`（用空白隔開、用 127.0.0.1）。 |
| 結束 daemon 後，COM port 仍被佔用 | 可能殘留 `powershell.exe` 程序，請在 Windows 工作管理員中結束它。 |
| Claude Code 裡看不到 `com_*` 工具 | 剛註冊或剛修改 `comtool_mcp.py`，請重開 session；用 `/mcp` 確認 comtool 是不是 connected。 |
| `com_open` 回傳「comd 啟動失敗」 | 錯誤訊息會附在回傳內容裡，完整紀錄在 `log/COMx.comd.out`；常見原因是 COM port 被其他程式佔用。 |
| `com_send` 一直 `timeout` | `expect` 沒有比對到，例如大小寫不同（`-- more --` 是小寫），或設備停在分頁提示。用 `com_read` 看目前收到的內容。 |
| `com_list` 顯示 comd 執行中，但其實已經結束 | `com_list` 會自動清掉殘留的 pidfile；如果仍然異常，請手動刪除 `log/COMx.pid`。 |

## 安全性

- TCP 只監聽 `127.0.0.1`，區域網路上的其他電腦連不進來。
- 連線**沒有驗證**：本機任何程式都能連上並操作設備的 console。
- 設備的帳密不要寫進這個目錄的任何檔案。
- 透過 MCP 的 `com_send` 送出的密碼會留在 Claude Code 的對話紀錄裡（設備畫面上只會顯示 `*`）。
