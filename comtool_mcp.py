#!/usr/bin/env python3
"""comtool MCP server（stdio，僅用 Python 標準函式庫）。

由 Claude Code 啟動，所有操作都透過 comhub（hubclient.py）：
  - WSL 模式：連 <repo>/comtool.sock，hub 沒在執行時自動在背景啟動
  - SSH 模式：<repo>/comtool.sock 由本機 Windows 的 hub（start_comtool.bat）執行 ssh -R 建立，轉送回 hub
對每個 COM port 維持一條 attach 連線（console 原始資料流），
提供 com_list / com_open / com_send / com_read / com_log / com_close / com_put_file 工具。
MCP server 結束時只關閉自己的連線，不會結束 comd。
"""
import json, os, re, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import hubclient  # noqa: E402
from hubclient import norm_port  # noqa: E402

BUF_MAX = 1 << 20
SERVER_INFO = {"name": "comtool", "version": "1.1.0"}


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


# ---------------------------------------------------------------- 連線
class Conn:
    def __init__(self, com, sock, early=b""):
        """sock 是已經對 hub 送過 attach 的連線；early 是 attach 回應之後已經讀到的資料。"""
        self.com = com
        self.sock = sock
        self.sock.settimeout(None)
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.base = 0          # buf[0] 的絕對位置
        self.read_pos = 0      # 已回傳到的絕對位置
        self.last_rx = 0.0
        self.closed = False
        self.strip = TelnetStripper()
        if early:
            self.buf += self.strip.feed(early)
            self.last_rx = time.monotonic()
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
        raise RuntimeError(f"{com} 的連線已中斷（comd 已結束，或 SSH 斷線），請重新 com_open")
    if not c:
        raise RuntimeError(f"{com} 尚未開啟，請先呼叫 com_open")
    return c


# ---------------------------------------------------------------- 工具實作
def t_com_list(args):
    resp, greet = hubclient.request({"cmd": "list"}, timeout=60)
    res = [hubclient.describe(greet, hubclient.hub_address())]
    for p in resp.get("ports", []):
        d = p.get("daemon")
        st = f"comd 執行中（pid {d['pid']}, tcp {d['tcp']}, {d['baud']} baud）" if d else "comd 未執行"
        if p["port"] in conns and not conns[p["port"]].closed:
            st += "，MCP 已連線"
        res.append(f"{p['port']}: {p['name']} [{p['dev']}] — {st}")
    if len(res) == 1:
        res.append("找不到任何 COM port")
    return "\n".join(res)


def t_com_open(args):
    com, _ = norm_port(args["port"])
    req = {"cmd": "open", "port": com, "baud": int(args.get("baud", 115200))}
    if "baud" not in args:
        del req["baud"]
    if args.get("tcp_port"):
        req["tcp_port"] = int(args["tcp_port"])
    resp, greet = hubclient.request(req, timeout=30)
    tcp = resp["tcp"]
    if resp.get("started"):
        msg = [f"已啟動 comd（pid {resp['pid']}, {resp['baud']} baud, tcp {tcp}）"]
    else:
        msg = [f"comd 已在執行（pid {resp['pid']}, {resp['baud']} baud, tcp {tcp}）"]
    if resp.get("warning"):
        msg.append("注意：" + resp["warning"])
    old = conns.pop(com, None)
    if old:
        old.close()
    sock, early, _ = hubclient.attach(com)
    conns[com] = Conn(com, sock, early)
    where = "本機 Windows 上" if greet.get("platform") == "windows" else "WSL 或 Windows 上"
    msg.append(f"MCP 已連線；{where}的 PuTTY/telnet 可同時連 127.0.0.1 {tcp}")
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
    resp, _ = hubclient.request({"cmd": "log", "port": com, "bytes": n})
    text = resp["text"]
    if args.get("strip_ansi", True):
        text = clean(text)
    return f"[{resp['path']}，共 {resp['size']} bytes，顯示最後 {resp['shown']} bytes]\n{text}"


def t_com_close(args):
    com, _ = norm_port(args["port"])
    msg = []
    c = conns.pop(com, None)
    if c:
        c.close()
        msg.append("已中斷 MCP 連線")
    if args.get("stop_daemon", True):
        resp, _ = hubclient.request({"cmd": "close", "port": com})
        msg.append(resp["message"])
    return "\n".join(msg) or "沒有需要關閉的連線"


def t_com_put_file(args):
    r = hubclient.put_file(args["path"], args.get("name") or None)
    return (f"[{r['status']} {r['secs']:.2f}s]\n"
            f"{r['src']} → {r['path']}\n"
            f"{r['size']} bytes, md5 {r['md5']}")


PORT = {"type": "string", "description": "Windows COM port，例如 COM3"}
STRIP = {"type": "boolean", "default": True, "description": "去掉 ANSI 跳脫碼（游標定位換成換行）"}
TOOLS = [
    ("com_list", t_com_list,
     "列出 hub 所在 Windows 的 COM port，以及各 port 的 comd 執行狀態與 MCP 連線狀態；第一行是 hub 的位置與 TFTP 根目錄。",
     {"type": "object", "properties": {}}),
    ("com_open", t_com_open,
     "開啟 COM port：comd 沒在執行就由 hub 啟動它，然後建立 MCP 的持續連線。comd 會一直執行到 com_close，"
     "hub 所在的機器上 PuTTY/telnet 可同時連 127.0.0.1 7000+N。",
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
    ("com_put_file", t_com_put_file,
     "把檔案（例如 firmware image）傳到 hub 所在機器的 TFTP 根目錄，讓設備用 TFTP 下載。"
     "hub 會比對 md5，已有相同檔案時不重傳；寫入完成才換成正式檔名，設備不會抓到寫一半的檔案。"
     "回傳第一行為 [uploaded 秒數] 或 [same 秒數]。",
     {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "要傳的檔案路徑（MCP server 所在機器上；相對路徑以 MCP 的工作目錄為準）"},
         "name": {"type": "string",
                  "description": "存到 TFTP 根目錄的檔名，預設與原檔名相同；只能用英數字與 . _ -"}}}),
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
