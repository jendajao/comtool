#!/usr/bin/env python3
"""comtool hub：管理 comd、轉送 console、接收傳檔（WSL 或 Windows 原生 Python 皆可執行）。

usage: comhub.py [--listen unix:PATH|tcp:PORT] [--ssh USER@HOST:REMOTE_SOCK ...] [--tftp-root DIR] [--max-size MB]

  預設監聽位置：Windows 為 tcp:7000（127.0.0.1），其他平台為 unix:<repo>/comtool.sock（權限 0600）。
  SSH 模式：使用者在本機 Windows 雙擊 start_comtool.bat 執行本程式。個人設定讀同目錄的 comhub.ini
    （[ssh] user/host/repo、[hub] tftp_root；命令列參數優先）。每個 --ssh 由一個 Tunnel 執行緒
    執行 `ssh -R REMOTE_SOCK:127.0.0.1:7000`，維持 tunnel、斷線重建、清除 build server 上殘留的 socket。
  WSL 模式：由 comtool_mcp.py 在需要時自動於背景啟動，不需要 comhub.ini。

協定（JSON 一行一個訊息）：連線後 hub 先送問候訊息，client 送一個請求：
  info / list / open / close / log：回一個 JSON 後關閉
  attach：回 ok 後，連線變成與 comd 對接的原始資料流
  put：回 same（已有相同檔案）或 ready；ready 後 client 送 size 個位元組，hub 驗證 md5 後回結果
"""
import argparse, atexit, configparser, hashlib, json, os, re, shutil, signal, socket, stat, subprocess, sys
import threading, time

from hubclient import (CHUNK, HUB_TCP_PORT, DEFAULT_SOCK, JsonSock, file_md5, fmt_addr, norm_port,
                       parse_addr)

HERE = os.path.dirname(os.path.realpath(__file__))
LOGDIR = os.path.join(HERE, "log")
COMD = os.path.join(HERE, "comd.py")
BRIDGE = os.path.join(HERE, "bridge.ps1")
INI = os.path.join(HERE, "comhub.ini")
VERSION = 1
HUB_ID = os.urandom(6).hex()   # 這次執行的識別碼；tunnel 的清理程式用它分辨入口是不是連到自己
IS_WIN = sys.platform == "win32"
NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def platform_name():
    if IS_WIN:
        return "windows"
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower():
                return "wsl"
    except OSError:
        pass
    return "linux"


PLATFORM = platform_name()

# ---------------------------------------------------------------- log
_log_lock = threading.Lock()
_log_file = None


def log(*a):
    msg = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    with _log_lock:
        if _log_file:
            _log_file.write(msg + "\n")
            _log_file.flush()
        if sys.stderr:  # pythonw 沒有 console 時 sys.stderr 是 None
            print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 平台差異
def no_window():
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


def pid_alive(pid):
    if IS_WIN:
        # Windows 上 os.kill(pid, 0) 會送 CTRL_C_EVENT，不能拿來檢查
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED：存在但沒有權限
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    # hub 自己啟動的 comd 結束後會變成 zombie（還沒被 wait），os.kill(pid, 0) 仍會成功
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def exit_code(rc):
    """Windows 的結束碼是 32 位元無號數，-1 會變成 4294967295；轉回有號數方便閱讀。"""
    if isinstance(rc, int) and rc >= 1 << 31:
        return rc - (1 << 32)
    return rc


def kill_pid(pid):
    # Linux：SIGTERM，comd 會關閉 bridge 並由 atexit 刪 pidfile。
    # Windows：等同 TerminateProcess；comd 結束後 bridge 的 stdin 會關閉，bridge.ps1 讀到 EOF 就自行結束並釋放 COM port。
    os.kill(pid, signal.SIGTERM)


def python_for_child():
    # pythonw 執行的 hub 啟動 comd 時改用 python.exe（comd 會 print），搭配 CREATE_NO_WINDOW 不會出現視窗
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt):
            return alt
    return exe


def to_local_path(p):
    """WSL 上收到 Windows 路徑（D:\\tftp）時轉成 /mnt/d/tftp。"""
    if not IS_WIN and re.match(r"^[A-Za-z]:[\\/]", p) and shutil.which("wslpath"):
        try:
            return subprocess.check_output(["wslpath", "-u", p], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return p


# ---------------------------------------------------------------- TFTP 根目錄
def tftpd64_ini_candidates():
    if IS_WIN:
        bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    else:
        bases = ["/mnt/c/Program Files", "/mnt/c/Program Files (x86)"]
    for b in filter(None, bases):
        for d in ("Tftpd64", "Tftpd64-SE", "Tftpd32"):
            for n in ("tftpd64.ini", "tftpd32.ini"):
                yield os.path.join(b, d, n)


def detect_tftp_root():
    """回傳 (路徑, 來源說明)；找不到回傳 (None, 原因)。"""
    env = os.environ.get("COMTOOL_TFTP_ROOT")
    if env:
        return to_local_path(env), "環境變數 COMTOOL_TFTP_ROOT"
    for ini in tftpd64_ini_candidates():
        try:
            with open(ini, encoding="latin-1") as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r"^\s*BaseDirectory\s*=\s*(.*?)\s*$", text, re.I | re.M)
        if not m or not m.group(1):
            continue
        root = to_local_path(m.group(1))
        if not os.path.isabs(root) and not re.match(r"^[A-Za-z]:", root):
            root = os.path.normpath(os.path.join(os.path.dirname(ini), root))
        return root, f"{ini} 的 BaseDirectory"
    return None, "未設定（請在 comhub.ini 填 tftp_root，或用 --tftp-root、環境變數 COMTOOL_TFTP_ROOT 指定）"


# ---------------------------------------------------------------- comd 管理
_children = []
_port_locks = {}
_port_locks_lock = threading.Lock()


def port_lock(com):
    with _port_locks_lock:
        return _port_locks.setdefault(com, threading.Lock())


def pidfile_of(com):
    return os.path.join(LOGDIR, com + ".pid")


def daemon_info(com):
    """回傳 (pid, tcp_port, baud)；沒有在執行則回傳 None（並清掉殘留的 pidfile）。"""
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)
    path = pidfile_of(com)
    try:
        with open(path) as f:
            pid, tcp, baud = (int(x) for x in f.read().split()[:3])
    except (OSError, ValueError):
        return None
    if not pid_alive(pid):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return pid, tcp, baud


def list_ports():
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } |"
          " ForEach-Object { [Console]::Out.WriteLine($_.Name + '|' + $_.DeviceID) }")
    found = []
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True,
                             timeout=30, **no_window()).stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        raise RuntimeError(f"hub 所在的機器（{socket.gethostname()}）找不到 powershell.exe，無法列出 COM port") from None
    for row in out.splitlines():
        if "|" not in row:
            continue
        name, dev = row.strip().split("|", 1)
        m = re.search(r"\((COM\d+)\)", name)
        if m:
            found.append((m.group(1), name, dev))
    found.sort(key=lambda x: int(x[0][3:]))
    res = []
    for com, name, dev in found:
        info = daemon_info(com)
        res.append({"port": com, "name": name, "dev": dev,
                    "daemon": dict(zip(("pid", "tcp", "baud"), info)) if info else None})
    return res


def _cmd_has_port(cmd, com):
    return re.search(rf"(?<![A-Za-z0-9]){com}(?![0-9])", cmd, re.I) is not None


_bridge_win_path = None


def bridge_win_path():
    """comd 傳給 powershell.exe 的 bridge.ps1 路徑（WSL 上是 wslpath -w 的結果）。"""
    global _bridge_win_path
    if _bridge_win_path is None:
        _bridge_win_path = BRIDGE
        if not IS_WIN:
            try:
                _bridge_win_path = subprocess.check_output(["wslpath", "-w", BRIDGE], text=True).strip()
            except (OSError, subprocess.CalledProcessError):
                pass
    return _bridge_win_path


def find_orphans(com):
    """找出這個 repo 的 comd.py／bridge.ps1 中，參數是 com 的程序。

    只在「這個 port 沒有執行中的 comd」時呼叫，所以找到的都是殘留。
    Windows 程序（bridge，以及 Windows 上的 comd）用 PowerShell CIM 查詢；
    Linux 程序（WSL 上的 comd）讀 /proc。查詢失敗時略過該部分。
    回傳 [{"pid", "side": "win"|"linux", "kind": "comd"|"bridge", "cmd"}]。
    """
    found = []
    if IS_WIN or shutil.which("powershell.exe"):
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              # python 的程序名稱不一定是 python.exe（例如 Microsoft Store 版是 python3.12.exe）
              "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%' OR Name='powershell.exe'\" |"
              # 用 [Console]::Out 直接寫，避免 PowerShell 依視窗寬度把長命令列折行（會切斷路徑）
              " ForEach-Object { [Console]::Out.WriteLine([string]$_.ProcessId + '|' + $_.CommandLine) }")
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True,
                                 timeout=30, **no_window()).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        comd_l, bridge_l = COMD.lower(), bridge_win_path().lower()
        for row in out.splitlines():
            pid, sep, cmd = row.strip().partition("|")
            if not sep or not pid.isdigit() or int(pid) == os.getpid() or not _cmd_has_port(cmd, com):
                continue
            low = cmd.lower()
            if bridge_l in low:
                found.append({"pid": int(pid), "side": "win", "kind": "bridge", "cmd": cmd})
            elif IS_WIN and comd_l in low:
                found.append({"pid": int(pid), "side": "win", "kind": "comd", "cmd": cmd})
    if not IS_WIN:
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == os.getpid():
                continue
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    argv = [x.decode(errors="replace") for x in f.read().split(b"\0") if x]
                cwd = os.readlink(f"/proc/{d}/cwd")
            except OSError:
                continue
            if not any(a.upper() == com for a in argv[1:]):
                continue
            for a in argv[:3]:
                if a.endswith(("comd", "comd.py")) and \
                        os.path.realpath(os.path.join(cwd, a)) == COMD:
                    found.append({"pid": int(d), "side": "linux", "kind": "comd", "cmd": " ".join(argv)})
                    break
    return found


def kill_orphan(o):
    if o["side"] == "win" and not IS_WIN:  # WSL 上的 hub 結束 Windows 程序
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {o['pid']} -Force"],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        kill_pid(o["pid"])
    except OSError:
        pass


def fmt_orphans(orphans):
    return "、".join(f"{o['kind']} pid {o['pid']}" for o in orphans)


def open_fail_hint(com, err):
    """comd 啟動失敗時，判斷是殘留程序還是其他程式占用。不結束任何程序。"""
    if "OPEN FAIL" not in err and "無法使用" not in err:
        return ""
    orphans = find_orphans(com)
    if orphans:
        return f"{com} 有殘留的 comd／bridge（{fmt_orphans(orphans)}），請先 com_close 清除再開。"
    if "OPEN FAIL" in err:
        return f"{com} 被其他程式占用（例如 Tera Term、PuTTY），請關閉該程式後再開。"
    return ""


def open_port(com, n, baud, tcp, baud_given):
    with port_lock(com):
        info = daemon_info(com)
        if info:
            r = {"ok": True, "started": False, "pid": info[0], "tcp": info[1], "baud": info[2]}
            if baud_given and info[2] != baud:
                r["warning"] = f"comd 目前是 {info[2]} baud，與要求的 {baud} 不同；要改速率請先 com_close"
            return r
        tcp = tcp or 7000 + n
        os.makedirs(LOGDIR, exist_ok=True)
        outpath = os.path.join(LOGDIR, com + ".comd.out")
        out = open(outpath, "ab")
        start = out.tell()
        # comd 的輸出導到檔案時，Windows 上 Python 預設用系統 code page（cp950）寫入；
        # 固定成 UTF-8，hub 才能正確讀回錯誤訊息（也才比對得到「無法使用」等字串）
        kw = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, close_fds=True,
                  env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        if IS_WIN:
            kw["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        p = subprocess.Popen([python_for_child(), COMD, com, "--baud", str(baud), "--port", str(tcp)], **kw)
        out.close()
        _children.append(p)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if daemon_info(com):
                log(f"{com} 已啟動 comd pid {p.pid} {baud} baud tcp {tcp}")
                return {"ok": True, "started": True, "pid": p.pid, "tcp": tcp, "baud": baud}
            if p.poll() is not None:
                with open(outpath, "rb") as f:
                    f.seek(start)
                    err = f.read().decode("utf-8", "replace").strip()
                return {"ok": False, "error": f"{open_fail_hint(com, err)}comd 啟動失敗（exit {exit_code(p.returncode)}）：{err}"}
            time.sleep(0.2)
        p.terminate()
        return {"ok": False, "error": "等待 comd READY 逾時（15 秒）"}


def close_port(com):
    with port_lock(com):
        msgs, stuck = [], []
        info = daemon_info(com)
        if info:
            pid = info[0]
            kill_pid(pid)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(0.2)
            if pid_alive(pid):
                return {"ok": True, "message": f"comd（pid {pid}）尚未結束，請檢查"}
            try:  # Windows 上 comd 被強制結束，atexit 不會執行
                os.remove(pidfile_of(com))
            except OSError:
                pass
            log(f"{com} 已結束 comd pid {pid}")
            msgs.append(f"已結束 comd（pid {pid}）")
        # 這時已沒有執行中的 comd，找到的 comd／bridge 都是殘留（例如 pidfile 不見了的 comd、
        # 卡住沒結束的 bridge）；剛結束的 comd 的 bridge 讀到 stdin EOF 會自己結束，先等一下
        orphans = []
        for i in range(3):
            orphans = find_orphans(com)
            if not orphans:
                break
            if i < 2:
                time.sleep(1)
        if orphans:
            for o in orphans:
                kill_orphan(o)
            deadline = time.monotonic() + 3
            left = orphans
            while left and time.monotonic() < deadline:
                time.sleep(0.5)
                alive = {o["pid"] for o in find_orphans(com)}
                left = [o for o in orphans if o["pid"] in alive]
            gone = [o for o in orphans if o not in left]
            if gone:
                log(f"{com} 已清除殘留程序：{fmt_orphans(gone)}")
                msgs.append(f"已清除殘留程序：{fmt_orphans(gone)}")
            if left:
                stuck = left
                log(f"{com} 殘留程序結束不了：{fmt_orphans(left)}")
                msgs.append(f"{fmt_orphans(left)} 結束不了（可能是 USB 轉 serial 在開啟狀態下被拔掉），請重新插拔 USB")
        if not msgs:
            return {"ok": True, "message": "comd 未在執行"}
        return {"ok": True, "message": "；".join(msgs) + ("" if stuck else f"，{com} 已釋放")}


def read_log(com, nbytes):
    path = os.path.join(LOGDIR, com + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError as e:
        return {"ok": False, "error": f"讀取 {path} 失敗: {e}"}
    return {"ok": True, "path": path, "size": size, "text": data.decode("utf-8", "replace"), "shown": len(data)}


# ---------------------------------------------------------------- 連線處理
# ---------------------------------------------------------------- SSH tunnel（SSH 模式）
# 在 build server 上執行（ssh HOST python3 - SOCK，程式由 stdin 送過去），判斷 SOCK 能不能刪：
# 只有「擁有者是自己的 socket，而且沒有 hub 回應」才刪除。輸出一行 JSON。
CLEANUP_PY = r"""
import json, os, socket, stat, sys
p = sys.argv[1]
def out(**k):
    print(json.dumps(k))
    sys.stdout.flush()
    os._exit(0)
try:
    st = os.lstat(p)
except FileNotFoundError:
    out(state="absent")
except OSError as e:
    out(state="error", error=str(e))
if not stat.S_ISSOCK(st.st_mode):
    out(state="notsock")
if st.st_uid != os.getuid():
    out(state="notmine")
g = {}
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(p)
    buf = b""
    while b"\n" not in buf and len(buf) < 65536:
        d = s.recv(4096)
        if not d:
            break
        buf += d
    if b"\n" in buf:
        g = json.loads(buf.split(b"\n", 1)[0])
except (OSError, ValueError):
    pass
s.close()
if isinstance(g, dict) and g.get("hub") == "comtool":
    out(state="live", id=g.get("id"), host=g.get("host"))
try:
    os.unlink(p)
except OSError as e:
    out(state="error", error=str(e))
out(state="removed")
"""

SSH = shutil.which("ssh")
_tunnels = []
_job = None


def job_add(proc):
    """Windows：把 ssh.exe 放進 KILL_ON_JOB_CLOSE 的 Job Object。hub 結束（含關閉視窗、被強制結束）時
    job handle 關閉，ssh.exe 一定跟著結束。只放 ssh，comd 不放（hub 結束後 comd 要繼續執行）。"""
    global _job
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job is None:
            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            h = k32.CreateJobObjectW(None, None)
            if not h:
                raise OSError(f"CreateJobObject 失敗（{ctypes.get_last_error()}）")
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(f"SetInformationJobObject 失敗（{ctypes.get_last_error()}）")
            _job = h
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not k32.AssignProcessToJobObject(_job, int(proc._handle)):
            raise OSError(f"AssignProcessToJobObject 失敗（{ctypes.get_last_error()}）")
    except Exception as e:
        log(f"無法把 ssh 加入 Job Object（hub 被強制結束時 ssh.exe 可能殘留）: {e}")


def stop_tunnels():
    for t in _tunnels:
        p = t.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass


class Tunnel(threading.Thread):
    """執行並監看 `ssh -N -R REMOTE_SOCK:127.0.0.1:PORT HOST`，斷線自動重建，殘留的 socket 交給清理程式處理。"""

    def __init__(self, host, sock, port):
        super().__init__(daemon=True)
        self.host, self.sock, self.port = host, sock, port
        self.state, self.error, self.since = "starting", "", time.time()
        self.proc = None

    def status(self):
        return {"host": self.host, "sock": self.sock, "state": self.state, "error": self.error,
                "since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since))}

    def set(self, state, error=""):
        if (state, error) != (self.state, self.error):
            log(f"[tunnel {self.host}] {state}" + (f"：{error}" if error else ""))
        if state != self.state:
            self.since = time.time()
        self.state, self.error = state, error

    def ssh_base(self):
        return [SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]  # 不記 server 的 banner

    def cleanup(self):
        try:
            r = subprocess.run(self.ssh_base() + [self.host, "python3", "-", self.sock], input=CLEANUP_PY.encode(),
                               capture_output=True, timeout=30, **no_window())
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"state": "error", "error": str(e)}
        for line in reversed(r.stdout.decode("utf-8", "replace").splitlines()):
            try:
                res = json.loads(line)
                if isinstance(res, dict) and "state" in res:
                    return res
            except ValueError:
                continue
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"state": "error", "error": err[-1] if err else f"exit {exit_code(r.returncode)}"}

    def run_once(self):
        """執行一次 ssh，結束後回傳 (是否因轉送失敗而結束, stderr 最後一行, 維持秒數)。"""
        cmd = self.ssh_base() + ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                                 "-o", "ServerAliveCountMax=3", "-R", f"{self.sock}:127.0.0.1:{self.port}", self.host]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 **no_window())
        except OSError as e:
            return False, f"無法執行 ssh: {e}", 0
        self.proc = p
        job_add(p)
        res = {"fwd_failed": False, "last": ""}

        def reader():
            for raw in iter(p.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                log(f"[tunnel {self.host}] ssh: {line}")
                res["last"] = line
                if "forwarding failed" in line:
                    res["fwd_failed"] = True
        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        t0 = time.monotonic()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            self.set("up")
            log(f"[tunnel {self.host}] tunnel 已建立：{self.host}:{self.sock} → 127.0.0.1:{self.port}")
            p.wait()
        rt.join(2)
        return res["fwd_failed"], res["last"] or f"ssh 結束（exit {exit_code(p.returncode)}）", time.monotonic() - t0

    def run(self):
        if not SSH:
            self.set("error", "找不到 ssh（Windows 內建的 OpenSSH 用戶端）")
            return
        backoff, removed = 5, 0
        while True:
            fwd_failed, last, secs = self.run_once()
            if secs > 60:
                backoff, removed = 5, 0
            if fwd_failed:
                r = self.cleanup()
                st = r.get("state")
                if st in ("absent", "removed") and removed < 2:
                    removed += 1
                    log(f"[tunnel {self.host}] {self.sock} " +
                        ("是殘留的 socket，已刪除，重新建立 tunnel" if st == "removed" else "不存在，重新建立 tunnel"))
                    continue
                if st == "live" and r.get("id") == HUB_ID:
                    self.set("up(舊的 ssh.exe)", "入口由先前留下的 ssh.exe 轉送到這個 hub；60 秒後再檢查")
                    time.sleep(60)
                    continue
                if st == "live":
                    self.set("busy", f"其他 hub 正在使用 {self.sock}（{r.get('host') or '?'}）；60 秒後再試")
                    time.sleep(60)
                    continue
                if st not in ("absent", "removed"):
                    self.set("error", f"無法清除 {self.sock}：{st} {r.get('error', '')}".strip() + "；60 秒後再試")
                    time.sleep(60)
                    continue
            self.set("down", f"{last}；{backoff} 秒後重試")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def parse_ssh_spec(spec):
    host, sep, sock = spec.partition(":")
    if not sep or not host or not sock.startswith("/"):
        sys.exit(f"--ssh 格式錯誤: {spec}（應為 USER@HOST:/絕對路徑/comtool.sock）")
    return host, sock


INI_TEMPLATE = """[ssh]
user = <build server 的帳號>
host = <build server 的 IP 或主機名稱>
repo = <build server 上的 repo 路徑，例如 /home/<帳號>/tools/comtool>

[hub]
tftp_root =        ; TFTP 根目錄，留空表示自動讀 tftpd64.ini"""


def load_ini():
    """讀 comhub.ini，回傳 (ssh spec 清單, tftp_root)；沒有檔案回傳 ([], None)。"""
    if not os.path.exists(INI):
        return [], None
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    try:
        with open(INI, encoding="utf-8-sig") as f:
            cp.read_file(f)
    except (OSError, configparser.Error, UnicodeDecodeError) as e:
        sys.exit(f"{INI} 格式錯誤: {e}")
    specs = []
    if cp.has_section("ssh"):
        user, host, repo = (cp.get("ssh", k, fallback="").strip() for k in ("user", "host", "repo"))
        if host or repo or user:
            if not host or not repo.startswith("/"):
                sys.exit(f"{INI} 的 [ssh] 要填 host 與 repo（repo 是 build server 上的絕對路徑）")
            specs.append(f"{user + '@' if user else ''}{host}:{repo.rstrip('/')}/comtool.sock")
    tftp = cp.get("hub", "tftp_root", fallback="").strip() or None
    return specs, tftp


class Hub:
    def __init__(self, listen, tftp_root, tftp_src, max_size):
        self.listen, self.tftp_root, self.tftp_src, self.max_size = listen, tftp_root, tftp_src, max_size
        self.put_lock = threading.Lock()

    def greeting(self):
        return {"hub": "comtool", "version": VERSION, "id": HUB_ID, "platform": PLATFORM,
                "host": socket.gethostname(), "tftp_root": self.tftp_root, "tftp_root_src": self.tftp_src,
                "max_size": self.max_size, "tunnels": [t.status() for t in _tunnels]}

    def handle(self, sock):
        js = JsonSock(sock)
        try:
            sock.settimeout(30)
            js.send(self.greeting())
            req = js.recv()
            cmd = req.get("cmd")
            if cmd == "info":
                js.send(dict(self.greeting(), ok=True))
            elif cmd == "list":
                js.send({"ok": True, "ports": list_ports()})
            elif cmd == "open":
                com, n = norm_port(req["port"])
                baud = int(req.get("baud") or 115200)
                js.send(open_port(com, n, baud, req.get("tcp_port"), "baud" in req))
            elif cmd == "close":
                com, _ = norm_port(req["port"])
                js.send(close_port(com))
            elif cmd == "log":
                com, _ = norm_port(req["port"])
                js.send(read_log(com, max(0, min(int(req.get("bytes", 4000)), 1 << 20))))
            elif cmd == "attach":
                com, _ = norm_port(req["port"])
                self.attach(js, com)
            elif cmd == "put":
                self.put(js, req)
            else:
                js.send({"ok": False, "error": f"未知的指令: {cmd}"})
        except (EOFError, ConnectionError, socket.timeout):
            pass
        except Exception as e:
            log("處理請求失敗:", repr(e))
            try:
                js.send({"ok": False, "error": str(e)})
            except OSError:
                pass
        finally:
            js.close()

    def attach(self, js, com):
        info = daemon_info(com)
        if not info:
            js.send({"ok": False, "error": f"{com} 的 comd 沒有在執行，請先 com_open"})
            return
        try:
            up = socket.create_connection(("127.0.0.1", info[1]), timeout=3)
        except OSError as e:
            js.send({"ok": False, "error": f"連不到 {com} 的 comd（127.0.0.1:{info[1]}）: {e}"})
            return
        up.settimeout(None)
        js.sock.settimeout(None)
        js.send({"ok": True, "tcp": info[1], "baud": info[2]})
        down = js.sock
        early = js.take_buf()
        if early:
            up.sendall(early)

        def pump(src, dst):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    dst.sendall(d)
            except OSError:
                pass
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(up, down), daemon=True)
        t.start()
        pump(down, up)
        t.join(5)
        up.close()

    def put(self, js, req):
        name = str(req.get("name", ""))
        size, md5 = req.get("size"), str(req.get("md5", "")).lower()
        root = self.tftp_root
        if not root:
            js.send({"ok": False, "error": f"TFTP 根目錄{self.tftp_src}"})
            return
        if not os.path.isdir(root):
            js.send({"ok": False, "error": f"TFTP 根目錄不存在: {root}"})
            return
        if not NAME_RE.fullmatch(name) or name.endswith(".part"):
            js.send({"ok": False, "error": f"檔名不合法: {name!r}（只能用英數字與 . _ -，不能以 . 開頭，最長 128 字元）"})
            return
        if not isinstance(size, int) or size < 0:
            js.send({"ok": False, "error": f"size 不合法: {size!r}"})
            return
        if size > self.max_size:
            js.send({"ok": False, "error": f"檔案太大：{size} bytes，上限 {self.max_size} bytes（--max-size）"})
            return
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            js.send({"ok": False, "error": "md5 格式不合法"})
            return
        dest = os.path.join(root, name)
        part = dest + ".part"
        with self.put_lock:
            if os.path.isfile(dest) and os.path.getsize(dest) == size and file_md5(dest) == md5:
                js.send({"ok": True, "status": "same", "path": dest, "size": size, "md5": md5})
                return
            free = shutil.disk_usage(root).free
            if free < size + (16 << 20):
                js.send({"ok": False, "error": f"磁碟空間不足：剩 {free} bytes，需要 {size} bytes"})
                return
            js.send({"ok": True, "status": "ready"})
            js.sock.settimeout(120)
            h = hashlib.md5()
            got = 0
            try:
                with open(part, "wb") as f:
                    early = js.take_buf()[:size]
                    if early:
                        f.write(early); h.update(early); got += len(early)
                    while got < size:
                        d = js.sock.recv(min(CHUNK, size - got))
                        if not d:
                            raise EOFError(f"傳輸中斷（收到 {got}/{size} bytes）")
                        f.write(d); h.update(d); got += len(d)
                if h.hexdigest() != md5:
                    raise ValueError(f"md5 不符：收到 {h.hexdigest()}，應為 {md5}")
                for i in range(6):
                    try:
                        os.replace(part, dest)
                        break
                    except PermissionError:
                        if i == 5:
                            raise PermissionError(f"{dest} 被其他程式使用中（例如 tftpd64 正在傳送），無法覆蓋")
                        time.sleep(0.5)
            except Exception as e:
                try:
                    os.remove(part)
                except OSError:
                    pass
                log(f"put {name} 失敗: {e}")
                try:
                    js.send({"ok": False, "error": str(e)})
                except OSError:
                    pass
                return
        log(f"put {name} {size} bytes md5 {md5} → {dest}")
        js.send({"ok": True, "status": "uploaded", "path": dest, "size": size, "md5": md5})


# ---------------------------------------------------------------- 監聽
def make_listener(addr):
    kind, val = addr
    if kind == "unix":
        if os.path.lexists(val):
            if not stat.S_ISSOCK(os.lstat(val).st_mode):
                sys.exit(f"{val} 已存在而且不是 socket，請確認後刪除")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1)
            try:
                probe.connect(val)
                sys.exit(f"{val} 已經有程式在監聽（另一個 hub，或 SSH tunnel），不重複啟動")
            except OSError:
                os.unlink(val)  # 殘留檔
            finally:
                probe.close()
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            ls.bind(val)
        finally:
            os.umask(old)
        os.chmod(val, 0o600)
        ino = os.lstat(val).st_ino

        def cleanup():
            try:
                if os.lstat(val).st_ino == ino:
                    os.unlink(val)
            except OSError:
                pass
        atexit.register(cleanup)
    else:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not IS_WIN:
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            ls.bind(("127.0.0.1", val))
        except OSError as e:
            sys.exit(f"TCP port {val} 無法使用（已有 hub 在執行？）: {e}")
    ls.listen(16)
    ls.settimeout(0.5)  # 讓 Windows 上的 Ctrl+C 能及時處理
    return ls


def main():
    global _log_file
    ap = argparse.ArgumentParser(description="comtool hub：管理 comd、轉送 console、接收傳檔")
    ap.add_argument("--listen", help=f"監聽位置，unix:PATH 或 tcp:PORT（預設 Windows 為 tcp:{HUB_TCP_PORT}，"
                                     f"其他為 unix:{DEFAULT_SOCK}）")
    ap.add_argument("--ssh", action="append", default=[], metavar="USER@HOST:REMOTE_SOCK",
                    help="建立 SSH tunnel：在 build server 的 REMOTE_SOCK（絕對路徑）開入口，轉回這個 hub；可重複指定。"
                         "沒有指定時讀 comhub.ini 的 [ssh]")
    ap.add_argument("--tftp-root", help="TFTP 根目錄（預設依序：comhub.ini 的 tftp_root、環境變數 COMTOOL_TFTP_ROOT、"
                                        "tftpd64.ini 的 BaseDirectory）")
    ap.add_argument("--max-size", type=int, default=256, help="傳檔大小上限（MB），預設 256")
    a = ap.parse_args()
    ini_specs, ini_tftp = load_ini()
    specs = a.ssh or ini_specs
    if IS_WIN and not specs and not a.listen:
        sys.exit(f"找不到 {INI}，不知道要連哪台 build server。\n\n"
                 "請在 build server 上的 Claude Code 輸入：\n"
                 "    請依照 INSTALL.md 幫我產生 comhub.ini\n"
                 "產生後，重新用 scp -r 把 repo 複製到這台 Windows，再執行 start_comtool.bat。\n\n"
                 f"也可以照下面的範本，自己在 {HERE} 建立 comhub.ini（UTF-8）：\n\n{INI_TEMPLATE}\n")
    tunnels = [parse_ssh_spec(x) for x in specs]

    if a.listen:
        addr = parse_addr(a.listen)
    elif IS_WIN:
        addr = ("tcp", HUB_TCP_PORT)
    else:
        if not shutil.which("powershell.exe"):
            sys.exit("這台機器找不到 powershell.exe，不能開 COM port。SSH 模式的 hub 要在本機 Windows 執行；"
                     "只是測試的話，請用 --listen 指定其他位置")
        addr = ("unix", DEFAULT_SOCK)

    if tunnels and addr[0] != "tcp":
        sys.exit("--ssh 只能搭配 tcp 監聽（SSH 模式的 hub 在本機 Windows 執行，預設 tcp:7000）")

    if a.tftp_root:
        root, src = to_local_path(a.tftp_root), "--tftp-root"
    elif ini_tftp:
        root, src = to_local_path(ini_tftp), "comhub.ini 的 tftp_root"
    else:
        root, src = detect_tftp_root()

    os.makedirs(LOGDIR, exist_ok=True)
    _log_file = open(os.path.join(LOGDIR, "comhub.log"), "a", encoding="utf-8")
    ls = make_listener(addr)
    hub = Hub(addr, root, src, a.max_size << 20)

    def stop(*_):
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop)

    log(f"comhub 啟動：{PLATFORM}，監聽 {fmt_addr(addr)}，TFTP 根目錄 {root or '未設定'}（{src}）")
    if root and not os.path.isdir(root):
        log(f"警告：TFTP 根目錄不存在: {root}")
    atexit.register(stop_tunnels)
    for host, sock in tunnels:
        t = Tunnel(host, sock, addr[1])
        _tunnels.append(t)
        log(f"[tunnel {host}] 建立 tunnel：{host}:{sock} → 127.0.0.1:{addr[1]}")
        t.start()
    while True:
        try:
            s, _ = ls.accept()
        except socket.timeout:
            continue
        threading.Thread(target=hub.handle, args=(s,), daemon=True).start()


if __name__ == "__main__":
    main()
