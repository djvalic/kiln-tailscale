#!/usr/bin/env python3
"""A stand-in for tailscaled, for test_component.py.

Takes tailscaled's flags (--statedir, --socket, ...) and serves the part of
the LocalAPI the component uses over the unix socket: status, prefs
(PATCH), start, login-interactive, logout, watch-ipn-bus. Like the real
LocalAPI it refuses a wrong Host header and requests with Origin or
Referer.

Driven by files in the state directory:
  fake-mode       one word, read at start: "normal", "exit1" (exit 1 at
                  once, then "normal"), "sigkill" (SIGKILL itself after
                  start, then "normal"), "oom" (raise the oom_kill counter
                  in fake-vmstat, then SIGKILL itself; stays "oom")
  login-ok        created by the test: the user opened the login link
  fake-state      written here: logged in (the node key)
  calls.jsonl     written here: one line per LocalAPI call and at start
"""
import json
import os
import signal
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

AUTH_URL = "https://login.tailscale.com/a/0123456789abcdef"


def parse_args(argv):
    args = {}
    for a in argv:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            args[k] = v
        elif a.startswith("--"):
            args[a[2:]] = True
    return args


ARGS = parse_args(sys.argv[1:])
STATEDIR = ARGS["statedir"]
SOCK = ARGS["socket"]
LOCK = threading.Lock()


def path(name):
    return os.path.join(STATEDIR, name)


def record(kind, **kw):
    kw["call"] = kind
    with LOCK:
        with open(path("calls.jsonl"), "a") as f:
            f.write(json.dumps(kw) + "\n")


def read_mode():
    try:
        with open(path("fake-mode")) as f:
            return f.read().strip() or "normal"
    except OSError:
        return "normal"


def write_mode(mode):
    with open(path("fake-mode"), "w") as f:
        f.write(mode)


class Backend:
    def __init__(self):
        self.logged_in = os.path.exists(path("fake-state"))
        self.state = "Starting" if self.logged_in else "NoState"
        self.auth_url = ""
        self.prefs = {}
        self.version = 0
        self.cond = threading.Condition()
        if self.logged_in:
            self.set_state("Running")

    def set_state(self, state):
        with self.cond:
            self.state = state
            self.version += 1
            self.cond.notify_all()

    def status(self):
        st = {
            "Version": "1.104.7-fake",
            "BackendState": self.state,
            "HaveNodeKey": self.logged_in or bool(self.auth_url),
            "AuthURL": self.auth_url,
            "TailscaleIPs": None,
            "Self": {"HostName": "os-name", "DNSName": ""},
            "Health": [],
            "CurrentTailnet": None,
        }
        if self.state == "Running":
            st["TailscaleIPs"] = ["100.64.0.7", "fd7a:115c:a1e0::7"]
            st["Self"] = {"HostName": self.prefs.get("Hostname", "x"),
                          "DNSName": "kiln-test.example-tailnet.ts.net."}
            st["CurrentTailnet"] = {"Name": "user@example.com"}
        return st

    def watch_login(self):
        # The user opening the link: the test creates login-ok
        while True:
            time.sleep(0.05)
            if self.auth_url and os.path.exists(path("login-ok")):
                os.unlink(path("login-ok"))
                self.logged_in = True
                self.auth_url = ""
                with open(path("fake-state"), "w") as f:
                    f.write("node key\n")
                print("control: LoginFinished", flush=True)
                self.set_state("Running")


BACKEND = Backend()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def _send(self, code, obj=None):
        body = b"" if obj is None else (json.dumps(obj) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _check(self):
        if self.headers.get("Host") != "local-tailscaled.sock" or \
                self.headers.get("Origin") or self.headers.get("Referer"):
            self._send(403, {"error": "invalid localapi request"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        if not self._check():
            return
        if self.path.startswith("/localapi/v0/status"):
            record("status")
            self._send(200, BACKEND.status())
        elif self.path.startswith("/localapi/v0/watch-ipn-bus"):
            record("watch", path=self.path)
            self._watch()
        else:
            self._send(404, {"error": "not found"})

    def _watch(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        seen = -1
        url_sent = ""
        while True:
            with BACKEND.cond:
                if BACKEND.version == seen:
                    BACKEND.cond.wait(0.2)
                version, state, url = (BACKEND.version, BACKEND.state,
                                       BACKEND.auth_url)
            msg = {"Version": "1.104.7-fake", "State": None,
                   "BrowseToURL": None, "LoginFinished": None,
                   "ErrMessage": None}
            send = False
            if version != seen:
                seen = version
                msg["State"] = ["NoState", "InUseOtherUser", "NeedsLogin",
                                "NeedsMachineAuth", "Stopped", "Starting",
                                "Running"].index(state)
                send = True
            if url and url != url_sent:
                url_sent = url
                msg["BrowseToURL"] = url
                send = True
            if send:
                try:
                    self.wfile.write((json.dumps(msg) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    return

    def do_POST(self):
        if not self._check():
            return
        body = self._body()
        if self.path == "/localapi/v0/start":
            record("start", body=body)
            BACKEND.prefs.update((body or {}).get("UpdatePrefs") or {})
            BACKEND.set_state("Running" if BACKEND.logged_in
                              else "NeedsLogin")
            self._send(204)
        elif self.path == "/localapi/v0/login-interactive":
            record("login-interactive")
            self._send(204)
            BACKEND.auth_url = AUTH_URL
            print("Received auth URL: " + AUTH_URL, flush=True)
            BACKEND.set_state("NeedsLogin")
        elif self.path == "/localapi/v0/logout":
            record("logout")
            BACKEND.logged_in = False
            BACKEND.auth_url = ""
            try:
                os.unlink(path("fake-state"))
            except OSError:
                pass
            BACKEND.set_state("NeedsLogin")
            self._send(204)
        else:
            self._send(404, {"error": "not found"})

    def do_PATCH(self):
        if not self._check():
            return
        body = self._body()
        if self.path == "/localapi/v0/prefs":
            record("prefs", body=body)
            BACKEND.prefs.update({k: v for k, v in (body or {}).items()
                                  if not k.endswith("Set")})
            if BACKEND.logged_in and BACKEND.state != "Running":
                BACKEND.set_state("Running")
            self._send(200, BACKEND.prefs)
        else:
            self._send(404, {"error": "not found"})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    mode = read_mode()
    with open("/proc/self/oom_score_adj") as f:
        oom = int(f.read())
    record("started", argv=sys.argv[1:], nice=os.nice(0), oom_score_adj=oom,
           gomemlimit=os.environ.get("GOMEMLIMIT"),
           ts_logs_dir=os.environ.get("TS_LOGS_DIR"), mode=mode,
           pid=os.getpid())
    print("fake tailscaled starting, mode " + mode, flush=True)
    if mode == "exit1":
        write_mode("normal")
        sys.exit(1)
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = Server(SOCK, Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=BACKEND.watch_login, daemon=True).start()

    def term(signum, frame):
        record("sigterm")
        os._exit(0)
    signal.signal(signal.SIGTERM, term)
    if mode in ("sigkill", "oom"):
        time.sleep(1.0)
        if mode == "oom":
            vm = path("fake-vmstat")
            with open(vm) as f:
                n = int(f.read().split()[1])
            with open(vm, "w") as f:
                f.write("oom_kill %d\n" % (n + 1))
        else:
            write_mode("normal")
        os.kill(os.getpid(), signal.SIGKILL)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
