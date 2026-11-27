"""comhub 的協定共用函式與 client（comtool_mcp.py、comput 使用；comhub.py 也引用協定部分）。

hub 位置：環境變數 COMTOOL_HUB（"unix:PATH" 或 "tcp:PORT"）；
沒有設定時，Windows 為 tcp:7000，其他平台為 unix:<repo>/comtool.sock。
SSH 模式下 <repo>/comtool.sock 由 sshd 建立：本機 Windows 的 hub（start_comtool.bat）執行 ssh -R，轉送回 hub。
"""
import errno, hashlib, json, os, re, shutil, socket, stat, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
LOGDIR = os.path.join(HERE, "log")
COMHUB = os.path.join(HERE, "comhub.py")
HUB_TCP_PORT = 7000
DEFAULT_SOCK = os.path.join(HERE, "comtool.sock")
IS_WIN = sys.platform == "win32"
CHUNK = 1 << 20


class HubError(RuntimeError):
    pass


# ---------------------------------------------------------------- 協定（hub 與 client 共用）
def norm_port(port):
    """'3'、'com3'、'COM3' → ('COM3', 3)"""
    p = str(port).strip().upper()
    if p.isdigit():
        p = "COM" + p
    m = re.fullmatch(r"COM(\d+)", p)
    if not m:
        raise ValueError(f"COM port 名稱錯誤: {port}")
    return p, int(m.group(1))


def parse_addr(text):
    """'unix:/path/comtool.sock' → ('unix', path)；'tcp:7000' → ('tcp', 7000)"""
    kind, _, val = text.partition(":")
    if kind == "unix" and val:
        return "unix", os.path.expanduser(val)
    if kind == "tcp" and val.isdigit():
        return "tcp", int(val)
    raise ValueError(f"位置格式錯誤（應為 unix:PATH 或 tcp:PORT）: {text}")


def fmt_addr(addr):
    return f"{addr[0]}:{addr[1]}"


class JsonSock:
    """一行一個 JSON 訊息；保留讀過頭的位元組，供 attach/put 切換成原始資料時使用。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    def recv(self, limit=1 << 16):
        while b"\n" not in self.buf:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError("連線已關閉")
            self.buf += d
            if len(self.buf) > limit:
                raise ValueError("訊息過長")
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def take_buf(self):
        b, self.buf = self.buf, b""
        return b

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


# ---------------------------------------------------------------- client
def hub_address():
    env = os.environ.get("COMTOOL_HUB")
    if env:
        return parse_addr(env)
    return ("tcp", HUB_TCP_PORT) if IS_WIN else ("unix", DEFAULT_SOCK)


def can_start_local():
    """本機能不能自己跑 hub（需要能執行 powershell.exe：WSL 或 Windows）。"""
    return IS_WIN or shutil.which("powershell.exe") is not None


def describe(greet, addr=None):
    plat = greet.get("platform")
    if plat == "windows":
        where = "本機 Windows（經由 SSH 轉送）" if addr and addr[0] == "unix" else "Windows"
    elif plat == "wsl":
        where = "WSL"
    else:
        where = f"{plat}（沒有 powershell.exe，無法開 COM port）"
    root = greet.get("tftp_root") or "未設定"
    return f"hub: {where} @ {greet.get('host', '?')}，TFTP 根目錄: {root}"


class _Unavailable(Exception):
    def __init__(self, msg, startable):
        super().__init__(msg)
        self.startable = startable


def _sshd_holder(path):
    """找出監聽 path 這個 Unix Domain Socket 的 process（盡力而為，只看得到自己的 process）。"""
    try:
        inode = None
        with open("/proc/net/unix") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 8 and parts[7] == path and parts[5] == "01":
                    inode = parts[6]
        if not inode:
            return None
        target = f"socket:[{inode}]"
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
                        return int(pid)
            except OSError:
                continue
    except OSError:
        return None
    return None


def _proc_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime"))
        return time.strftime("%m-%d %H:%M:%S", time.localtime(btime + ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError, StopIteration, IndexError):
        return "?"


def _my_sshd():
    """往上找目前這個 process 所屬的 sshd。"""
    pid = os.getpid()
    for _ in range(64):
        try:
            with open(f"/proc/{pid}/stat") as f:
                rest = f.read().rsplit(")", 1)
            comm = rest[0].split("(", 1)[1]
            ppid = int(rest[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
        if comm == "sshd":
            return pid
        if ppid <= 1:
            return None
        pid = ppid
    return None


def _no_greeting_hint(path):
    holder = _sshd_holder(path)
    mine = _my_sshd()
    if holder and holder != mine:
        return (f"連上 {path} 後等不到 hub 回應。這個 socket 由 pid {holder}（開始於 {_proc_start(holder)}）監聽，"
                f"不是目前這條 SSH 連線的 sshd（pid {mine}），可能是已經斷線的舊連線。"
                f"請執行 kill {holder}；本機 hub 重建 tunnel 時也會自動清除它。")
    return f"連上 {path} 後等不到 hub 回應（逾時）。請確認本機 Windows 的 comhub.py 是否正常執行。"


def _connect_once(addr, timeout):
    kind, val = addr
    if kind == "unix":
        try:
            st = os.lstat(val)
        except FileNotFoundError:
            raise _Unavailable(
                f"找不到 {val}，tunnel 沒有建立。SSH 模式下這個 socket 由本機 Windows 的 hub 建立："
                f"請確認本機 Windows 的 start_comtool.bat 視窗還開著，而且 comhub.ini 的 user／host／repo 正確"
                f"（repo 要是 {os.path.dirname(val)}）；原因看 Windows 上的 log/comhub.log。",
                startable=True)
        if not stat.S_ISSOCK(st.st_mode):
            raise _Unavailable(f"{val} 不是 socket，請確認內容後手動刪除；本機 hub 會自動重建 tunnel。", startable=False)
        if st.st_uid != os.getuid():
            raise _Unavailable(f"{val} 的擁有者不是你（uid {st.st_uid}），拒絕連線。", startable=False)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(val)
        except OSError as e:
            s.close()
            if e.errno in (errno.ECONNREFUSED, errno.ENOENT):
                # 沒有人在監聽：sshd 斷線後不會刪掉 socket（StreamLocalBindUnlink 預設 no），
                # 留下的檔案會讓 hub 重建 tunnel 時的 ssh -R 失敗；hub 的清理程式也會處理，這裡先順手清掉
                try:
                    os.unlink(val)
                except OSError:
                    pass
                raise _Unavailable(
                    f"{val} 是上次斷線留下的殘留檔（沒有人監聽），已刪除。SSH 模式下本機 hub 會自動重建 tunnel，"
                    f"請等幾秒再試；持續失敗請看 Windows 上的 log/comhub.log。"
                    f"（請 build server 管理員在 sshd_config 加上 StreamLocalBindUnlink yes，重建會更快）",
                    startable=True)
            raise _Unavailable(f"無法連到 {val}: {e}", startable=False)
    else:
        try:
            s = socket.create_connection(("127.0.0.1", val), timeout=timeout)
        except OSError as e:
            raise _Unavailable(f"無法連到 127.0.0.1:{val}，hub 沒有在執行？ {e}", startable=True)
    js = JsonSock(s)
    try:
        greet = js.recv()
    except EOFError:
        js.close()
        if kind == "unix" and not can_start_local():
            raise _Unavailable("連上 comtool.sock 但馬上被斷線：入口還在，但本機 Windows 的 comhub.py 沒有在執行"
                               "（可能是 hub 被強制結束後留下的 ssh.exe，或 ssh config 還留著舊的 RemoteForward）。"
                               "請拿掉 RemoteForward，重新執行 start_comtool.bat。", startable=False)
        raise _Unavailable("連上後馬上被斷線，hub 沒有在執行？", startable=True)
    except socket.timeout:
        js.close()
        if kind == "unix":
            raise _Unavailable(_no_greeting_hint(val), startable=False)
        raise _Unavailable("連上後等不到 hub 回應（逾時）", startable=False)
    if greet.get("hub") != "comtool":
        js.close()
        raise _Unavailable(f"對方不是 comtool hub: {greet}", startable=False)
    if "error" in greet:
        js.close()
        raise _Unavailable(f"hub 拒絕連線：{greet['error']}", startable=False)
    s.settimeout(None)
    return js, greet


def start_local_hub(addr):
    os.makedirs(LOGDIR, exist_ok=True)
    out = open(os.path.join(LOGDIR, "comhub.out"), "ab")
    cmd = [sys.executable, COMHUB, "--listen", fmt_addr(addr)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, close_fds=True)
    if IS_WIN:
        kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    out.close()


def connect(timeout=5.0, auto_start=True):
    """連到 hub，回傳 (JsonSock, greeting)。本機可以跑 hub（WSL）而 hub 沒在執行時，自動在背景啟動。"""
    addr = hub_address()
    try:
        return _connect_once(addr, timeout)
    except _Unavailable as e:
        if not (auto_start and e.startable and can_start_local()):
            raise HubError(str(e)) from None
    start_local_hub(addr)
    deadline = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            return _connect_once(addr, timeout)
        except _Unavailable as e:
            last = e
    raise HubError(f"已在背景啟動 hub，但 5 秒內連不上：{last}（見 log/comhub.out）")


def request(obj, timeout=30.0):
    """送出一個控制請求，回傳 (回應, greeting)。回應不是 ok 時丟出 HubError。"""
    js, greet = connect()
    try:
        js.sock.settimeout(timeout)
        js.send(obj)
        resp = js.recv()
    except (OSError, EOFError, ValueError) as e:
        raise HubError(f"與 hub 通訊失敗: {e}") from None
    finally:
        js.close()
    if not resp.get("ok"):
        raise HubError(resp.get("error", f"hub 回應錯誤: {resp}"))
    return resp, greet


def attach(com):
    """建立 COM port 的原始資料流。回傳 (socket, 已讀過頭的位元組, greeting)。"""
    js, greet = connect()
    try:
        js.sock.settimeout(10)
        js.send({"cmd": "attach", "port": com})
        resp = js.recv()
    except (OSError, EOFError, ValueError) as e:
        js.close()
        raise HubError(f"與 hub 通訊失敗: {e}") from None
    if not resp.get("ok"):
        js.close()
        raise HubError(resp.get("error", f"hub 回應錯誤: {resp}"))
    js.sock.settimeout(None)
    return js.sock, js.take_buf(), greet


def put_file(path, name=None):
    """把本機檔案傳到 hub 的 TFTP 根目錄。回傳 hub 的回應（status 為 same 或 uploaded）加上 secs。"""
    t0 = time.monotonic()
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise HubError(f"找不到檔案: {path}")
    size = os.path.getsize(path)
    md5 = file_md5(path)
    name = name or os.path.basename(path)
    js, greet = connect()
    try:
        js.sock.settimeout(30)
        js.send({"cmd": "put", "name": name, "size": size, "md5": md5})
        resp = js.recv()
        if resp.get("ok") and resp.get("status") == "ready":
            js.sock.settimeout(120)
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(CHUNK), b""):
                    js.sock.sendall(blk)
            resp = js.recv()
    except (OSError, EOFError, ValueError) as e:
        raise HubError(f"傳檔失敗: {e}") from None
    finally:
        js.close()
    if not resp.get("ok"):
        raise HubError(resp.get("error", f"hub 回應錯誤: {resp}"))
    resp["secs"] = time.monotonic() - t0
    resp["src"] = path
    return resp
