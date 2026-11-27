#!/usr/bin/env python3
"""Windows COM port <-> TCP bridge daemon（可在 WSL 或 Windows 原生 Python 執行）。

usage: comd COMx [--baud 115200] [--port 7000+x]

  由 powershell.exe 執行 bridge.ps1 開啟 COM port，透過 stdio 轉送資料。
  監聽 127.0.0.1:<port>，可多個 client 同時連線：
    - COM port 收到的資料 -> 附加寫入 log/COMx.log，並廣播給所有 client
    - 任一 client 送來的資料 -> 寫入 COM port
  client 連上時主動送 IAC WILL ECHO / WILL SGA（由設備負責回顯）；收到 IAC 的 client 視為 telnet，
  其餘 client 原樣收送（連線開頭會多收到這 6 個協商位元組，comsend 會自動濾掉）。
"""
import argparse, atexit, os, re, signal, socket, subprocess, sys, threading

HERE = os.path.dirname(os.path.realpath(__file__))
IAC, SB, SE = 255, 250, 240
WILL, WONT, DO, DONT = 251, 252, 253, 254
ECHO, SGA = 1, 3
PENDING_MAX = 1 << 20    # 一個 client 累積超過這麼多沒送出的資料，就視為卡住並斷線


class Client:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.telnet = False      # 收到過 IAC 才視為 telnet client
        self.state = 0           # 0:data 1:after IAC 2:option byte 3:in SB 4:SB after IAC
        self.last_cr = False
        # 送出佇列：rx 執行緒只把資料放進 pending，由這個 client 自己的 writer 執行緒送出，
        # 一個不讀資料的 client 不會卡住 rx（否則 log、其他 client、甚至 COM port 都會被拖住）
        self.pending = bytearray()
        self.cv = threading.Condition()
        self.closed = False
        self.stalled = False

    def send(self, data):
        """放進送出佇列後立刻回傳；佇列太大表示 client 卡住，關閉連線（由 client_loop 走 drop）。"""
        if self.telnet:
            data = data.replace(b"\xff", b"\xff\xff")
        with self.cv:
            if self.closed:
                return
            if len(self.pending) + len(data) > PENDING_MAX:
                self.stalled = True
                self.close()
                return
            self.pending += data
            self.cv.notify()

    def writer(self):
        while True:
            with self.cv:
                while not self.pending and not self.closed:
                    self.cv.wait()
                if self.closed:
                    return
                data = bytes(self.pending)
                self.pending.clear()
            try:
                self.sock.sendall(data)   # socket timeout 5 秒，只會卡住這個 client 自己
            except OSError:
                self.close()
                return

    def close(self):
        """結束 writer 並讓 client_loop 的 recv 失敗；可重複呼叫。"""
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def filter(self, data):
        """去掉 telnet 協商位元組；telnet 模式下把 CR LF / CR NUL 轉成 CR。"""
        out = bytearray()
        for b in data:
            st = self.state
            if st == 0:
                if b == IAC:
                    self.state = 1
                    self.telnet = True
                    continue
                if self.telnet and self.last_cr and b in (0, 10):
                    self.last_cr = False
                    continue
                self.last_cr = (b == 13)
                out.append(b)
            elif st == 1:
                if b == IAC:
                    out.append(IAC); self.state = 0
                elif b in (WILL, WONT, DO, DONT):
                    self.state = 2
                elif b == SB:
                    self.state = 3
                else:
                    self.state = 0
            elif st == 2:
                self.state = 0
            elif st == 3:
                if b == IAC:
                    self.state = 4
            elif st == 4:
                self.state = 0 if b == SE else 3
        return bytes(out)


def main():
    ap = argparse.ArgumentParser(description="Windows COM port <-> TCP bridge")
    ap.add_argument("com", help="Windows COM port，例如 COM3")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--port", type=int, help="TCP port，預設 7000+N")
    a = ap.parse_args()

    com = a.com.upper()
    m = re.fullmatch(r"COM(\d+)", com)
    if not m:
        sys.exit("COM port 名稱錯誤: " + a.com)
    tcp_port = a.port or 7000 + int(m.group(1))

    logdir = os.path.join(HERE, "log")
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, com + ".log"), "ab", buffering=0)
    pidfile = os.path.join(logdir, com + ".pid")

    def rm_pidfile():
        try:
            with open(pidfile) as f:
                if f.read().split()[0] == str(os.getpid()):
                    os.remove(pidfile)
        except (OSError, IndexError):
            pass
    atexit.register(rm_pidfile)

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":  # Windows 的 SO_REUSEADDR 會讓別的程式搶走同一個 port，不能設
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lsock.bind(("127.0.0.1", tcp_port))
    except OSError as e:
        sys.exit(f"TCP port {tcp_port} 無法使用: {e}")
    lsock.listen(8)
    # accept 用短 timeout 輪詢：讓 Windows 上的 Ctrl+C 能及時處理，也能察覺 bridge 已結束
    lsock.settimeout(0.5)

    if sys.platform == "win32":
        ps1 = os.path.join(HERE, "bridge.ps1")
    else:
        ps1 = subprocess.check_output(["wslpath", "-w", os.path.join(HERE, "bridge.ps1")]).decode().strip()
    pr = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, com, str(a.baud)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def stop(*_):
        try:
            pr.stdin.close()
        except Exception:
            pass
        pr.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop)

    # Windows 的 select 不能用在 pipe 上，所以改用執行緒：
    #   rx 執行緒讀 bridge stdout、err 執行緒讀 bridge stderr、每個 client 一個執行緒，主執行緒 accept
    clients = {}
    clients_lock = threading.Lock()   # 保護 clients
    stdin_lock = threading.Lock()     # 多個 client 同時寫入 bridge stdin
    done = threading.Event()          # bridge 結束

    def drop(c):
        with clients_lock:
            if clients.pop(c.sock, None) is None:
                return
            n = len(clients)
        c.close()
        try:
            c.sock.close()
        except OSError:
            pass
        why = "太久沒有讀取資料，已斷線" if c.stalled else "離線"
        print(f"client {c.addr[0]}:{c.addr[1]} {why}（目前 {n} 個）", flush=True)

    def rx_loop():
        while True:
            try:
                data = os.read(pr.stdout.fileno(), 4096)
            except OSError:
                data = b""
            if not data:
                break
            log.write(data)
            with clients_lock:
                targets = list(clients.values())
            for c in targets:
                c.send(data)   # 只放進該 client 的送出佇列，不會阻塞
        rc = pr.wait()
        if rc >= 1 << 31:   # Windows 的結束碼是 32 位元無號數，轉回有號數（例如 -1）
            rc -= 1 << 32
        print(f"{com} 橋接結束（exit {rc}）", flush=True)
        done.set()

    def err_loop():
        for raw in iter(pr.stderr.readline, b""):
            line = raw.decode(errors="replace").strip().lstrip("﻿")
            if line.startswith("OPENED"):
                # pidfile 內容: "<pid> <tcp_port> <baud>"，供 comhub.py 判斷 daemon 狀態
                with open(pidfile, "w") as f:
                    f.write(f"{os.getpid()} {tcp_port} {a.baud}\n")
                print(f"READY {com}@{a.baud} tcp://127.0.0.1:{tcp_port}", flush=True)
            elif line:
                print(line, flush=True)

    def client_loop(c):
        while True:
            try:
                data = c.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                data = b""
            if not data:
                break
            data = c.filter(data)
            if data:
                try:
                    with stdin_lock:
                        pr.stdin.write(data)
                        pr.stdin.flush()
                except (OSError, ValueError):
                    break
        drop(c)

    threading.Thread(target=rx_loop, daemon=True).start()
    threading.Thread(target=err_loop, daemon=True).start()

    while not done.is_set():
        try:
            s, addr = lsock.accept()
        except socket.timeout:
            continue
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(5)
        # 主動協商：由 server 負責 echo，client 進入逐字模式
        # （Windows telnet 不會主動協商，不先送的話它會在本機回顯，字元變兩個）
        try:
            s.sendall(bytes([IAC, WILL, ECHO, IAC, WILL, SGA]))
        except OSError:
            s.close()
            continue
        c = Client(s, addr)
        with clients_lock:
            clients[s] = c
            n = len(clients)
        threading.Thread(target=c.writer, daemon=True).start()
        threading.Thread(target=client_loop, args=(c,), daemon=True).start()
        print(f"client {addr[0]}:{addr[1]} 連線（目前 {n} 個）", flush=True)


if __name__ == "__main__":
    main()
