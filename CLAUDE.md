# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

從 WSL2 控制 Windows COM port 的工具組。WSL2 的 `/dev/ttyS*` 不對應 Windows COMx，所以由 `powershell.exe` 在 Windows 端開 port，再透過 stdio 轉回 WSL。只用 Python 標準函式庫與 Windows 內建的 PowerShell 5.1，**不要引入第三方套件**（包括 MCP SDK、pyserial）。

使用者文件在 [README.md](README.md)（繁體中文）；改動行為、參數或預設值時要同步更新 README。

## 開發與驗證

沒有建置流程、測試套件或 lint 設定。常用的檢查方式：

```bash
python3 -m py_compile comd.py comtool_mcp.py comsend   # 語法檢查
bash -n install.sh

# 不接硬體，直接測 MCP server 的 JSON-RPC
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 comtool_mcp.py

# 接硬體的端到端測試
./comd.py COM3                 # 前景執行，看到 READY 表示成功
./comsend COM3 "" 1            # 另一個終端機送一次 Enter
```

修改 `comtool_mcp.py` 後，要重開 Claude Code session，`com_*` 工具才會載入新版本。

## 架構

```
comtool_mcp.py / comsend / PuTTY ──TCP 127.0.0.1:7000+N──▶ comd.py ──stdio──▶ powershell.exe bridge.ps1 ──▶ COMx
```

各元件之間的約定（改一邊時要檢查另一邊）：

- **bridge.ps1 ↔ comd.py**：stdout／stdin 是原始位元組；stderr 是控制訊息。`OPENED ...` 表示開啟成功，`OPEN FAIL: ...` 後以 exit 2 結束。comd 關閉 stdin 時 bridge 會結束並釋放 COM port。
- **comd.py 的 pidfile**：收到 `OPENED` 後才寫 `log/COMx.pid`，內容是 `<pid> <tcp_port> <baud>`，結束時由 atexit 刪除（只刪自己 pid 的檔案）。`comtool_mcp.py` 的 `daemon_info()` 以這個檔案判斷 comd 是否執行中、該連哪個 TCP port；`com_open` 也是輪詢它來等 READY。
- **Telnet 協商**：comd 在 client 連上時主動送 `IAC WILL ECHO, IAC WILL SGA`，並把設備輸出中的 `0xFF` 跳脫成 `0xFF 0xFF`（只對送過 IAC 的 client）。因此三個地方各有一份 telnet 過濾邏輯：`comd.py` 的 `Client.filter`（client→設備）、`comsend` 的 `strip_telnet`、`comtool_mcp.py` 的 `TelnetStripper`（後兩者是設備→client，後者可跨 recv 邊界）。
- **預設值重複出現**：baud 預設 115200 寫在 `comd.py`、`bridge.ps1`、`comtool_mcp.py`（`t_com_open` 與 tool schema）、`install.sh` 的提示訊息；TCP port 預設 `7000 + COM 編號` 寫在 comd、comsend、MCP 三處。

### comtool_mcp.py

- 手寫的 stdio JSON-RPC（一行一個訊息），不是用 SDK。**stdout 只能輸出協定訊息**，除錯訊息請用 `log()` 寫到 stderr。
- 新增工具：寫 `t_<name>(args)` 回傳字串，在 `TOOLS` 加一筆 `(name, fn, description, inputSchema)`。丟出例外會自動轉成 `isError: true` 的回應。
- 每個 COM port 一個 `Conn`：背景執行緒收資料到 `buf`（上限 1 MB），位置用絕對 offset（`base`、`read_pos`）。`com_send` 會回傳送出前還沒讀過的輸出，但 `expect` 只比對送出之後的資料。
- comd 由 `com_open` 以 `start_new_session=True` 在背景啟動，**不會**跟著 MCP server 結束；只有 `com_close`（`stop_daemon=true`）會送 SIGTERM 結束它。
- 回傳格式第一行是 `[狀態 秒數]`，狀態為 `matched`／`timeout`／`idle`／`closed`（README 有說明，改動時要同步）。

## 操作實體設備時的注意事項

- 已知設備與 baud rate 見 README「已知的設備」。COM3 接的設備會更換，開啟前先確認。
- 送 `logout` 前先確認設備停在提示字元，否則會被當成帳號輸入。
- 設備帳密不要寫進 repo 的任何檔案。`log/` 已列在 `.gitignore`。
