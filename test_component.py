#!/usr/bin/env python3
"""Tests for the Kiln OS Moonraker component
(moonraker/components/tailscale.py) with a stand-in for Moonraker and
a fake tailscaled (tests/fake_tailscaled.py: tailscaled's flags and the
LocalAPI over its unix socket). Covered: the memory guard and its
override, the warnings, the first start's settings and the login link, the
restart after Moonraker restarts and after tailscaled stops, the OOM
switch-off, logout and login again, nice and oom_score_adj, the parent
death signal, the plugin.cfg macros. The real tailscaled is tested on a
printer (README.md, "Testing on a printer").
"""
import asyncio
import configparser
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
COMPONENT = os.path.join(HERE, "moonraker", "components", "tailscale.py")
FAKE = os.path.join(HERE, "tests", "fake_tailscaled.py")
spec = importlib.util.spec_from_file_location("tailscale_component",
                                              COMPONENT)
kt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kt)
kt.BACKOFF_MIN = 0.2
kt.SOCKET_WAIT = 10.

FAILS = []
PASSES = 0


def check(name, cond):
    global PASSES
    if cond:
        PASSES += 1
        print("[PASS] " + name)
    else:
        FAILS.append(name)
        print("[FAIL] " + name)


class Server:
    def __init__(self, data_path):
        self.data_path = data_path
        self.warnings = {}
        self.events = []
        self.endpoints = {}
        self.remote = {}
        self.notifications = []

    def get_app_args(self):
        return {"data_path": self.data_path}

    def register_endpoint(self, uri, methods, cb, **kw):
        self.endpoints[uri] = (methods, cb)

    def register_remote_method(self, name, cb):
        self.remote[name] = cb

    def register_notification(self, name, notify_name=None):
        # Moonraker: notify_<notify_name>, or the part after ":"
        self.notifications.append(notify_name or name.split(":")[-1])

    def add_warning(self, msg, warn_id=None, log=True, exc_info=None):
        self.warnings[warn_id] = msg

    def remove_warning(self, warn_id):
        self.warnings.pop(warn_id, None)

    def send_event(self, name, *args):
        self.events.append((name, args))

    def console(self):
        return [a[0] for n, a in self.events if n == "server:gcode_response"]


class Config:
    def __init__(self, server, opts):
        self.server, self.opts = server, opts

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        return self.opts.get(key, default)

    def getboolean(self, key, default=None):
        return bool(self.opts.get(key, default))


class Env:
    """A data path, a memory file, a socket and the fake binary."""

    def __init__(self, mem="extended", hw=512):
        self.dir = tempfile.mkdtemp(prefix="kts-", dir="/tmp")
        self.data = os.path.join(self.dir, "pd")
        self.state = os.path.join(self.data, "tailscale")
        os.makedirs(self.state)
        self.mem = os.path.join(self.dir, "mem.conf")
        if mem is not None:
            with open(self.mem, "w") as f:
                f.write("MEM_KB=480000\nLOW_MEM=0\nHW_MEM_MIB=%d\n"
                        "MEM_MODE=%s\nMEM_MODE_FIXED=0\n" % (hw, mem))
        self.vmstat = os.path.join(self.state, "fake-vmstat")
        with open(self.vmstat, "w") as f:
            f.write("oom_kill 3\n")
        self.binary = os.path.join(self.dir, "tailscaled")
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\nexec %s -I %s \"$@\"\n" % (sys.executable,
                                                          FAKE))
        os.chmod(self.binary, 0o755)
        self.sock = os.path.join(self.dir, "s", "ts.sock")

    def component(self, **opts):
        srv = Server(self.data)
        o = {"binary": self.binary, "memory_conf": self.mem,
             "socket": self.sock, "hostname": "kiln-test"}
        o.update(opts)
        c = kt.load_component(Config(srv, o))
        c.vmstat = self.vmstat
        return srv, c

    def mode(self, mode):
        with open(os.path.join(self.state, "fake-mode"), "w") as f:
            f.write(mode)

    def calls(self, kind=None):
        try:
            with open(os.path.join(self.state, "calls.jsonl")) as f:
                rows = [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []
        return [r for r in rows if kind is None or r["call"] == kind]

    def login(self):
        open(os.path.join(self.state, "login-ok"), "w").close()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


async def wait_for(cond, timeout=10.):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def test_units():
    g = kt.memory_guard({"MEM_MODE": "extended", "HW_MEM_MIB": "512"}, False)
    check("guard: extended -> ok", g["ok"] and not g["override"])
    g = kt.memory_guard({"MEM_MODE": "standard", "HW_MEM_MIB": "512"}, False)
    check("guard: standard -> blocked", not g["ok"])
    check("guard message: how to turn on Extended memory",
          "KILN_MEMORY_MODE ENABLE=1" in kt.guard_message(g))
    g = kt.memory_guard({"MEM_MODE": "standard", "HW_MEM_MIB": "256"}, False)
    check("guard message: 256 MiB has no Extended memory",
          "256 MiB" in kt.guard_message(g)
          and "KILN_MEMORY_MODE" not in kt.guard_message(g))
    g = kt.memory_guard({"MEM_MODE": "standard", "HW_MEM_MIB": "256"}, True)
    check("guard: override -> ok, marked", g["ok"] and g["override"])
    g = kt.memory_guard({}, False)
    check("guard: no memory file -> blocked, unknown",
          not g["ok"] and "unknown" in g["reason"])
    check("oom: SIGKILL + counter up",
          kt.was_oom_killed(-signal.SIGKILL, 3, 4))
    check("oom: SIGKILL, counter same -> no",
          not kt.was_oom_killed(-signal.SIGKILL, 3, 3))
    check("oom: exit 1 -> no", not kt.was_oom_killed(1, 3, 4))
    check("oom: no counter -> no",
          not kt.was_oom_killed(-signal.SIGKILL, None, None))
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("nr_free_pages 1\noom_kill 12\n")
    check("oom_kill_count reads /proc/vmstat format",
          kt.oom_kill_count(f.name) == 12)
    os.unlink(f.name)
    check("redact login link",
          kt.redact("visit https://login.tailscale.com/a/abc123def now")
          == "visit [login link] now")
    check("default hostname: kiln- prefix, DNS-safe",
          kt.default_hostname().startswith("kiln-")
          and all(c.isalnum() or c == "-" for c in kt.default_hostname()))
    p = kt.safe_prefs("kiln-x", "")
    check("safe prefs: no SSH, no exit node, no routes, shields down",
          p["RunSSH"] is False and p["RunSSHSet"] and p["ExitNodeID"] == ""
          and p["RouteAll"] is False and p["ShieldsUp"] is False
          and p["AdvertiseRoutes"] == [] and "ControlURL" not in p)
    p = kt.start_prefs("kiln-x", "https://hs.example.com")
    check("start prefs: WantRunning, login server, no *Set keys",
          p["WantRunning"] and p["ControlURL"] == "https://hs.example.com"
          and not any(k.endswith("Set") for k in p))
    conf = {"MEM_MODE": "extended", "HW_MEM_MIB": "512"}
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("# x\nMEM_KB=1\nHW_MEM_MIB=512\nMEM_MODE=\"extended\"\n")
    check("read_memory_conf: keys, quotes",
          {k: v for k, v in kt.read_memory_conf(f.name).items()
           if k in conf} == conf)
    os.unlink(f.name)


def test_parse_ports():
    for text, want in (("", None), ("  ", None), ("80", [80]),
                       ("80, 7912", [80, 7912]), ("7912 80 80", [80, 7912]),
                       ("80,,7912\t81", [80, 81, 7912]), ("65535", [65535])):
        got, err = kt.parse_ports(text)
        check("ports %r -> %r" % (text, want), got == want and err == "")
    for text in ("0", "65536", "http", "80;81", "-1", "80, x", "1e3",
                 "\u00b2", "080080"):
        got, err = kt.parse_ports(text)
        check("ports %r -> error" % text, got is None and err)


async def test_ports():
    env = Env()
    os.environ[kt.PORTS_ENV] = "1"  # from outside: never passed on
    try:
        srv, c = env.component()
        await c.component_init()
        await wait_for(lambda: c.get_status()["state"] == "needs_login")
        started = env.calls("started")
        check("ports unset: no TS_KILN_ALLOWED_PORTS for tailscaled",
              started and started[-1]["allowed_ports"] is None)
        check("ports unset: status ports None (every port)",
              c.get_status()["ports"] is None)
        await c.close()
    finally:
        del os.environ[kt.PORTS_ENV]
    with open(os.path.join(env.state, "fake-state"), "w") as f:
        f.write("node key\n")
    srv, c = env.component(ports="80, 7912 80")
    await c.component_init()
    await wait_for(lambda: c.get_status()["state"] == "running")
    started = env.calls("started")
    check("ports 80, 7912: TS_KILN_ALLOWED_PORTS=80,7912",
          started[-1]["allowed_ports"] == "80,7912")
    check("ports: status lists them", c.get_status()["ports"] == [80, 7912])
    await c._remote_status()
    check("ports: console status lists them",
          any("Reachable ports: 80, 7912" in l for l in srv.console()))
    await c.close()
    # A changed list: the next Moonraker start runs tailscaled with it
    srv, c = env.component(ports="80")
    await c.component_init()
    await wait_for(lambda: len(env.calls("started")) == 3)
    check("ports changed: tailscaled started again with the new list",
          env.calls("started")[-1]["allowed_ports"] == "80")
    await c.close()
    srv, c = env.component(ports="80, 7912x")
    await c.component_init()
    await asyncio.sleep(0.5)
    st = c.get_status()
    check("ports invalid: not started", st["state"] == "config_error"
          and len(env.calls("started")) == 3)
    check("ports invalid: warning names the entry",
          "7912x" in srv.warnings.get("tailscale_ports", ""))
    await srv.remote["tailscale_login"]()
    check("ports invalid: TAILSCALE_LOGIN says why",
          any("7912x" in l for l in srv.console()))
    await c.close()
    env.cleanup()


async def test_memory_guard():
    for mem, hw in (("standard", 512), ("standard", 256), (None, 512)):
        env = Env(mem=mem, hw=hw)
        srv, c = env.component()
        await c.component_init()
        await asyncio.sleep(0.5)
        st = c.get_status()
        check("guard %s/%s: not started" % (mem, hw),
              st["state"] == "memory_guard" and not env.calls("started"))
        check("guard %s/%s: warning" % (mem, hw),
              "tailscale_memory" in srv.warnings
              and "allow_low_memory: True" in srv.warnings[
                  "tailscale_memory"])
        await c._remote_status()
        check("guard %s/%s: console status says why" % (mem, hw),
              any("not started" in l for l in srv.console()))
        await c.close()
        env.cleanup()


async def test_override():
    env = Env(mem="standard")
    srv, c = env.component(allow_low_memory=True)
    await c.component_init()
    ok = await wait_for(lambda: c.get_status()["state"] == "needs_login")
    check("override: started, waiting for login", ok)
    check("override: persistent low-memory warning",
          srv.warnings.get("tailscale_low_memory") ==
          kt.LOW_MEMORY_WARNING and "may cause slowdowns or memory errors "
          "during prints" in kt.LOW_MEMORY_WARNING)
    check("override: no guard warning",
          "tailscale_memory" not in srv.warnings)
    check("override: status marks it",
          c.get_status()["memory_guard"]["override"])
    await c.close()
    env.cleanup()


async def test_first_start_login_restart():
    env = Env()
    srv, c = env.component()
    check("endpoints registered", set(srv.endpoints) == {
        "/server/tailscale/status", "/server/tailscale/login",
        "/server/tailscale/logout"})
    check("remote methods registered", set(srv.remote) == {
        "tailscale_status", "tailscale_login",
        "tailscale_logout"})
    await c.component_init()
    ok = await wait_for(lambda: c.get_status()["login_url"])
    st = c.get_status()
    check("first start: login link in status", ok and
          st["login_url"] == "https://login.tailscale.com/a/0123456789abcdef"
          and st["state"] == "needs_login")
    check("first start: login warning names the link",
          st["login_url"] in srv.warnings.get("tailscale_login", ""))
    check("first start: link in the console",
          any(st["login_url"] in l for l in srv.console()))
    check("first start: no memory warnings", not any(
        w in srv.warnings for w in ("tailscale_memory",
                                    "tailscale_low_memory")))
    started = env.calls("started")
    check("tailscaled flags", started and all(a in started[0]["argv"] for a in (
        "--tun=userspace-networking", "--statedir=" + env.state,
        "--socket=" + env.sock, "--port=0")))
    check("nice 19", started and started[0]["nice"] == 19)
    check("oom_score_adj 1000", started and started[0]["oom_score_adj"] == 1000)
    check("GOMEMLIMIT and TS_LOGS_DIR set", started and
          started[0]["gomemlimit"] == "48MiB" and
          started[0]["ts_logs_dir"] == env.state)
    start = env.calls("start")
    prefs = start[0]["body"]["UpdatePrefs"] if start else {}
    check("first start: Start with safe prefs",
          prefs.get("WantRunning") is True and prefs.get("RunSSH") is False
          and prefs.get("ShieldsUp") is False and prefs.get("RouteAll") is False
          and prefs.get("ExitNodeID") == "" and prefs.get("CorpDNS") is False
          and prefs.get("Hostname") == "kiln-test")
    check("first start: one login-interactive",
          len(env.calls("login-interactive")) == 1)
    check("watch-ipn-bus with NotifyInitialState|NotifyNoNetMap",
          any(r["path"].endswith("mask=%d" % kt.WATCH_MASK)
              for r in env.calls("watch")))
    check("moonraker log: login link redacted",
          not any("0123456789abcdef" in l for l in c.log_lines)
          and any("[login link]" in l for l in c.log_lines))
    check("notification name notify_tailscale_status_changed",
          srv.notifications == ["tailscale_status_changed"])
    check("status notification sent", any(
        n == "tailscale:status_changed" for n, a in srv.events))

    env.login()
    ok = await wait_for(lambda: c.get_status()["state"] == "running")
    st = c.get_status()
    check("after login: running, tailnet IP and name", ok and
          st["tailnet_ip"] == "100.64.0.7" and
          st["dns_name"] == "kiln-test.example-tailnet.ts.net" and
          st["tailnet"] == "user@example.com" and st["login_url"] is None)
    check("after login: login warning gone",
          "tailscale_login" not in srv.warnings)
    await c._remote_status()
    check("console status: connected + Fluidd address", any(
        "connected as kiln-test.example-tailnet.ts.net (100.64.0.7)" in l
        and "http://kiln-test.example-tailnet.ts.net/" in l
        for l in srv.console()))
    await c.close()
    check("close: tailscaled got SIGTERM", len(env.calls("sigterm")) == 1)

    # Moonraker restarts: the node is logged in
    srv, c = env.component()
    await c.component_init()
    ok = await wait_for(lambda: c.get_status()["state"] == "running")
    check("restart: running again without a login", ok and
          len(env.calls("login-interactive")) == 1)
    check("restart: safe prefs as PATCH, not Start",
          len(env.calls("start")) == 1 and env.calls("prefs") and
          env.calls("prefs")[-1]["body"]["RunSSH"] is False and
          env.calls("prefs")[-1]["body"]["WantRunning"] is True)

    # tailscaled stops: restarted with the back-off
    env.mode("exit1")
    c.proc.kill()
    ok = await wait_for(lambda: c.get_status()["state"] == "running"
                        and c.restarts >= 2)
    check("tailscaled stopped twice: restarted, running", ok)
    check("exit 1 seen", any(r["mode"] == "exit1"
                             for r in env.calls("started")))
    check("killed without OOM: no switch-off",
          not os.path.exists(os.path.join(env.state, kt.OOM_MARKER)))
    await c.close()
    env.cleanup()


async def test_oom():
    env = Env()
    # logged in from before
    with open(os.path.join(env.state, "fake-state"), "w") as f:
        f.write("node key\n")
    srv, c = env.component()
    env.mode("oom")
    await c.component_init()
    ok = await wait_for(lambda: c.get_status()["state"] == "oom_off")
    check("OOM: switched off", ok)
    check("OOM: marker written",
          os.path.exists(os.path.join(env.state, kt.OOM_MARKER)))
    check("OOM: warning", "tailscale_oom" in srv.warnings)
    await asyncio.sleep(1.)
    check("OOM: not restarted", len(env.calls("started")) == 1)
    check("OOM: console says so",
          any("ran out of memory" in l for l in srv.console()))
    await c.close()

    # The next start (Moonraker restart, reboot): stays off
    srv, c = env.component()
    await c.component_init()
    await asyncio.sleep(0.5)
    check("OOM: next start stays off", c.get_status()["state"] == "oom_off"
          and len(env.calls("started")) == 1
          and "tailscale_oom" in srv.warnings)
    env.mode("normal")
    await srv.remote["tailscale_login"]()
    ok = await wait_for(lambda: c.get_status()["state"] == "running")
    check("OOM: TAILSCALE_LOGIN starts it again", ok and
          "tailscale_oom" not in srv.warnings and
          not os.path.exists(os.path.join(env.state, kt.OOM_MARKER)))
    await c.close()
    env.cleanup()


async def test_sigkill_not_oom():
    env = Env()
    srv, c = env.component()
    env.mode("sigkill")
    await c.component_init()
    ok = await wait_for(lambda: c.restarts >= 1 and
                        c.get_status()["state"] == "needs_login")
    check("SIGKILL without OOM: restarted", ok and c.restarts >= 1)
    check("SIGKILL without OOM: no switch-off",
          not os.path.exists(os.path.join(env.state, kt.OOM_MARKER)))
    await c.close()
    env.cleanup()


async def test_logout():
    env = Env()
    srv, c = env.component()
    await c.component_init()
    await wait_for(lambda: c.get_status()["login_url"])
    env.login()
    await wait_for(lambda: c.get_status()["state"] == "running")
    st = await srv.endpoints["/server/tailscale/logout"][1](None)
    check("logout: LocalAPI logout called", len(env.calls("logout")) == 1)
    check("logout: stopped, logged_out", st["state"] == "logged_out"
          and not st["daemon"]["running"])
    await asyncio.sleep(0.5)
    check("logout: not restarted", len(env.calls("started")) == 1)
    await c.close()
    srv, c = env.component()
    await c.component_init()
    await asyncio.sleep(0.5)
    check("logout: next start stays off",
          c.get_status()["state"] == "logged_out"
          and len(env.calls("started")) == 1)
    await srv.endpoints["/server/tailscale/login"][1](None)
    ok = await wait_for(lambda: c.get_status()["login_url"])
    check("login again: new link", ok and
          len(env.calls("login-interactive")) == 2)
    await c.close()
    # logout from the console
    srv, c = env.component()
    await c.component_init()
    await wait_for(lambda: c.get_status()["state"] == "needs_login")
    await srv.remote["tailscale_logout"]()
    check("TAILSCALE_LOGOUT: logged out, console says how to remove",
          c.get_status()["state"] == "logged_out" and
          any("admin console" in l for l in srv.console()))
    await c.close()
    env.cleanup()


async def test_errors():
    env = Env()
    srv, c = env.component(binary=os.path.join(env.dir, "nope"))
    await c.component_init()
    await asyncio.sleep(0.3)
    check("missing binary: error state and warning",
          c.get_status()["state"] == "error" and
          "tailscale_error" in srv.warnings)
    await c.close()
    srv, c = env.component(socket="/tmp/" + "x" * 120 + "/ts.sock")
    await c.component_init()
    await asyncio.sleep(0.3)
    check("socket path too long: error",
          "too long" in c.get_status()["error"])
    await c.close()
    env.cleanup()


def test_parent_death():
    """Moonraker killed (SIGKILL): tailscaled goes too (PR_SET_PDEATHSIG)."""
    env = Env()
    script = os.path.join(env.dir, "harness.py")
    with open(script, "w") as f:
        f.write("""
import asyncio, importlib.util, sys, time
sys.path.insert(0, %r)
import test_component as t
async def main():
    env = t.Env.__new__(t.Env)
    env.__dict__.update(%r)
    srv, c = env.component()
    await c.component_init()
    await t.wait_for(lambda: c.proc is not None)
    print(c.proc.pid, flush=True)
    await asyncio.sleep(60)
asyncio.run(main())
""" % (HERE, {k: v for k, v in env.__dict__.items()}))
    h = subprocess.Popen([sys.executable, script], stdout=subprocess.PIPE)
    pid = int(h.stdout.readline())
    time.sleep(0.5)
    alive = os.path.exists("/proc/%d" % pid)
    h.kill()
    h.wait()
    end = time.monotonic() + 5
    while os.path.exists("/proc/%d" % pid) and time.monotonic() < end:
        time.sleep(0.05)
    gone = not os.path.exists("/proc/%d" % pid) or \
        open("/proc/%d/stat" % pid).read().split()[2] == "Z"
    check("parent death: tailscaled stops with a killed Moonraker",
          alive and gone)
    if not gone:
        os.kill(pid, signal.SIGKILL)
    env.cleanup()


def test_plugin_files():
    cfg = configparser.ConfigParser(strict=False, interpolation=None)
    cfg.read(os.path.join(HERE, "plugin.cfg"))
    macros = {s.split(None, 1)[1]: cfg[s]["gcode"] for s in cfg.sections()
              if s.startswith("gcode_macro ")}
    for name, method in (("TAILSCALE_STATUS", "tailscale_status"),
                         ("TAILSCALE_LOGIN", "tailscale_login"),
                         ("TAILSCALE_LOGOUT", "tailscale_logout")):
        check("plugin.cfg: %s calls %s" % (name, method),
              'action_call_remote_method("%s")' % method in macros.get(name, ""))
    mc = configparser.ConfigParser(strict=False, interpolation=None)
    mc.read(os.path.join(HERE, "moonraker.conf"))
    check("moonraker.conf: [tailscale] without options",
          mc.has_section("tailscale") and not mc.options("tailscale"))
    um = mc["update_manager kiln-tailscale"]
    check("moonraker.conf: update_manager",
          um["origin"] == "https://github.com/djvalic/kiln-tailscale.git" and
          um["path"] == "/data/ad5x/plugins/tailscale" and
          um["managed_services"] == "moonraker")
    with open(os.path.join(HERE, "plugin.json")) as f:
        meta = json.load(f)
    check("plugin.json: name tailscale", meta["name"] == "tailscale")


async def main_async():
    await test_memory_guard()
    await test_ports()
    await test_override()
    await test_first_start_login_restart()
    await test_oom()
    await test_sigkill_not_oom()
    await test_logout()
    await test_errors()


def main():
    test_units()
    test_parse_ports()
    test_plugin_files()
    asyncio.run(main_async())
    test_parent_death()
    print("\n%d passed, %d failed" % (PASSES, len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
