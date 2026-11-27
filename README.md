# comtool：從 WSL 或 SSH remote 控制 Windows COM port

收送 Windows 端 COM port（序列埠）的資料，並開放 TCP 連線，讓 PuTTY、Windows telnet、腳本可以**同時**連到同一個 console。另外提供 MCP server，讓 Claude Code 可以直接操作 COM port，並把檔案（例如 firmware image）傳到 TFTP 根目錄。

支援兩種工作方式，MCP server 會自動判斷：

| 模式 | Claude Code 執行位置 | COM port 所在 |
|---|---|---|
| **WSL 模式** | 本機 Windows 的 WSL2 | 同一台 Windows |
| **SSH 模式** | build server（用 VSCode Remote-SSH 或一般 ssh 登入都可以） | 你的本機 Windows（不需要 WSL） |

## 為什麼需要這個工具

- WSL2 裡的 `/dev/ttyS*` 是 Linux 核心自己的虛擬裝置，**不會**對應到 Windows 的 COMx（只有 WSL1 才有這種對應）。
- `usbipd-win` 只能把 USB 裝置掛進 WSL，主機板內建的 UART（例如 COM1）沒辦法；而且掛進去之後，Windows 端就不能再用這個裝置。

所以改由 Windows 的 `powershell.exe` 開啟 COM port，再把資料轉出來。這種做法不用在 Windows 安裝任何東西（SSH 模式只需要 Python），也不需要系統管理員權限。

## 架構

```
WSL 模式（全部在同一台 Windows 上）：
  Claude Code ─stdio─ comtool_mcp.py ─unix─▶ <repo>/comtool.sock ◀─ comhub.py（WSL，由 MCP 自動啟動後長駐）
                                                                      ├─▶ comd.py COMx（按需啟動，監聽 127.0.0.1:700N）
                                                                      │     └─stdio─▶ powershell.exe bridge.ps1 ─▶ COMx
                                                                      └─▶ 傳檔：寫入 /mnt/<槽>/<TFTP 根目錄>
  comsend / PuTTY / telnet ─TCP 127.0.0.1:700N─▶ comd.py（可同時連線）

SSH 模式（Claude Code 在 build server，COM port 在本機 Windows）：
  build server                                       本機 Windows
  comtool_mcp.py ─┐                                  comhub.py（Windows 原生 Python，長駐，監聽 127.0.0.1:7000）
  comput ─────────┴─unix─▶ <repo>/comtool.sock         │ ① 啟動並監看 ssh.exe（tunnel）
                    （sshd 依 hub 的 ssh -R 建立）       ▼
                    sshd ◀════ SSH 連線（TCP 22）════ ssh.exe ─TCP─▶ 127.0.0.1:7000（hub）
                                                     hub ├─▶ comd.py COMx（按需啟動，監聽 127.0.0.1:700N）
                                                         │     └─stdio─▶ powershell.exe bridge.ps1 ─▶ COMx
                                                         └─▶ 傳檔：寫入 TFTP 根目錄（例如 D:\fw_image）
                                                     PuTTY / telnet ─TCP 127.0.0.1:700N─▶ comd.py
```

- hub 是長駐的程式，comd 平常不執行：`com_open` 時才由 hub 啟動 comd，comd 再啟動 bridge 開啟 COM port；`com_close` 時由 hub 結束。
- client（MCP、comput）只連 `comtool.sock` 這一個入口，comd 的 `700N` 只在 hub 那台機器上監聽。傳檔、控制請求、多個 COM port 的 console 都送到 hub，由 hub 依請求內容分派；SSH 模式下它們是同一條 SSH 連線裡的不同 channel。

各程式在兩種模式下的執行位置與職責：

| 角色 | WSL 模式 | SSH 模式 | 職責 |
|---|---|---|---|
| MCP（comtool_mcp.py）／comput | WSL（Linux） | build server（Linux） | client，連 `comtool.sock` 發出請求。兩者都透過共用模組 `hubclient.py` 連 hub（不是獨立執行的程式） |
| sshd | — | build server | 依 ssh.exe 的請求建立 `comtool.sock`；接起上面的連線，在 SSH 連線上開 channel |
| ssh.exe | — | 本機 Windows，由 hub 啟動 | 連到 build server，請 sshd 建立 `comtool.sock` 入口，再把連進來的連線送回 hub |
| hub（comhub.py） | WSL（Linux 的 Python），由 MCP 自動啟動後長駐；自己監聽 `comtool.sock` | 本機 Windows（Windows 原生 Python），需要時雙擊 `start_comtool.bat` 啟動，之後長駐；監聽 `127.0.0.1:7000` | 依請求啟動／結束 comd、轉送 console、接收傳檔；SSH 模式下還要執行並監看 ssh.exe（tunnel） |
| comd.py | WSL（Linux 的 Python），由 hub 按需啟動 | 本機 Windows（Windows 原生 Python），由 hub 按需啟動 | 監聽 `127.0.0.1:700N`，啟動 bridge，廣播 console 資料並寫 log |
| bridge.ps1（powershell.exe） | 本機 Windows（comd 透過 WSL interop 啟動，是 Windows 程序） | 本機 Windows，由 comd 啟動 | 實際開啟 COM port 的程式 |

| 檔案 | 說明 |
|---|---|
| `bridge.ps1` | Windows 端腳本。開啟 COM port，透過 stdin/stdout 收送原始位元組。 |
| `comd.py` | daemon（WSL 或 Windows 原生 Python 都能跑）。啟動 `bridge.ps1`，監聽 TCP，把 COM port 收到的資料廣播給所有 client 並寫入 log。 |
| `comhub.py` | hub（WSL 或 Windows 原生 Python 都能跑）。管理 comd 的啟動與結束、把 console 轉給 MCP、接收傳檔寫入 TFTP 根目錄。 |
| `start_comtool.bat` | SSH 模式在本機 Windows 啟動 hub 的批次檔，需要時雙擊執行。沒有個人設定，大家共用。 |
| `comhub.ini` | SSH 模式的個人設定（build server 的帳號、IP、repo 路徑、TFTP 根目錄），由 Claude 安裝時產生，`comhub.py` 讀取。每個人都不同，列在 `.gitignore`。 |
| `hubclient.py` | 共用模組（不是獨立執行的程式）。`comtool_mcp.py`、`comput` import 它來連 hub，負責自動判斷模式與錯誤提示；`comhub.py` 也用它的協定部分。 |
| `comsend` | 輔助工具（除錯用，見「輔助：除錯與自動化」）。送出一行指令，等幾秒後印出回應（直接連 comd，SSH 模式下在 build server 上不能用）。 |
| `comput` | 輔助工具（自動化用，見「輔助：除錯與自動化」）。把檔案傳到 hub 的 TFTP 根目錄，功能和 MCP 的 `com_put_file` 相同。 |
| `comtool_mcp.py` | MCP server，給 Claude Code 使用（見下方「MCP server」段落）。只用 Python 標準函式庫。 |
| `INSTALL.md` | 安裝指南：WSL 模式、SSH 模式（build server 端與 Windows 端）；可請 Claude Code 依指南安裝。 |
| `comtool.sock` | MCP 連 hub 的入口（Unix Domain Socket，權限 0600）。WSL 模式由 hub 建立；SSH 模式由 sshd 依 hub 的 `ssh -R` 請求建立。 |
| `log/COMx.log` | COM port 收到的所有原始資料（附加寫入）。在 comd 執行的機器上。 |
| `log/COMx.pid` | comd 執行中時存在，內容為 `<pid> <tcp_port> <baud>`；comd 結束時自動刪除。 |
| `log/COMx.comd.out` | 由 hub 啟動的 comd 的執行訊息（READY、client 連線紀錄、錯誤訊息）。 |
| `log/comhub.log` | hub 的執行紀錄（啟動、comd 啟動／結束、傳檔，SSH 模式下還有 tunnel 的建立、斷線、重連）。 |

## 連線步驟與流程（SSH 模式）

各程式的角色見上方「架構」的表格。重點：

- 本機的 hub 先啟動並**長駐**，負責兩件事：建立並維持到 build server 的 SSH tunnel，以及等待 remote 的請求。
- tunnel 由 hub 自己執行 `ssh -R` 建立，**跟 VSCode 無關**：沒開 VSCode 也能在 build server 上用 comtool（例如另開終端機 ssh 登入後執行 `comput`，或使用 Claude Code CLI）；VSCode 斷線也不會影響 tunnel。
- comd 平常不執行，`com_open` 時才由 hub 啟動。

連線方向固定是：build server 上的 MCP／comput → sshd → SSH 連線 → 本機 ssh.exe → 本機 hub →（需要時）comd → bridge → COM port。

### A. 一次性設定（見 INSTALL.md）

| 位置 | 設定 |
|---|---|
| 本機 Windows | 能用金鑰登入 build server，不需要輸入密碼。金鑰有 passphrase 的話，要放進 Windows 的 ssh-agent 服務（`ssh-add`）。第一次請先手動 `ssh <帳號>@<IP>` 登入一次，接受 host key（寫進 known_hosts）；之後可以用 `ssh -o BatchMode=yes <帳號>@<IP> true` 確認不需要輸入任何東西 |
| build server（Claude 代勞） | 產生個人設定檔 `comhub.ini`（帳號、IP、repo 路徑由 Claude 自動填好），之後隨 repo 一起複製到 Windows |
| 本機 Windows | `~\.ssh\config` **不要**有 `RemoteForward <repo>/comtool.sock ...`。舊版的設定方式要拿掉，否則 VSCode 連線時會跟 hub 搶同一個入口 |
| build server | 註冊 MCP server（`claude mcp add ...`） |

個人設定放在 `comhub.ini`（每個人都不同，已列在 `.gitignore`，不會進 repo），`start_comtool.bat` 本身沒有任何個人設定，大家共用同一份：

```ini
[ssh]
user = jenda                       ; build server 的帳號
host = 192.168.2.201               ; build server 的 IP（或主機名稱）
repo = /home/jenda/tools/comtool   ; build server 上的 repo 路徑

[hub]
tftp_root =                        ; TFTP 根目錄，留空表示自動讀 tftpd64.ini
```

- Claude 依 INSTALL.md 安裝時，會在 build server 的 repo 目錄產生 `comhub.ini`：帳號取 `$USER`、IP 取 `$SSH_CONNECTION` 的 server 位址、repo 路徑取 repo 的實際路徑。你用 `scp -r` 把 repo 複製到 Windows 時會一起帶過去，**不用自己編輯**；以後更新時也一樣，不會被覆蓋成空白。
- 只想（重新）產生 `comhub.ini` 時，在 build server 上的 Claude Code 輸入「請依照 INSTALL.md 幫我產生 comhub.ini」，產生後重新 `scp -r` 到 Windows。hub 在 Windows 上找不到 `comhub.ini` 時，視窗也會顯示這句提示詞與範本。
- 只有 tftpd64.ini 讀不到時，才需要自己在 Windows 那份 `comhub.ini` 填 `tftp_root`（或請 Claude 在 build server 那份填好再複製）。
- `start_comtool.bat` 只做三件事：找 Python（先試 `py -3`，再試 `python`）、執行 `comhub.py`、hub 結束後 `pause`，讓錯誤訊息留在畫面上。`comhub.py` 會讀同一個目錄的 `comhub.ini`，命令列參數（`--ssh`、`--tftp-root`）優先；找不到 `comhub.ini` 時會顯示要填哪些欄位後結束。
- 帳號與 IP 都在 `comhub.ini` 裡，不需要 ssh config。
- WSL 模式的 hub 會忽略 `comhub.ini` 的 `[ssh]`，只讀 `tftp_root`；repo 裡留著 SSH 模式的 `comhub.ini` 也不影響 WSL 模式。
- 執行後會開一個 console 視窗，顯示 hub 與 tunnel 的紀錄（同時寫進 `log/comhub.log`）。關閉視窗或按 Ctrl+C 就會結束 hub，並一併結束它啟動的 ssh.exe。
- 要同時連多台 build server 時，直接執行 `comhub.py` 並重複指定 `--ssh 帳號@IP:<repo>/comtool.sock`。

### B. 在本機 Windows 雙擊 start_comtool.bat，hub 建立 tunnel

> **要使用 comtool 之前，先在本機 Windows 雙擊 `start_comtool.bat`**，並讓它的視窗保持開啟。這是 SSH 模式唯一需要你手動做的步驟；之後 tunnel 的建立、斷線重建都由 hub 自動處理。
>
> **WSL 模式不需要這個步驟**：WSL 模式沒有 tunnel，MCP 會在需要時自動在背景啟動 WSL 上的 hub。

```
本機 Windows                                                   build server
你 ─雙擊─▶ start_comtool.bat ─▶ python comhub.py（讀 comhub.ini 的帳號、IP、repo 路徑；hub，視窗保持開啟）
hub ─啟動─▶ ssh.exe（背景、不開視窗）══① TCP 22，金鑰登入═════▶ sshd
                    ② 請求：「請在 <repo>/comtool.sock 開一個入口，轉回 127.0.0.1:7000」
                       └─ 失敗（comtool.sock 已存在）→ hub 另開一條 ssh 執行清理程式：
                            連得上，而且收到 hub 的問候訊息 → 其他 hub 正在使用，不動它，稍後再試
                            其他情況（沒有人監聽、沒有回應）→ 視為殘留，刪除後重試 ②
                    ③ 成功：sshd 以你的身分建立 comtool.sock（0600），開始監聽
hub 監看 ssh.exe：每 15 秒送一次 keepalive，連續 3 次沒有回應（約 45 秒）ssh.exe 就結束，
                  hub 再從 ① 重來（間隔從 5 秒開始加倍，最長 60 秒）
```

- ①～③ 和斷線後的重建都是 hub 自動進行的，不需要再操作。
- tunnel 的建立、失敗原因、重連都會顯示在 `start_comtool.bat` 的視窗上，也會寫進 Windows 上的 `log/comhub.log`。
- 關閉 `start_comtool.bat` 的視窗，hub 和 tunnel 就會結束，build server 上就不能再用 comtool；comd 不受影響，會繼續執行。
- ① 失敗時（網路不通、需要輸入密碼）hub 不會卡住（`BatchMode=yes`），會在 log 留下原因，稍後再試。

### C. Claude 操作 COM port 時（MCP 工具 com_*）

`com_list`、`com_open`、`com_send`、`com_read`、`com_log`、`com_close`、`com_put_file` 是 `comtool_mcp.py`（MCP server）提供給 **Claude Code** 使用的工具，不是 shell 指令。你不用自己呼叫它們，只要在 build server 上的 Claude Code 對話中用一般的話描述，例如「開 COM1，baud 9600」「送 show version」「關掉 COM1」，Claude 就會呼叫對應的工具（各工具的參數見「MCP server → 工具」）。

不經過 Claude 時，在 build server 上目前只有 `comput`（傳檔）可以用，它走的路徑跟下圖一樣。

以下是 Claude 每呼叫一次 com_* 工具時，連線經過的路徑：

```
build server                                 本機 Windows
MCP ─① connect comtool.sock─▶ sshd
                               │② 在 SSH 連線上開一個 channel
                               └══════════▶ ssh.exe（由 hub 啟動）─③ connect 127.0.0.1:7000─▶ hub
MCP ◀════════════════ ④ hub 送出問候訊息 ═════════════════════════════════════ hub
MCP ═════════════════ ⑤ 送出請求（list / open / put / attach …）═══════════▶ hub ─▶ comd（127.0.0.1:700N）／TFTP 目錄
MCP ◀════════════════ ⑥ 回應 ═════════════════════════════════════════════════ hub
```

- ① 之前，MCP 會先檢查 `comtool.sock` 的擁有者是不是自己。
- ③ 失敗時（Windows 上的 hub 沒有執行），ssh.exe 會關閉這個 channel，MCP 會回報「本機 Windows 的 comhub.py 沒有在執行」。
- 控制請求（list、open、close、log、put）在 ⑥ 之後就關閉連線。attach（`com_open` 建立）會保持連線，之後這條連線傳的就是 console 的原始資料。
- 同時有多個請求（例如 attach COM1、attach COM3、傳檔）時，每個都是同一條 SSH 連線裡各自的 channel，互不影響。

### C2. com_open／com_close 時 hub 與 comd 的動作

```
com_open COM1：
MCP ─open─▶ hub ─① 啟動─▶ comd.py COM1 ─② 監聽 127.0.0.1:7001
                                      └─③ 啟動─▶ powershell bridge.ps1 ─④ 開啟 COM1，stderr 輸出「OPENED」
                             comd ⑤ 寫入 log/COM1.pid
       hub ⑥ 看到 pidfile，回應 ok
MCP ─attach─▶ hub ─⑦ connect 127.0.0.1:7001─▶ comd   （之後 hub 在兩條連線之間原樣轉送）

com_close COM1：
MCP ─close─▶ hub ─① 結束 comd → bridge 的 stdin 被關閉 → bridge 關閉 COM1 後結束
                 ② 刪除 pidfile
                 ③ 找出並結束殘留的 comd／bridge（這個 repo 的、COM1 的）
```

- comd 已經在執行時（例如上次沒有 `com_close`，或你手動執行的 comd），`com_open` 會跳過 ①～⑥，直接 attach。
- ④ 失敗時（COM port 被占用），comd 會結束，hub 回傳錯誤：
  - 有 comtool 自己殘留的 comd／bridge 時，錯誤訊息會列出它們的 pid，請先 `com_close` 清除再開。
  - 沒有的話，就是被其他程式占用（例如 Tera Term、PuTTY 直接開 COM port），請關閉該程式後再開。hub 不會結束其他程式。
- 同一個 COM port 只會有一個 comd；Windows 上的 PuTTY／telnet 可以同時連 `127.0.0.1:700N`，跟 MCP 看到同一個 console。

### D. 斷線、重連時

- SSH 連線斷掉時，所有經過 tunnel 的連線（包括 attach）都會中斷，MCP 的 `com_send` 會回傳 `closed`。本機的 hub 和 comd 不受影響，comd 也會繼續寫 log。
- hub 會自動重建 tunnel（B）。sshd 斷線時不會刪除 `comtool.sock`（`StreamLocalBindUnlink` 預設是 `no`），所以第一次重建通常會失敗，hub 會用清理程式刪掉殘留的 socket 再重試，**不需要你做任何操作**。
- tunnel 重建後，呼叫 `com_open` 就會重新 attach。
- MCP 如果剛好在重建期間連線，會收到「comtool.sock 沒有人監聽」之類的錯誤（MCP 也會順手刪除殘留的 socket），等幾秒再試即可。
- **可選**：請 build server 管理員在 `/etc/ssh/sshd_config` 加上下面這行並 reload sshd。sshd 建立入口前會先刪掉舊的 socket，重建時就不用先跑清理程式，會比較快。這個設定只影響 Unix Domain Socket 轉送，對其他人沒有影響。
  ```
  StreamLocalBindUnlink yes
  ```

### WSL 模式（對照）

沒有 B：不需要雙擊 `start_comtool.bat`，也不需要 tunnel。C 也簡化成：MCP 直接連 `<repo>/comtool.sock`，接起連線的就是 WSL 上的 hub，不經過 sshd 和 ssh.exe；hub 沒在執行時，MCP 會自動在背景啟動它。C2 相同。

### 防止誤連

- 每個人的 `comtool.sock` 都在自己 repo 的目錄下，同事的 MCP 只會連到他自己的那一個。
- `comtool.sock` 是 Unix Domain Socket，權限 0600（sshd 建立時就是 0600，WSL 上的 hub 也會設成 0600），其他使用者連線時會被拒絕（Permission denied）。
- MCP 連線前也會檢查 `comtool.sock` 的擁有者是自己。
- hub 的清理程式只刪除擁有者是自己、而且沒有 hub 回應的 socket，不會搶走另一個正在使用中的 hub（例如你另一台電腦上的 hub）的入口。
- 這只防誤用，不防刻意的攻擊（例如 build server 的 root）。如果改用 `COMTOOL_HUB=tcp:<port>` 轉送 TCP port，build server 上任何人都連得到，就沒有這層保護。

## 安裝

請見 [INSTALL.md](INSTALL.md)。repo 放在哪裡都可以；最快的方式是在 repo 目錄開啟 Claude Code，輸入：

```
請依照 INSTALL.md 幫我安裝 comtool
```

Claude 會在它執行的那台機器上（WSL，或 build server）完成檢查環境、設定執行權限、註冊 MCP server，需要選擇的地方會先問你。

### ⚠ 需要你手動做的事（Claude 無法代勞）

Claude 碰不到它執行環境以外的地方，也不能重開自己的 session，所以下列步驟**一定要你自己做**。Claude 依 INSTALL.md 安裝時，會在最後逐項提醒你；詳細步驟見 INSTALL.md 標示「👤 使用者手動」的地方。

**WSL 模式**

| 時機 | 你要做的事 |
|---|---|
| 安裝或更新 `comtool_mcp.py`／`hubclient.py` 後 | **重開 Claude Code 的 session**，`com_*` 工具才會出現或更新 |
| 只有環境檢查失敗時 | 修改 `/etc/wsl.conf`（interop）或 `.wslconfig`（localhostForwarding）後，在 Windows 執行 `wsl --shutdown` 重新啟動 WSL（會中斷 WSL 裡的 Claude） |

除此之外不需要手動設定：Windows 端不用安裝任何東西，hub 也由 MCP 自動啟動。

**SSH 模式**

| 時機 | 你要做的事（都在**本機 Windows**） |
|---|---|
| 第一次 | 安裝 Windows 版 Python 3.8 以上 |
| 第一次、每次更新 | 把 repo 複製到 Windows（`scp -r`，指令見 INSTALL.md）；Claude 產生的個人設定 `comhub.ini` 會一起帶過去 |
| 只有 tftpd64.ini 讀不到時 | 在 Windows 那份 `comhub.ini` 填 `tftp_root` |
| 第一次 | 設定用金鑰登入 build server：先手動 `ssh 帳號@IP` 登入一次接受 host key；金鑰有 passphrase 的話要啟用 ssh-agent 並 `ssh-add` |
| 第一次（從舊版升級時） | 拿掉 `~\.ssh\config` 裡舊的 `RemoteForward .../comtool.sock` |
| **每次要用 comtool 之前** | **雙擊 `start_comtool.bat`，並讓視窗保持開啟** |
| 安裝或更新 `comtool_mcp.py`／`hubclient.py` 後 | **重開 Claude Code 的 session**（在 build server 上） |

## 使用方式

comtool 主要是給 Claude Code 使用的工具：

1. **SSH 模式**：先在本機 Windows 雙擊 `start_comtool.bat`（見「B.」）。WSL 模式不需要這一步。
2. 在 Claude Code 對話中用一般的話請 Claude 操作，例如「列出 COM port」「開 COM1，baud 9600」「送 show version」「把 build/fw.bin 傳到 TFTP」「關掉 COM1」。Claude 會呼叫 MCP 的 `com_*` 工具（見「MCP server」）。

comd、comsend、PuTTY、comput 這些不經過 Claude 的操作是輔助用途，見後面的「輔助：除錯與自動化」。

## 傳檔（TFTP）

設備用 TFTP 從本機的 TFTP server（例如 tftpd64）下載 firmware 時，可以用 MCP 的 `com_put_file` 或 `comput` 把檔案放進 TFTP 根目錄。檔案由 hub 寫入，所以 SSH 模式下也能從 build server 直接傳到本機 Windows。

流程：

1. client 先算出檔案大小和 md5，送給 hub。
2. TFTP 根目錄已有同名而且 md5 相同的檔案時，hub 回 `same`，不重傳。
3. 否則 hub 把資料寫到 `<檔名>.part`，收完比對大小和 md5，一致才換成正式檔名。設備不會抓到寫一半的檔案。
4. 回傳結果，第一行是 `[uploaded 秒數]` 或 `[same 秒數]`，接著是來源 → 目的路徑、大小、md5。

限制：

- 檔名只能用英數字與 `. _ -`，不能以 `.` 開頭，最長 128 字元；只能寫進 TFTP 根目錄本身，不能指定子目錄。
- 大小上限預設 256 MB（hub 的 `--max-size`，單位 MB）。
- Windows 上如果 tftpd64 正在傳送同名的舊檔案，覆蓋可能失敗（hub 會重試幾次）；請不要在設備下載途中重傳同名檔案。

TFTP 根目錄依下列順序決定，hub 啟動時會寫進 `log/comhub.log`，`com_list` 第一行也會顯示：

1. hub 的 `--tftp-root` 參數
2. `comhub.ini` 的 `tftp_root`（主要用於 SSH 模式；WSL 模式有這個檔案時也會讀）
3. 環境變數 `COMTOOL_TFTP_ROOT`
4. tftpd64 的設定檔 `tftpd64.ini` 的 `BaseDirectory`（在 `Program Files\Tftpd64` 等常見位置自動尋找）

WSL 上的 hub 收到 Windows 路徑（例如 `D:\tftp`）時，會自動轉成 `/mnt/d/tftp`。

## MCP server（給 Claude Code 使用）

`comtool_mcp.py` 是一個 stdio MCP server。Claude Code 啟動它之後，它會透過 hub 對每個 COM port 維持一條**持續連線**，Claude 不用再透過 Bash 執行 `comsend`、固定等幾秒，而是送出指令後**等到提示字元出現就回傳**，回傳的內容也已經去掉 ANSI 碼。

MCP 一律連 `<repo>/comtool.sock`，用下面的順序自動判斷模式：

1. 連得上，而且收到 hub 的問候訊息 → 直接使用（SSH 模式，或 WSL 上 hub 已在執行）。
2. 連不上，但本機有 `powershell.exe`（WSL） → 在背景啟動本機的 hub，再連一次。
3. 都不是 → 回傳錯誤，並說明原因（轉送沒有建立、殘留的 socket、本機 hub 沒有執行等，見疑難排解）。

要連到其他位置的 hub 時，可以設定環境變數 `COMTOOL_HUB=unix:PATH` 或 `COMTOOL_HUB=tcp:PORT`。

### 註冊

依 [INSTALL.md](INSTALL.md) 安裝時會註冊（建議使用者層級，所有專案都能用）。手動註冊的方式如下：

```bash
claude mcp add -s user comtool -- "$(command -v python3)" <repo 路徑>/comtool_mcp.py
```

- `claude` 不在 PATH 上時，請改用 VS Code 擴充套件內附的執行檔，例如 `~/.vscode-server/extensions/anthropic.claude-code-<版本>-linux-x64/resources/native-binary/claude`；其他可能的位置見 INSTALL.md。
- 註冊或修改 `comtool_mcp.py` 之後，要**重開 Claude Code 的 session** 才會生效。
- 查看狀態：在 Claude Code 對話框輸入 `/mcp`，或在終端機執行 `claude mcp list`。VS Code 內建的「MCP SERVERS」清單是 VS Code／Copilot 自己的設定，看不到 comtool。

### 工具

| 工具 | 主要參數 | 功能 |
|---|---|---|
| `com_list` | — | 第一行是 hub 的位置與 TFTP 根目錄；接著列出 Windows 的 COM port，以及各 port 的 comd 是否執行中、MCP 是否已連線 |
| `com_open` | `port`、`baud`=115200、`tcp_port` | comd 沒在執行就由 hub 啟動（等到 READY，最多 15 秒），然後建立 MCP 連線；已在執行就直接連線 |
| `com_send` | `port`、`text`、`expect`、`timeout`=10、`idle`=0.5、`newline`=true、`raw`=false、`strip_ansi`=true | 送出文字（預設加 CR）。有 `expect`（regex）就等到符合或逾時；沒有就等到 `idle` 秒內沒有新資料 |
| `com_read` | `port`、`expect`、`timeout`=0 | 不送任何東西，讀取還沒讀過的輸出；可等待某段文字出現（例如看開機 log） |
| `com_log` | `port`、`bytes`=4000 | 讀 `log/COMx.log` 的最後一段，可看到 MCP 連線之前的輸出 |
| `com_close` | `port`、`stop_daemon`=true | 中斷 MCP 連線；預設一併結束 comd，釋放 COM port |
| `com_put_file` | `path`、`name` | 把檔案傳到 hub 的 TFTP 根目錄（見「傳檔（TFTP）」）；`path` 是 MCP 所在機器上的路徑 |

`com_send`、`com_read` 回傳內容的第一行是 `[狀態 秒數]`，狀態有四種（`com_put_file` 的狀態是 `uploaded`／`same`）：

| 狀態 | 意思 |
|---|---|
| `matched` | 符合 `expect`；符合後會再多收 0.15 秒內接著到的資料，避免輸出被切成兩段 |
| `timeout` | 逾時，`expect` 沒有符合 |
| `idle` | 沒有指定 `expect`，而且一段時間內沒有新資料 |
| `closed` | 與 comd 的連線中斷了（comd 結束，或 SSH 斷線） |

使用注意：

- `com_send` 的回傳會**包含送出前還沒讀過的輸出**（例如你從 telnet 操作的內容），不會漏掉；但 `expect` 只比對送出之後收到的資料。
- 使用 `raw=true` 可以送控制字元，例如 `\x03`（Ctrl+C）、`\x11`（Ctrl+Q），或分頁時要按的 `g`、空白鍵。
- 去掉 ANSI 碼的規則：游標定位碼換成換行，其他 CSI 序列刪除，`\r` 也刪除。這樣全螢幕的登入畫面也能讀成一行一行的文字。

### 生命週期

- MCP server 跟著 Claude Code 的 session 啟動和結束；結束時只會關閉自己的連線。
- **comd 由 hub 按需啟動與結束**，不會跟著 Claude Code 或 hub 結束。所以你的 PuTTY／telnet 不會斷線，log 也會繼續記錄；但用完要記得呼叫 `com_close`（或手動結束 comd），否則 COM port 會一直被佔用。
- `com_close` 除了結束 comd，也會找出並結束這個 COM port 殘留的 comd／bridge（只限這個 repo 目錄下的 comd.py、bridge.ps1），例如 pidfile 被刪掉後還在執行的 comd。
- hub 長駐：在 WSL 模式由 MCP 在背景啟動，之後一直執行；在 SSH 模式由你雙擊 `start_comtool.bat` 啟動，執行期間負責維持 tunnel（斷線自動重建），關閉視窗就結束。
- SSH 模式的 hub 結束時，會一併結束它啟動的 ssh.exe。hub 如果是被強制結束（例如工作管理員），ssh.exe 可能還在，仍然會把連線轉到 `127.0.0.1:7000`；新的 hub 啟動後可以直接接手，不影響使用。新的 hub 的清理程式會發現入口已經連到自己（問候訊息裡帶有 hub 的識別碼），就不會重複建立 tunnel，只會定期確認入口還在。
- 你手動執行的 `comd` 也會寫 pidfile，所以 `com_list`、`com_open`、`com_close` 一樣看得到、管得到它（要在 hub 同一台機器、同一個 repo 目錄）。

## 輔助：除錯與自動化（不經過 Claude）

以下不是主要的使用方式，只在下列情況使用：

- **comd＋comsend**：不經過 hub 和 MCP，單獨測試 comd（例如修改 comd 之後，確認問題出在 comd 還是 hub／MCP）。
- **PuTTY／telnet**：在本機 Windows 跟 Claude 一起看、一起操作同一個 console。
- **comput**：給 build script 等自動化使用（例如編譯完自動把 firmware 放進 TFTP 根目錄），功能和 MCP 的 `com_put_file` 相同。

兩種模式下，每個操作要在哪裡執行並不一樣，容易搞混，請先看這張表：

| 操作 | WSL 模式 | SSH 模式 |
|---|---|---|
| 1. 手動啟動 daemon（comd） | **WSL 的 Linux 終端機**：在 repo 目錄執行 `./comd.py COM3` | **本機 Windows 的 PowerShell**：`python comd.py COM3`（在 `%LOCALAPPDATA%\comtool` 下執行）。**不能在 build server 上執行**，build server 沒有 COM port |
| 2. comsend 送指令 | WSL 的 Linux 終端機（`./comsend COM3 "" 1`） | 本機 Windows（`python comsend COM3 "" 1`）；build server 上不能用 |
| 3. PuTTY／telnet 一起操作 | 本機 Windows | 本機 Windows |
| 4. 查看 log | WSL：`<repo>/log/COM3.log` | 本機 Windows：`%LOCALAPPDATA%\comtool\log\COM3.log`（log 在 comd 執行的機器上，不在 build server） |
| 5. comput 傳檔 | WSL 的 Linux 終端機 | **build server**（要先在本機 Windows 雙擊 `start_comtool.bat`，見「B.」） |

- 1～4 是直接操作 comd，所以要在 comd 執行的機器上做：WSL 模式是 WSL，SSH 模式是本機 Windows。
- 5 是經由 hub 操作：WSL 模式在 WSL 上，SSH 模式在 build server 上。SSH 模式下，只要本機的 hub 在執行、tunnel 已建立，用任何方式登入 build server（VSCode、一般 ssh 終端機）都能使用。
- 平常用 Claude Code 時，`com_open` 會請 hub 自動啟動 comd，**不需要手動執行 1**。手動執行 comd 只用在不經過 Claude、自己用 PuTTY 操作的情況。

### 1. 啟動 daemon

**WSL 模式：在 WSL 的 Linux 終端機、repo 目錄下執行**

```bash
./comd.py COM3                 # 預設 115200 baud，監聽 127.0.0.1:7003
./comd.py COM1 --baud 9600     # 指定 baud rate
./comd.py COM3 --port 8003     # 指定 TCP port（預設為 7000 + COM 編號）
```

**SSH 模式：在本機 Windows 的 PowerShell 執行**（不是在 build server 上）

```powershell
cd $env:LOCALAPPDATA\comtool
python comd.py COM3            # 參數與 WSL 模式相同
python comd.py COM1 --baud 9600
```

兩種模式的 daemon 都會一直在前景執行，開啟成功時會印出：

```
READY COM3@115200 tcp://127.0.0.1:7003
```

之後 client 連線或離線時，也會印出一行訊息。要結束時按 `Ctrl+C`，或送 SIGTERM，COM port 會隨之釋放。

> **注意**：daemon 執行期間，COM port 會被佔用，Tera Term／PuTTY **不能**直接開啟該 COM port。要直接用 COM port 時，請先結束 daemon；改連 TCP port 的話，則可以跟 daemon 同時使用。

### 2. 用 comsend 送指令

在 repo 目錄執行（SSH 模式在本機 Windows 改用 `python comsend ...`）：

```bash
./comsend COM3 "show system-info" 2   # 送出指令並自動加 CR，等 2 秒後印出回應
./comsend COM3 "" 1                   # 只按一次 Enter
./comsend --raw COM3 '\x11' 1         # 原樣送出（不加 CR），可用 \xNN 表示控制字元
```

- 等待秒數預設為 1 秒，輸出較多的指令（例如開機、`show config`）請自行加長。
- 輸出保留設備送出的 ANSI 跳脫碼，只拿掉 `\r`。要看純文字的話，可以這樣過濾：
  ```bash
  ./comsend COM3 "show config" 3 | sed 's/\x1b\[[0-9;]*[A-Za-z]/\n/g'
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
- 請用 `127.0.0.1`，不要用 `localhost`。`localhost` 會先解析成 IPv6 的 `::1`，但 comd 只監聽 IPv4（WSL 也只轉發 IPv4 到 Windows），所以會連不上。
- Backspace 請用 Control-H：這台設備的 CLI 不認 DEL（`0x7F`），會直接顯示 `^?`。

所有 client 看到的都是同一個 console：任何一方輸入的內容都會送到設備，設備的輸出也會送給每個 client。

### 4. 查看 log

```bash
tail -f <repo>/log/COM3.log                                      # WSL 模式：在 WSL 執行
```

```powershell
Get-Content -Wait $env:LOCALAPPDATA\comtool\log\COM3.log         # SSH 模式：在本機 Windows 執行
```

在 build server 上可以用 MCP 的 `com_log` 讀取（經由 hub）。不管當時有沒有 client 連線，daemon 都會把收到的資料寫進 log，所以設備重開機時的輸出不會漏掉。log 只會一直附加寫入，需要時請自行清理。

### 5. 傳檔到 TFTP 根目錄（comput）

```bash
<repo>/comput build/fw.bin              # 傳到 hub 的 TFTP 根目錄，保留原檔名
<repo>/comput build/fw.bin fw_test.bin  # 改用新檔名
```

在 WSL 或 build server 上都能用（跟 MCP 一樣經由 hub）。詳見「傳檔（TFTP）」。

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
| 啟動時出現 `OPEN FAIL: ... 拒絕存取` | COM port 被占用。`com_open` 的錯誤訊息如果列出殘留的 comd／bridge pid，先 `com_close` 清除再開；沒有列出的話，就是被其他程式（Tera Term、PuTTY 直接開 COM port）占用，請先關掉該程式。 |
| 啟動時出現 `TCP port 700N 無法使用` | 已經有另一個 comd 在執行（錯誤訊息會列出殘留的 pid，先 `com_close`），或該 port 被其他程式占用。 |
| 收到亂碼，或只收到 `0x00` | baud rate 不對（預設是 115200），請用 `--baud` 試試其他速率（9600、19200、38400、57600）。 |
| WSL 裡找不到或無法執行 `powershell.exe` | WSL interop 沒有開啟，請檢查 `/etc/wsl.conf` 的 `[interop]` 設定，然後在 Windows 執行 `wsl --shutdown` 重新啟動 WSL。 |
| `comsend` 顯示「無法連到 COMx」 | comd 沒有在執行，或 TCP port 不是預設值（請設定 `COMSEND_PORT`）。 |
| PuTTY 看不到自己輸入的字 | 請確認 Connection type 選 Telnet，且 Terminal → Local echo 維持 Auto。 |
| Windows telnet 連不上 | 請確認用的是 `telnet 127.0.0.1 7003`（用空白隔開、用 127.0.0.1）。 |
| 結束 daemon 後，COM port 仍被佔用 | 可能殘留 comd 或 `powershell.exe`（bridge）程序。依序處理：① 呼叫 `com_close`，它會清除這個 COM port 殘留的 comd／bridge。② 還是不行的話，在 Windows PowerShell 執行 `Get-CimInstance Win32_Process \| Where-Object { $_.CommandLine -match 'bridge\.ps1\|comd\.py' } \| Select-Object ProcessId, CommandLine` 找出來，再 `Stop-Process -Id <pid>`。③ USB 轉 serial 在開啟狀態下被拔掉時，bridge 可能卡在驅動程式裡結束不了，請重新插拔 USB。 |
| Claude Code 裡看不到 `com_*` 工具 | 剛註冊或剛修改 `comtool_mcp.py`，請重開 session；用 `/mcp` 確認 comtool 是不是 connected。 |
| `com_open` 回傳「comd 啟動失敗」 | 錯誤訊息會附在回傳內容裡，完整紀錄在 `log/COMx.comd.out`；常見原因是 COM port 被其他程式佔用。 |
| `com_send` 一直 `timeout` | `expect` 沒有比對到，例如大小寫不同（`-- more --` 是小寫），或設備停在分頁提示。用 `com_read` 看目前收到的內容。 |
| `com_list` 顯示 comd 執行中，但其實已經結束 | `com_list` 會自動清掉殘留的 pidfile；如果仍然異常，請手動刪除 `log/COMx.pid`。 |
| 「找不到 `<repo>/comtool.sock`」 | SSH 模式：tunnel 沒有建立。確認本機 Windows 的 hub 有執行（`start_comtool.bat` 的視窗還開著），而且 `comhub.ini` 的 `user`、`host`、`repo` 設定正確（repo 要跟 build server 上的 repo 位置一致）；原因看 Windows 上的 `log/comhub.log`。 |
| 「comtool.sock 是上次斷線留下的殘留檔，已刪除」 | SSH 斷線後、hub 還沒重建 tunnel 前的正常情況（見「D. 斷線、重連時」）。等幾秒再試即可。 |
| 「連上 comtool.sock 但馬上被斷線」 | 入口還在，但本機 Windows 的 comhub.py 沒有在執行。通常是 hub 被強制結束後留下的 ssh.exe，或 ssh config 還留著舊的 `RemoteForward`（入口由 VSCode 的連線建立）。請拿掉 RemoteForward，重新執行 `start_comtool.bat`；錯誤原因看 Windows 上的 `log/comhub.log`。 |
| 「連上後等不到 hub 回應」，並提到另一個 sshd 的 pid | 舊的 SSH 連線已經斷了，但 build server 上的 sshd 還沒發現，仍占著 `comtool.sock`。hub 的清理程式會在重建 tunnel 時刪掉它；也可以依提示 `kill <pid>`。 |
| `comhub.log` 出現 `Permission denied (publickey...)`、`passphrase`，tunnel 一直建立失敗 | hub 在背景執行 ssh，不能輸入密碼或 passphrase。請設定金鑰登入；金鑰有 passphrase 的話，啟用 Windows 的「OpenSSH Authentication Agent」服務並執行 `ssh-add`。用 `ssh -o BatchMode=yes <帳號>@<IP> true` 確認不需要輸入任何東西；出現 `Host key verification failed` 時，先手動 `ssh <帳號>@<IP>` 登入一次接受 host key。 |
| `comhub.log` 出現「其他 hub 正在使用 comtool.sock」 | 你在另一台電腦上的 hub 已經建立了同一個入口。同一個 repo 路徑同時只能給一個 hub 使用；不用的那台請停掉 hub，或改用不同的 repo 路徑。 |
| VSCode「輸出 → Remote - SSH」出現 `remote port forwarding failed` | ssh config 還留著舊的 `RemoteForward <repo>/comtool.sock ...`。現在 tunnel 由 hub 建立，請把它拿掉。 |
| `com_put_file` 回傳「TFTP 根目錄未設定」 | hub 找不到 tftpd64.ini，SSH 模式請在 `comhub.ini` 填 `tftp_root`；WSL 模式請設定環境變數 `COMTOOL_TFTP_ROOT`（或 `comhub.py --tftp-root <路徑>`），然後重新啟動 hub。 |
| `com_put_file` 回傳「被其他程式使用中」 | tftpd64 正在傳送同名的檔案給設備。等傳完再重傳，或改用其他檔名。 |

## 安全性

- TCP 只監聽 `127.0.0.1`，區域網路上的其他電腦連不進來。
- comd 與 Windows hub 的 TCP 連線**沒有驗證**：本機任何程式都能連上並操作設備的 console，或寫檔到 TFTP 根目錄（只能寫進根目錄本身，檔名有限制）。
- SSH 模式下，build server 上的入口是 Unix Domain Socket（0600），其他使用者連不到（見「防止誤連」）；資料經過 SSH 加密。
- 設備的密碼或金鑰不要寫進這個目錄的任何檔案。
- 透過 MCP 的 `com_send` 送出的密碼會留在 Claude Code 的對話紀錄裡（設備畫面上只會顯示 `*`）。
