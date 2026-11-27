# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

從 WSL2 或 SSH remote（build server，用 VSCode 或一般 ssh 登入都可以）控制 Windows COM port 的工具組，主要給 Claude Code 透過 MCP 使用；comd／comsend／comput 的命令列用法只是輔助（除錯、自動化），不要為人手動操作另外設計功能。由 `powershell.exe` 在 Windows 端開 port，再透過 stdio 轉出來。SSH 模式下 COM port 在使用者本機 Windows（不開 WSL），使用者雙擊 `start_comtool.bat` 以 Windows 原生 Python 執行 comhub（讀 `comhub.ini` 的個人設定），hub 自己執行 `ssh -R` 在 build server 建立 Unix Domain Socket 入口，跟 VSCode 無關。只用 Python 標準函式庫與 Windows 內建的 PowerShell 5.1／OpenSSH，**不要引入第三方套件**（包括 MCP SDK、pyserial）。comd.py、comhub.py、hubclient.py 要能同時在 Linux（WSL）與 Windows 原生 Python 執行。

使用者文件在 [README.md](README.md)（繁體中文，**設計文件**：改動前要在計畫中列出、經使用者同意），安裝步驟在 [INSTALL.md](INSTALL.md)；改動行為、參數或預設值時要同步更新這兩份文件。需要使用者手動做的步驟，INSTALL.md 標示「👤 使用者手動」並要求 Claude 最後逐項提醒，README「安裝 → ⚠ 需要你手動做的事」也要列出。

## 開發與驗證

沒有建置流程、測試套件或 lint 設定。常用的檢查方式：

```bash
python3 -m py_compile comd.py comhub.py hubclient.py comtool_mcp.py comsend comput   # 語法檢查

# 不接硬體，直接測 MCP server 的 JSON-RPC
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 comtool_mcp.py

# 不接硬體，在 Linux 上單獨測 hub（傳檔、殘留 socket 處理）；不要用預設的 <repo>/comtool.sock，
# SSH 模式下它是 sshd 建立的轉送入口
python3 comhub.py --listen unix:/tmp/t/h.sock --tftp-root /tmp/t/tftp &
COMTOOL_HUB=unix:/tmp/t/h.sock ./comput <檔案>
# comd 可以用假的 powershell.exe（把 stdin 原樣寫回 stdout、stderr 印 OPENED）與假的 wslpath 測試；
# 假的 powershell.exe 對 -Command 不輸出任何東西時，find_orphans() 只會查 /proc

# 不接 Windows，在 Linux 上測 tunnel：遠端用 127.0.0.1（需要能用金鑰 ssh 到自己）。
# 為了不動使用者的 known_hosts，在 PATH 前面放一個 ssh wrapper：
#   exec /usr/bin/ssh -o UserKnownHostsFile=/tmp/t/kh -o StrictHostKeyChecking=accept-new "$@"
python3 comhub.py --listen tcp:17000 --ssh 127.0.0.1:/tmp/t/t.sock &
COMTOOL_HUB=unix:/tmp/t/t.sock ./comput <檔案>      # kill -9 hub 的 ssh 後幾秒內應自動重建

# 接硬體的端到端測試
./comd.py COM3                 # 前景執行，看到 READY 表示成功
./comsend COM3 "" 1            # 另一個終端機送一次 Enter
```

修改 `comtool_mcp.py`／`hubclient.py` 後，要重開 Claude Code session，`com_*` 工具才會載入新版本；修改 `comhub.py`／`comd.py` 後，要重新啟動 hub（SSH 模式下還要請使用者把檔案複製到 Windows，見 INSTALL.md W1，再重新雙擊 `start_comtool.bat`）。`comhub.ini` 是個人設定，列在 `.gitignore`，不要 commit。

## 架構

```
comtool_mcp.py / comput ──hubclient──▶ <repo>/comtool.sock ──▶ comhub.py ──▶ comd.py ──stdio──▶ powershell.exe bridge.ps1 ──▶ COMx
                                        （WSL：hub 自己監聽；SSH：start_comtool.bat → comhub.py 讀 comhub.ini → ssh -R → Windows 127.0.0.1:7000）
comsend / PuTTY ──TCP 127.0.0.1:7000+N──▶ comd.py（與 hub 在同一台機器）
```

各元件之間的約定（改一邊時要檢查另一邊）：

- **bridge.ps1 ↔ comd.py**：stdout／stdin 是原始位元組；stderr 是控制訊息。`OPENED ...` 表示開啟成功，`OPEN FAIL: ...` 後以 exit 2 結束。comd 關閉 stdin（或 comd 被強制結束）時 bridge 會結束並釋放 COM port。
- **comd.py**：用執行緒（rx、err、每個 client 一個 reader 與一個 writer）而不是 select，因為 Windows 的 select 不能用在 pipe 上。accept 用 0.5 秒 timeout 輪詢，讓 Windows 的 Ctrl+C 能處理。rx 執行緒只把資料放進各 client 的送出佇列（`Client.send()` 不阻塞），由 `Client.writer()` 送出；佇列超過 `PENDING_MAX`（1 MB）就把該 client 斷線，避免一個卡住的 client 拖住 log、其他 client 與 COM port。
- **comd.py 的 pidfile**：收到 `OPENED` 後才寫 `log/COMx.pid`，內容是 `<pid> <tcp_port> <baud>`，結束時由 atexit 刪除（只刪自己 pid 的檔案）。`comhub.py` 的 `daemon_info()` 以這個檔案判斷 comd 是否執行中、該連哪個 TCP port；`open` 也是輪詢它來等 READY。MCP **不再**直接讀 pidfile。Windows 上 comd 被 `TerminateProcess` 結束時 atexit 不會執行，pidfile 由 hub 的 `close_port()` 刪除；檢查 process 存活用 ctypes（`os.kill(pid, 0)` 在 Windows 上會送 CTRL_C_EVENT）；Linux 上 `pid_alive()` 會先回收 hub 自己的 zombie 子程序，否則剛結束的 comd 仍會被當成存活。
- **殘留程序**：`comhub.find_orphans(com)` 找命令列含**這個 repo** 的 `comd.py`／`bridge.ps1` 完整路徑、而且參數是 COMx 的程序（Windows 程序用一次 PowerShell CIM 查詢，WSL 上的 comd 讀 `/proc`）。只在「這個 port 沒有執行中的 comd」時呼叫，所以找到的都是殘留：`close_port()` 會結束它們（WSL 上的 Windows 程序用 `Stop-Process`），`open_port()` 失敗時只在錯誤訊息裡回報（沒有殘留而且是 `OPEN FAIL` 就提示被其他程式占用），不結束任何程序。
- **hub 協定**（`comhub.py` ↔ `hubclient.py`）：JSON 一行一個訊息。連線後 hub 先送問候訊息 `{"hub":"comtool","version":1,"id","platform","tunnels",...}`（`id` 是每次啟動隨機產生的 `HUB_ID`），client 送一個請求 `{"cmd": info|list|open|close|log|attach|put, ...}`。控制請求回一個 JSON 後關閉；`attach` 回 ok 後連線變成與 comd 對接的原始資料流（`JsonSock.take_buf()` 取回讀過頭的位元組）；`put` 回 `same` 或 `ready`，ready 後 client 送 size 個位元組，hub 寫 `<name>.part`、驗 md5 後 `os.replace`，回 `uploaded`。
- **comtool.sock**：`<repo>/comtool.sock`，Unix Domain Socket，權限 0600。WSL 模式由 hub 建立（`make_listener()`）；SSH 模式由 sshd 依 hub 的 `Tunnel` 執行緒執行的 `ssh -N -R <repo>/comtool.sock:127.0.0.1:7000`（`BatchMode`、`ExitOnForwardFailure`、`ServerAlive` 15 秒×3、`LogLevel=ERROR`）建立；Windows 上 ssh.exe 放進 KILL_ON_JOB_CLOSE 的 Job Object，hub 被強制結束也會一起結束（comd 不放）。sshd 斷線時不會刪除 socket（`StreamLocalBindUnlink` 預設 no），所以 ssh 因 `remote port forwarding failed` 結束時，hub 用另一條 ssh 在 build server 執行 `CLEANUP_PY`（`python3 -`，程式從 stdin 送）：不存在→`absent`；不是 socket／不是自己的→不動；5 秒內收到 hub 問候訊息→`live`（`id` 等於自己的 `HUB_ID` 表示是先前留下的 ssh 正轉給自己，否則是其他 hub，state `busy`）；其他情況刪除後回 `removed` 並重建。`hubclient._connect_once()` 遇到 ECONNREFUSED 也會刪除殘留檔，提示等 hub 重建。防止誤連只靠 socket 權限與擁有者檢查，沒有其他驗證。
- **Windows 上的文字編碼與 PowerShell 輸出**（都是 Windows 實測才發現的問題，Linux 假環境測不到）：
  - hub 啟動 comd 時設 `PYTHONIOENCODING=utf-8`：comd 的輸出導到 `log/COMx.comd.out` 時，Windows 預設用 cp950 寫，hub 以 UTF-8 讀回會變亂碼，`open_fail_hint()` 也比對不到「無法使用」。
  - `bridge.ps1` 開頭把 `[Console]::OutputEncoding` 設成不含 BOM 的 UTF-8（stderr 控制訊息）；comd 讀 stderr 時也會去掉 BOM，否則比對不到 `OPENED`。`bridge.ps1` 要維持純 ASCII（PowerShell 5.1 把沒有 BOM 的 .ps1 當 cp950 讀）。
  - 用 PowerShell 查詢後交給 Python 解析的輸出，一律用 `[Console]::Out.WriteLine()`：直接輸出字串時 PowerShell 會依寬度（約 120 字）折行，長命令列會被切斷（Store 版 Python 的路徑很長）。
  - Python 的程序名稱不一定是 `python.exe`（Microsoft Store 版是 `python3.12.exe`），CIM 查詢要用 `Name LIKE 'python%'`。
  - 從 Claude 的 shell 產生 `.bat` 時，`>nul` 可能被工具環境改寫成 `>/dev/null`；`start_comtool.bat` 要用 Python 寫入並檢查內容（CRLF、純 ASCII）。
- **Telnet 協商**：comd 在 client 連上時主動送 `IAC WILL ECHO, IAC WILL SGA`，並把設備輸出中的 `0xFF` 跳脫成 `0xFF 0xFF`（只對送過 IAC 的 client）。因此三個地方各有一份 telnet 過濾邏輯：`comd.py` 的 `Client.filter`（client→設備）、`comsend` 的 `strip_telnet`、`comtool_mcp.py` 的 `TelnetStripper`（後兩者是設備→client，後者可跨 recv 邊界）。hub 的 attach 只是原樣轉送，不處理 telnet。
- **預設值重複出現**：baud 預設 115200 寫在 `comd.py`、`bridge.ps1`、`comhub.py`（`open`）、`comtool_mcp.py`（tool schema）、`INSTALL.md` 的驗證說明；TCP port 預設 `7000 + COM 編號` 寫在 comd、comsend、comhub 三處；hub 的 TCP port 7000 寫在 `hubclient.py`（`HUB_TCP_PORT`，comhub 引用）、`INSTALL.md`、`README.md`。`comhub.ini` 的欄位（`[ssh] user/host/repo`、`[hub] tftp_root`）寫在 `comhub.py`（`load_ini()`、`INI_TEMPLATE`）、`INSTALL.md` 步驟 8、`README.md` A 節；TFTP 根目錄的優先順序（`--tftp-root` → ini → `COMTOOL_TFTP_ROOT` → tftpd64.ini）寫在 `comhub.main()` 與 README。

### comtool_mcp.py

- 手寫的 stdio JSON-RPC（一行一個訊息），不是用 SDK。**stdout 只能輸出協定訊息**，除錯訊息請用 `log()` 寫到 stderr。
- 新增工具：寫 `t_<name>(args)` 回傳字串，在 `TOOLS` 加一筆 `(name, fn, description, inputSchema)`。丟出例外會自動轉成 `isError: true` 的回應。
- 所有操作都透過 `hubclient`（`request()`、`attach()`、`put_file()`）；hub 連不上時的自動啟動與錯誤提示都在 `hubclient.connect()`。
- 每個 COM port 一個 `Conn`（包一條 attach 連線）：背景執行緒收資料到 `buf`（上限 1 MB），位置用絕對 offset（`base`、`read_pos`）。`com_send` 會回傳送出前還沒讀過的輸出，但 `expect` 只比對送出之後的資料。
- comd 由 hub 啟動（Linux 用 `start_new_session`，Windows 用 `CREATE_NO_WINDOW`），**不會**跟著 MCP server 或 hub 結束；只有 `com_close`（`stop_daemon=true`）會請 hub 結束它。
- 回傳格式第一行是 `[狀態 秒數]`，`com_send`／`com_read` 的狀態為 `matched`／`timeout`／`idle`／`closed`，`com_put_file` 為 `uploaded`／`same`（README 有說明，改動時要同步）。

## 操作實體設備時的注意事項

- 接的設備與 baud rate 因使用者環境而異（不寫在 repo 文件裡），開啟前先確認；預設 115200，收到亂碼或只有 `0x00` 就換 baud rate 再試。
- 送 `logout` 前先確認設備停在提示字元，否則會被當成帳號輸入。
- 設備帳密不要寫進 repo 的任何檔案。`log/` 已列在 `.gitignore`。
