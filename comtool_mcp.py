#!/usr/bin/env python3
"""comtool MCP server（stdio，僅用 Python 標準函式庫）。

由 Claude Code 啟動，對每個 COM port 維持一條連到 comd（127.0.0.1:700N）的 TCP 連線，
提供 com_list / com_open / com_send / com_read / com_log / com_close 工具。
MCP server 結束時只關閉自己的 TCP 連線，不會結束 comd。
"""
import json, os, re, signal, socket, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.realpath(__file__))
LOGDIR = os.path.join(HERE, "log")
COMD = os.path.join(HERE, "comd.py")
BUF_MAX = 1 << 20
SERVER_INFO = {"name": "comtool", "version": "1.0.0"}


def log(*a):
    print("[comtool]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 文字處理
CURSOR_POS = re.compile(r"\x1b\[\d*;?\d*[Hf]")
CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def clean(text):
    """去掉 ANSI 碼：游標定位換成換行，其他 CSI 刪除；刪除 \\r、NUL，壓縮多餘空行。"""
    text = CURSOR_POS.sub("\n", text)
    text = CSI.sub("", text).replace("\r", "").replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_raw(text):
    return text.encode("latin1", "backslashreplace").decode("unicode_escape").encode("latin1")


class TelnetStripper:
    """濾掉 comd 送來的 telnet 協商位元組（可跨 recv 邊界）。"""

    def __init__(self):
        self.st = 0

    def feed(self, data):
        out = bytearray()
        for b in data:
            st = self.st
            if st == 0:
                if b == 0xFF:
                    self.st = 1
                else:
                    out.append(b)
            elif st == 1:
                if b == 0xFF:
                    out.append(b); self.st = 0
                elif 251 <= b <= 254:
                    self.st = 2
                elif b == 250:
                    self.st = 3
                else:
                    self.st = 0
            elif st == 2:
                self.st = 0
            elif st == 3:
                if b == 0xFF:
                    self.st = 4
            elif st == 4:
                self.st = 0 if b == 240 else 3
        return bytes(out)


# ---------------------------------------------------------------- comd 狀態
def norm_port(port):
    p = str(port).strip().upper()
    if p.isdigit():
        p = "COM" + p
    m = re.fullmatch(r"COM(\d+)", p)
    if not m:
        raise ValueError(f"COM port 名稱錯誤: {port}")
    return p, int(m.group(1))


children = []  # 由 com_open 啟動的 comd，定期回收以免變成 zombie


def daemon_info(com):
    """回傳 (pid, tcp_port, baud)；沒有在執行則回傳 None（並清掉殘留的 pidfile）。"""
    for p in list(children):
        if p.poll() is not None:
            children.remove(p)
    path = os.path.join(LOGDIR, com + ".pid")
    try:
        with open(path) as f:
            pid, tcp, baud = (int(x) for x in f.read().split()[:3])
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    except PermissionError:
        pass
    return pid, tcp, baud


# ---------------------------------------------------------------- 連線
class Conn:
    def __init__(self, com, tcp_port):
        self.com, self.tcp_port = com, tcp_port
        self.sock = socket.create_connection(("127.0.0.1", tcp_port), timeout=3)
        self.sock.settimeout(None)
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.base = 0          # buf[0] 的絕對位置
        self.read_pos = 0      # 已回傳到的絕對位置
        self.last_rx = 0.0
        self.closed = False
        self.strip = TelnetStripper()
        threading.Thread(target=self._rx, daemon=True).start()

    @property
    def end(self):
        return self.base + len(self.buf)

    def _rx(self):
        while True:
            try:
                d = self.sock.recv(4096)
            except OSError:
                d = b""
            with self.cv:
                if not d:
                    self.closed = True
                    self.cv.notify_all()
                    return
                d = self.strip.feed(d)
                self.buf += d
                if len(self.buf) > BUF_MAX:
                    drop = len(self.buf) - BUF_MAX
                    del self.buf[:drop]
                    self.base += drop
                self.last_rx = time.monotonic()
                self.cv.notify_all()

    def text(self, a, b):
        a = max(a, self.base)
        return bytes(self.buf[a - self.base:b - self.base]).decode("utf-8", "replace")

    def send(self, data):
        self.sock.sendall(data)

    def wait(self, since, expect, timeout, idle):
        """等待資料。expect 只比對 since 之後的資料。回傳 (status, 經過秒數)。"""
        rx = re.compile(expect) if expect else None
        t0 = time.monotonic()
        deadline = t0 + timeout
        with self.cv:
            while True:
                now = time.monotonic()
                if rx and rx.search(clean(self.text(since, self.end))):
                    # 符合後再收 0.15 秒內接著到的資料（最多 1 秒），避免輸出被切成兩半
                    settle_end = min(now + 1.0, deadline + 1.0)
                    while not self.closed:
                        now = time.monotonic()
                        quiet_at = self.last_rx + 0.15
                        if now >= quiet_at or now >= settle_end:
                            break
                        self.cv.wait(min(quiet_at, settle_end) - now)
                    return "matched", time.monotonic() - t0
                if self.closed:
                    return "closed", now - t0
                if now >= deadline:
                    return ("timeout" if rx else "done"), now - t0
                if not rx and idle is not None and now - max(t0, self.last_rx) >= idle:
                    return "idle", now - t0
                wake = deadline
                if not rx and idle is not None:
                    wake = min(wake, max(t0, self.last_rx) + idle)
                self.cv.wait(max(0.01, wake - now))

    def take(self, strip_ansi):
        """取出 read_pos 之後的所有資料並前進 read_pos。"""
        with self.cv:
            skipped = max(0, self.base - self.read_pos)
            out = self.text(self.read_pos, self.end)
            self.read_pos = self.end
        if strip_ansi:
            out = clean(out)
        if skipped:
            out = f"[緩衝區已滿，略過 {skipped} bytes]\n" + out
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


conns = {}


def get_conn(com):
    c = conns.get(com)
    if c and c.closed:
        c.close()
        conns.pop(com, None)
        raise RuntimeError(f"{com} 與 comd 的連線已中斷（comd 可能已結束），請重新 com_open")
    if not c:
        raise RuntimeError(f"{com} 尚未開啟，請先呼叫 com_open")
    return c


# ---------------------------------------------------------------- 工具實作
def t_com_list(args):
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } |"
          " ForEach-Object { $_.Name + '|' + $_.DeviceID }")
    lines = []
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True,
                             timeout=30).stdout.decode("utf-8", "replace")
        for row in out.splitlines():
            if "|" not in row:
                continue
            name, dev = row.strip().split("|", 1)
            m = re.search(r"\((COM\d+)\)", name)
            if m:
                lines.append((m.group(1), name, dev))
    except Exception as e:
        return f"查詢 Windows COM port 失敗: {e}"
    lines.sort(key=lambda x: int(x[0][3:]))
    res = []
    for com, name, dev in lines:
        info = daemon_info(com)
        st = f"comd 執行中（pid {info[0]}, tcp {info[1]}, {info[2]} baud）" if info else "comd 未執行"
        if com in conns and not conns[com].closed:
            st += "，MCP 已連線"
        res.append(f"{com}: {name} [{dev}] — {st}")
    return "\n".join(res) or "找不到任何 COM port"


def t_com_open(args):
    com, n = norm_port(args["port"])
    baud = int(args.get("baud", 115200))
    tcp = int(args.get("tcp_port") or 7000 + n)
    msg = []
    info = daemon_info(com)
    if info:
        tcp = info[1]
        msg.append(f"comd 已在執行（pid {info[0]}, {info[2]} baud, tcp {tcp}）")
        if info[2] != baud and "baud" in args:
            msg.append(f"注意：comd 目前是 {info[2]} baud，與要求的 {baud} 不同；要改速率請先 com_close")
    else:
        os.makedirs(LOGDIR, exist_ok=True)
        outpath = os.path.join(LOGDIR, com + ".comd.out")
        out = open(outpath, "ab")
        start = out.tell()
        p = subprocess.Popen([sys.executable, COMD, com, "--baud", str(baud), "--port", str(tcp)],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
        out.close()
        children.append(p)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if daemon_info(com):
                break
            if p.poll() is not None:
                with open(outpath, "rb") as f:
                    f.seek(start)
                    err = f.read().decode("utf-8", "replace").strip()
                raise RuntimeError(f"comd 啟動失敗（exit {p.returncode}）：{err}")
            time.sleep(0.2)
        else:
            p.terminate()
            raise RuntimeError("等待 comd READY 逾時（15 秒）")
        msg.append(f"已啟動 comd（pid {p.pid}, {baud} baud, tcp {tcp}）")
    old = conns.pop(com, None)
    if old:
        old.close()
    conns[com] = Conn(com, tcp)
    msg.append(f"MCP 已連線到 127.0.0.1:{tcp}；PuTTY/telnet 可同時連 127.0.0.1 {tcp}")
    return "\n".join(msg)


def _fmt(status, secs, out):
    return f"[{status} {secs:.2f}s]\n{out}"


def t_com_send(args):
    com, _ = norm_port(args["port"])
    c = get_conn(com)
    text = args.get("text", "")
    data = parse_raw(text) if args.get("raw") else text.encode()
    if args.get("newline", True) and not args.get("raw"):
        data += b"\r"
    timeout = min(float(args.get("timeout", 10)), 300)
    idle = float(args.get("idle", 0.5))
    with c.cv:
        since = c.end
    c.send(data)
    status, secs = c.wait(since, args.get("expect"), timeout, idle)
    return _fmt(status, secs, c.take(args.get("strip_ansi", True)))


def t_com_read(args):
    com, _ = norm_port(args["port"])
    c = get_conn(com)
    timeout = min(float(args.get("timeout", 0)), 600)
    expect = args.get("expect")
    status, secs = "done", 0.0
    if expect or timeout > 0:
        with c.cv:
            since = c.read_pos
        status, secs = c.wait(since, expect, timeout, None)
    return _fmt(status, secs, c.take(args.get("strip_ansi", True)))


def t_com_log(args):
    com, _ = norm_port(args["port"])
    n = min(int(args.get("bytes", 4000)), BUF_MAX)
    path = os.path.join(LOGDIR, com + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            data = f.read()
    except OSError as e:
        raise RuntimeError(f"讀取 {path} 失敗: {e}")
    text = data.decode("utf-8", "replace")
    if args.get("strip_ansi", True):
        text = clean(text)
    return f"[{path}，共 {size} bytes，顯示最後 {len(data)} bytes]\n{text}"


def t_com_close(args):
    com, _ = norm_port(args["port"])
    msg = []
    c = conns.pop(com, None)
    if c:
        c.close()
        msg.append("已中斷 MCP 連線")
    if args.get("stop_daemon", True):
        info = daemon_info(com)
        if info:
            os.kill(info[0], signal.SIGTERM)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and daemon_info(com):
                time.sleep(0.2)
            if daemon_info(com):
                msg.append(f"comd（pid {info[0]}）尚未結束，請檢查")
            else:
                msg.append(f"已結束 comd（pid {info[0]}），{com} 已釋放")
        else:
            msg.append("comd 未在執行")
    return "\n".join(msg) or "沒有需要關閉的連線"


PORT = {"type": "string", "description": "Windows COM port，例如 COM3"}
STRIP = {"type": "boolean", "default": True, "description": "去掉 ANSI 跳脫碼（游標定位換成換行）"}
TOOLS = [
    ("com_list", t_com_list, "列出 Windows 的 COM port，以及各 port 的 comd 執行狀態與 MCP 連線狀態。",
     {"type": "object", "properties": {}}),
    ("com_open", t_com_open,
     "開啟 COM port：comd 沒在執行就在背景啟動它，然後建立 MCP 的持續連線。comd 會一直執行到 com_close，"
     "PuTTY/telnet 可同時連 127.0.0.1 7000+N。",
     {"type": "object", "required": ["port"], "properties": {
         "port": PORT,
         "baud": {"type": "integer", "default": 115200},
         "tcp_port": {"type": "integer", "description": "預設 7000+N"}}}),
    ("com_send", t_com_send,
     "送出文字到 COM port（預設自動加 CR），並等待回應。有 expect（regex）就等到符合或逾時；沒有就等到 idle 秒內沒有新資料。"
     "回傳第一行為 [狀態 秒數]，狀態為 matched/timeout/idle/closed。輸出包含送出前尚未讀取的資料。",
     {"type": "object", "required": ["port", "text"], "properties": {
         "port": PORT,
         "text": {"type": "string", "description": "要送出的文字；空字串等於按 Enter"},
         "expect": {"type": "string", "description": "等待的 regex，例如 'switch>|Username'"},
         "timeout": {"type": "number", "default": 10, "description": "最長等待秒數（上限 300）"},
         "idle": {"type": "number", "default": 0.5, "description": "沒有 expect 時，多久沒新資料就回傳"},
         "newline": {"type": "boolean", "default": True, "description": "是否在後面加 CR"},
         "raw": {"type": "boolean", "default": False,
                 "description": "原樣送出、不加 CR，並解析 \\xNN（例如 '\\x11' 為 Ctrl+Q）"},
         "strip_ansi": STRIP}}),
    ("com_read", t_com_read,
     "不送任何東西，讀取尚未讀取的輸出。可指定 expect 等到符合，或 timeout 等待一段時間（例如看開機 log）。",
     {"type": "object", "required": ["port"], "properties": {
         "port": PORT,
         "expect": {"type": "string", "description": "等待的 regex"},
         "timeout": {"type": "number", "default": 0, "description": "最長等待秒數（上限 600）"},
         "strip_ansi": STRIP}}),
    ("com_log", t_com_log, "讀取 comd 的 log 檔（log/COMx.log）最後一段，可看到 MCP 連線之前的輸出。",
     {"type": "object", "required": ["port"], "properties": {
         "port": PORT,
         "bytes": {"type": "integer", "default": 4000},
         "strip_ansi": STRIP}}),
    ("com_close", t_com_close, "中斷 MCP 連線；stop_daemon=true（預設）時一併結束 comd，釋放 COM port。",
     {"type": "object", "required": ["port"], "properties": {
         "port": PORT,
         "stop_daemon": {"type": "boolean", "default": True}}}),
]
HANDLERS = {name: fn for name, fn, _, _ in TOOLS}


# ---------------------------------------------------------------- JSON-RPC
def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if mid is None:
        return  # notification
    if method == "initialize":
        reply(mid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": [{"name": n, "description": d, "inputSchema": s} for n, _, d, s in TOOLS]})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            reply(mid, error={"code": -32602, "message": f"unknown tool: {name}"})
            return
        try:
            text, err = fn(args), False
        except Exception as e:
            text, err = f"錯誤：{e}", True
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": err})
    else:
        reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        for r in (req if isinstance(req, list) else [req]):
            handle(r)
    for c in conns.values():
        c.close()


if __name__ == "__main__":
    main()
