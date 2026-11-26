#!/usr/bin/env python3
"""Windows COM port <-> WSL TCP bridge daemon.

usage: comd COMx [--baud 9600] [--port 7000+x]

  Windows 端由 powershell.exe 執行 bridge.ps1 開啟 COM port，透過 stdio 轉送資料。
  WSL 端監聽 127.0.0.1:<port>，可多個 client 同時連線：
    - COM port 收到的資料 -> 附加寫入 log/COMx.log，並廣播給所有 client
    - 任一 client 送來的資料 -> 寫入 COM port
  client 連上時主動送 IAC WILL ECHO / WILL SGA（由設備負責回顯）；收到 IAC 的 client 視為 telnet，
  其餘 client 原樣收送（連線開頭會多收到這 6 個協商位元組，comsend 會自動濾掉）。
"""
import argparse, atexit, os, re, selectors, signal, socket, subprocess, sys

HERE = os.path.dirname(os.path.realpath(__file__))
IAC, SB, SE = 255, 250, 240
WILL, WONT, DO, DONT = 251, 252, 253, 254
ECHO, SGA = 1, 3


class Client:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.telnet = False      # 收到過 IAC 才視為 telnet client
        self.state = 0           # 0:data 1:after IAC 2:option byte 3:in SB 4:SB after IAC
        self.last_cr = False

    def send(self, data):
        if self.telnet:
            data = data.replace(b"\xff", b"\xff\xff")
        self.sock.sendall(data)

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
    ap.add_argument("--baud", type=int, default=9600)
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
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        lsock.bind(("127.0.0.1", tcp_port))
    except OSError as e:
        sys.exit(f"TCP port {tcp_port} 無法使用: {e}")
    lsock.listen(8)
    lsock.setblocking(False)

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
    signal.signal(signal.SIGHUP, stop)

    sel = selectors.DefaultSelector()
    sel.register(pr.stdout, selectors.EVENT_READ, "rx")
    sel.register(pr.stderr, selectors.EVENT_READ, "err")
    sel.register(lsock, selectors.EVENT_READ, "accept")
    clients = {}

    def drop(c):
        sel.unregister(c.sock)
        clients.pop(c.sock, None)
        c.sock.close()
        print(f"client {c.addr[0]}:{c.addr[1]} 離線（目前 {len(clients)} 個）", flush=True)

    while True:
        for key, _ in sel.select():
            tag = key.data
            if tag == "rx":
                data = os.read(pr.stdout.fileno(), 4096)
                if not data:
                    print(f"{com} 橋接結束（exit {pr.wait()}）", flush=True)
                    return
                log.write(data)
                for c in list(clients.values()):
                    try:
                        c.send(data)
                    except OSError:
                        drop(c)
            elif tag == "err":
                msg = os.read(pr.stderr.fileno(), 4096)
                if not msg:
                    sel.unregister(pr.stderr)
                    continue
                for line in msg.decode(errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("OPENED"):
                        # pidfile 內容: "<pid> <tcp_port> <baud>"，供 comtool_mcp.py 判斷 daemon 狀態
                        with open(pidfile, "w") as f:
                            f.write(f"{os.getpid()} {tcp_port} {a.baud}\n")
                        print(f"READY {com}@{a.baud} tcp://127.0.0.1:{tcp_port}", flush=True)
                    elif line:
                        print(line, flush=True)
            elif tag == "accept":
                s, addr = lsock.accept()
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
                clients[s] = c
                sel.register(s, selectors.EVENT_READ, c)
                print(f"client {addr[0]}:{addr[1]} 連線（目前 {len(clients)} 個）", flush=True)
            else:
                c = tag
                try:
                    data = c.sock.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    drop(c)
                    continue
                data = c.filter(data)
                if data:
                    pr.stdin.write(data)
                    pr.stdin.flush()


if __name__ == "__main__":
    main()
