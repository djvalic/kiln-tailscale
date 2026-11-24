# kiln-tailscale: runs Tailscale's tailscaled next to Moonraker on Kiln OS,
# so Fluidd and Moonraker are reachable from your own tailnet
#
# Kiln OS plugin (plugin API 1.4): Moonraker loads this component from the
# plugin's moonraker/components while the plugin is enabled, and restarts
# when the plugin is switched on or off. The component starts the plugin's
# bin/tailscaled as a child process (user klipper, like Moonraker) in
# userspace networking mode (Kiln's kernel has no TUN), at the lowest CPU
# priority and first in line for the OOM killer, restarts it with a back-off
# if it stops, and stops it when Moonraker stops. It talks to tailscaled's
# LocalAPI (HTTP over the unix socket) to apply safe settings and to get the
# login link.
#
# Memory guard: tailscaled is started only when the printer runs in Extended
# memory (/run/ad5x-memory.conf MEM_MODE=extended, 512 MiB printers), unless
# allow_low_memory: True. If the OOM killer stops tailscaled, Tailscale stays
# off (also after restarts) until TAILSCALE_LOGIN.
#
# [tailscale] options (all optional):
#   allow_low_memory  False; True runs Tailscale without Extended memory
#   hostname          the name in your tailnet (default kiln-<printer name>)
#   ports             only these local ports reachable from the tailnet
#                     ("80, 7912"); empty: every port. Needs the patched
#                     tailscaled (patches/0001, TS_KILN_ALLOWED_PORTS)
#   login_server      another control server (Headscale); default Tailscale
#   memory_limit      GOMEMLIMIT for tailscaled (default 48MiB)
#   state_dir         default <printer_data>/tailscale (the node's keys)
#   socket            tailscaled's LocalAPI socket
#   binary            the tailscaled program (default the plugin's bin/)
#   memory_conf       /run/ad5x-memory.conf
#
# Endpoints:  GET /server/tailscale/status
#             POST /server/tailscale/login   (again after logout or OOM)
#             POST /server/tailscale/logout
# Remote methods (the plugin.cfg macros): tailscale_status,
#   tailscale_login, tailscale_logout
# Notification: notify_tailscale_status_changed (the status)
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import collections
import ctypes
import json
import logging
import os
import re
import shlex
import signal
import socket
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Deque, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DEFAULT_DATA_PATH = "/home/klipper/printer_data"
MEMORY_CONF = "/run/ad5x-memory.conf"
RUN_DIR = "/run/moonraker"
VMSTAT = "/proc/vmstat"
LOCALAPI_HOST = "local-tailscaled.sock"
# Restart back-off: 5 s, doubling to 10 min; reset after 10 min of running
BACKOFF_MIN = 5.
BACKOFF_MAX = 600.
STABLE_AFTER = 600.
STOP_WAIT = 10.
SOCKET_WAIT = 60.
API_TIMEOUT = 15.
# Lowest CPU priority, and the first process the OOM killer takes
NICE = 19
OOM_SCORE_ADJ = 1000
PR_SET_PDEATHSIG = 1
# ipn.NotifyWatchOpt: NotifyInitialState | NotifyNoNetMap
WATCH_MASK = (1 << 1) | (1 << 13)
# tailscaled's own log lines in moonraker.log: at most this many a minute
LOG_PER_MINUTE = 30
LOG_KEEP = 40
OOM_MARKER = "kiln-oom-off"
LOGOUT_MARKER = "kiln-logged-out"

LOW_MEMORY_WARNING = (
    "kiln-tailscale: Tailscale on a printer without Extended memory: may "
    "cause slowdowns or memory errors during prints.")

PORTS_ENV = "TS_KILN_ALLOWED_PORTS"

LOGIN_URL_RE = re.compile(r"https?://[^\s\"']+/a/[0-9A-Za-z]+")


def parse_ports(text: str) -> Tuple[Optional[List[int]], str]:
    """The ports option: (None, "") for every port, (sorted ports, "") or
    (None, error). Ports separated by commas and/or spaces, 1-65535."""
    words = [w for w in re.split(r"[\s,]+", text.strip()) if w]
    if not words:
        return None, ""
    ports = set()
    for w in words:
        if not re.fullmatch(r"[0-9]{1,5}", w) or not 1 <= int(w) <= 65535:
            return None, ("ports: '%s' is not a port (1-65535); write the "
                          "ports separated by commas, like ports: 80, 7912"
                          % w)
        ports.add(int(w))
    return sorted(ports), ""


def read_memory_conf(path: str) -> Dict[str, str]:
    """KEY=VALUE lines of /run/ad5x-memory.conf (docs/memory.md)."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def memory_guard(conf: Dict[str, str], allow_low: bool) -> Dict[str, Any]:
    """Whether tailscaled may start, and why (for the status)."""
    mode = conf.get("MEM_MODE", "")
    hw = conf.get("HW_MEM_MIB", "")
    extended = mode == "extended"
    guard: Dict[str, Any] = {
        "extended_memory": extended,
        "mem_mode": mode or None,
        "hw_mem_mib": int(hw) if hw.isdigit() else None,
        "allow_low_memory": allow_low,
        "ok": extended or allow_low,
        "override": (not extended) and allow_low,
    }
    if extended:
        guard["reason"] = "Extended memory is on"
    elif not conf:
        guard["reason"] = "memory state unknown (no %s)" % MEMORY_CONF
    elif hw == "256":
        guard["reason"] = "256 MiB printer (no Extended memory)"
    else:
        guard["reason"] = "Extended memory is off"
    return guard


def guard_message(guard: Dict[str, Any]) -> str:
    if guard.get("hw_mem_mib") == 256:
        how = ("This printer has 256 MiB, which has no Extended memory. ")
    else:
        how = ("Turn on Extended memory first (KILN_MEMORY_MODE ENABLE=1 in "
               "the console, then restart the printer). ")
    return ("kiln-tailscale: Tailscale is not started: it needs Extended "
            "memory (%s). %sTo run it anyway, at the risk of slowdowns or "
            "memory errors during prints, set allow_low_memory: True in a "
            "[tailscale] section of your moonraker.conf." % (
                guard.get("reason", ""), how))


def oom_kill_count(path: str = VMSTAT) -> Optional[int]:
    try:
        with open(path, "r") as f:
            for line in f:
                if line.startswith("oom_kill "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def was_oom_killed(returncode: Optional[int], before: Optional[int],
                   after: Optional[int]) -> bool:
    """SIGKILL while the kernel's OOM kill counter went up."""
    if returncode != -signal.SIGKILL:
        return False
    if before is None or after is None:
        return False
    return after > before


def default_hostname() -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower())
    name = name.strip("-") or "printer"
    return ("kiln-" + name)[:63].rstrip("-")


def redact(text: str) -> str:
    """Login links are keys to this printer: keep them out of the log."""
    return LOGIN_URL_RE.sub("[login link]", text)


def safe_prefs(hostname: str, login_server: str) -> Dict[str, Any]:
    """ipn.MaskedPrefs: no exit node, no routes, no SSH, inbound allowed."""
    prefs: Dict[str, Any] = {
        "RouteAll": False, "RouteAllSet": True,
        "ExitNodeID": "", "ExitNodeIDSet": True,
        "ExitNodeIP": "", "ExitNodeIPSet": True,
        "CorpDNS": False, "CorpDNSSet": True,
        "RunSSH": False, "RunSSHSet": True,
        "RunWebClient": False, "RunWebClientSet": True,
        "ShieldsUp": False, "ShieldsUpSet": True,
        "AdvertiseRoutes": [], "AdvertiseRoutesSet": True,
        "Hostname": hostname, "HostnameSet": True,
    }
    if login_server:
        prefs.update({"ControlURL": login_server, "ControlURLSet": True})
    return prefs


def start_prefs(hostname: str, login_server: str) -> Dict[str, Any]:
    """ipn.Prefs for the first start (ipn.Options.UpdatePrefs)."""
    prefs = {k: v for k, v in safe_prefs(hostname, login_server).items()
             if not k.endswith("Set")}
    prefs["WantRunning"] = True
    return prefs


class LocalAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__("LocalAPI %d: %s" % (status, message))
        self.status = status


class LocalAPI:
    """tailscaled's LocalAPI: HTTP/1.0 over its unix socket (one request
    per connection, so the answer ends at EOF and is never chunked)."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def _open(self, method: str, url: str, body: Optional[bytes]
                    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_unix_connection(self.path)
        head = ["%s /localapi/v0/%s HTTP/1.0" % (method, url),
                "Host: " + LOCALAPI_HOST]
        if body is not None:
            head.append("Content-Type: application/json")
            head.append("Content-Length: %d" % len(body))
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
        if body:
            writer.write(body)
        await writer.drain()
        return reader, writer

    @staticmethod
    async def _read_head(reader: asyncio.StreamReader) -> int:
        line = await reader.readline()
        parts = line.decode("latin-1").split(None, 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise LocalAPIError(0, "bad answer %r" % line[:80])
        status = int(parts[1])
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                return status

    async def request(self, method: str, url: str, data: Any = None,
                      timeout: float = API_TIMEOUT) -> Any:
        body = None if data is None else json.dumps(data).encode()

        async def run() -> Any:
            reader, writer = await self._open(method, url, body)
            try:
                status = await self._read_head(reader)
                payload = await reader.read(8 << 20)
            finally:
                writer.close()
            text = payload.decode("utf-8", "replace")
            if status >= 400:
                raise LocalAPIError(status, text.strip()[:300])
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text
        return await asyncio.wait_for(run(), timeout)

    async def watch(self, mask: int) -> AsyncIterator[Dict[str, Any]]:
        reader, writer = await asyncio.wait_for(
            self._open("GET", "watch-ipn-bus?mask=%d" % mask, None),
            API_TIMEOUT)
        try:
            status = await asyncio.wait_for(self._read_head(reader),
                                            API_TIMEOUT)
            if status >= 400:
                raise LocalAPIError(status, "watch-ipn-bus")
            while True:
                line = await reader.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    yield msg
        finally:
            writer.close()


class KilnTailscale:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        data_path = self.server.get_app_args().get("data_path", "") \
            or DEFAULT_DATA_PATH
        self.allow_low = config.getboolean("allow_low_memory", False)
        self.hostname = config.get("hostname", "") or default_hostname()
        self.ports, self.ports_error = parse_ports(config.get("ports", ""))
        self.login_server = config.get("login_server", "").strip()
        self.memory_limit = config.get("memory_limit", "48MiB").strip()
        self.state_dir = config.get(
            "state_dir", os.path.join(data_path, "tailscale"))
        if os.access(RUN_DIR, os.W_OK):
            default_sock = os.path.join(RUN_DIR, "kiln-tailscale",
                                        "tailscaled.sock")
        else:
            default_sock = os.path.join(data_path, "comms", "tailscaled.sock")
        self.socket = config.get("socket", default_sock)
        self.binary = config.get(
            "binary", os.path.join(PLUGIN_DIR, "bin", "tailscaled"))
        self.memory_conf = config.get("memory_conf", MEMORY_CONF)
        self.vmstat = VMSTAT
        self.api = LocalAPI(self.socket)
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None
        self.control_task: Optional[asyncio.Task] = None
        self.wake = asyncio.Event()
        self.closing = False
        self.started_at = 0.
        self.restarts = 0
        self.last_exit: Optional[int] = None
        self.last_error = ""
        self.oom_killed = False
        self.login_requested = False
        self.guard: Dict[str, Any] = {}
        self.ts_status: Dict[str, Any] = {}
        self.login_url = ""
        self.log_lines: Deque[str] = collections.deque(maxlen=LOG_KEEP)
        self.log_window = (0., 0, 0)  # (minute start, logged, skipped)
        self.last_sent: Optional[Dict[str, Any]] = None
        self.server.register_endpoint(
            "/server/tailscale/status", ["GET"], self._handle_status)
        self.server.register_endpoint(
            "/server/tailscale/login", ["POST"], self._handle_login)
        self.server.register_endpoint(
            "/server/tailscale/logout", ["POST"], self._handle_logout)
        self.server.register_remote_method(
            "tailscale_status", self._remote_status)
        self.server.register_remote_method(
            "tailscale_login", self._remote_login)
        self.server.register_remote_method(
            "tailscale_logout", self._remote_logout)
        # notify_tailscale_status_changed (without the name Moonraker
        # would use the part after ":", notify_status_changed)
        self.server.register_notification(
            "tailscale:status_changed", "tailscale_status_changed")

    # Markers in the state directory (kept across Moonraker restarts)
    def _marker(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    def _has_marker(self, name: str) -> bool:
        return os.path.exists(self._marker(name))

    def _set_marker(self, name: str, text: str) -> None:
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            with open(self._marker(name), "w") as f:
                f.write(text + "\n")
        except OSError as e:
            logging.info("tailscale: marker %s: %s", name, e)

    def _clear_marker(self, name: str) -> None:
        try:
            os.unlink(self._marker(name))
        except OSError:
            pass

    def _console(self, msg: str) -> None:
        for line in msg.splitlines():
            self.server.send_event("server:gcode_response",
                                   "// tailscale: " + line)

    async def component_init(self) -> None:
        self.task = asyncio.create_task(self._supervise())

    # Why tailscaled isn't started now, or ""
    def _blocked(self) -> str:
        if self.ports_error:
            # Neither every port nor none: don't guess, don't start
            self.server.add_warning(
                "kiln-tailscale: Tailscale is not started: [tailscale] "
                "%s. Fix it in your moonraker.conf and restart Moonraker."
                % self.ports_error, warn_id="tailscale_ports")
            return "config_error"
        self.guard = memory_guard(read_memory_conf(self.memory_conf),
                                  self.allow_low)
        if not self.guard["ok"]:
            self.server.add_warning(guard_message(self.guard),
                                    warn_id="tailscale_memory")
            return "memory_guard"
        self.server.remove_warning("tailscale_memory")
        if self.guard["override"]:
            self.server.add_warning(LOW_MEMORY_WARNING,
                                    warn_id="tailscale_low_memory")
        else:
            self.server.remove_warning("tailscale_low_memory")
        if self._has_marker(OOM_MARKER):
            self.server.add_warning(
                "kiln-tailscale: tailscaled ran out of memory and was "
                "stopped by the system, so Tailscale is switched off to "
                "keep prints safe. TAILSCALE_LOGIN in the console "
                "starts it again.", warn_id="tailscale_oom")
            return "oom_off"
        self.server.remove_warning("tailscale_oom")
        if self._has_marker(LOGOUT_MARKER):
            return "logged_out"
        return ""

    def _prepare(self) -> bool:
        if not os.access(self.binary, os.X_OK):
            self.last_error = "%s is missing or not executable" % self.binary
            return False
        if len(os.fsencode(self.socket)) > 107:
            self.last_error = "socket path too long (unix sockets: 107 bytes)"
            return False
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
            sock_dir = os.path.dirname(self.socket)
            os.makedirs(sock_dir, mode=0o700, exist_ok=True)
            if os.path.exists(self.socket):
                os.unlink(self.socket)
        except OSError as e:
            self.last_error = "state or socket directory: %s" % e
            return False
        return True

    def _command(self) -> List[str]:
        return [self.binary, "--tun=userspace-networking",
                "--statedir=" + self.state_dir,
                "--socket=" + self.socket,
                "--port=0", "--no-logs-no-support"]

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.memory_limit:
            env["GOMEMLIMIT"] = self.memory_limit
        # logpolicy's directory (else ~/.local/share/tailscale)
        env["TS_LOGS_DIR"] = self.state_dir
        # Reachable local ports (patches/0001); unset: every port
        env.pop(PORTS_ENV, None)
        if self.ports:
            env[PORTS_ENV] = ",".join(str(p) for p in self.ports)
        return env

    async def _sleep(self, delay: float) -> None:
        # Sleeps until delay passed or something wakes the supervisor
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def _supervise(self) -> None:
        backoff = BACKOFF_MIN
        try:
            while not self.closing:
                blocked = self._blocked()
                if blocked:
                    self._publish()
                    await self._sleep(3600.)
                    continue
                if not self._prepare():
                    logging.info("tailscale: %s; retry in %d s",
                                 self.last_error, int(backoff))
                    self.server.add_warning(
                        "kiln-tailscale: " + self.last_error,
                        warn_id="tailscale_error")
                    self._publish()
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                self.server.remove_warning("tailscale_error")
                await self._run_once()
                if self.closing:
                    break
                if self.oom_killed:
                    self._set_marker(OOM_MARKER, time.strftime(
                        "%Y-%m-%d %H:%M:%S"))
                    logging.info("tailscale: tailscaled was OOM-killed;"
                                 " Tailscale switched off")
                    self._console("tailscaled ran out of memory; Tailscale "
                                  "is off until TAILSCALE_LOGIN")
                    continue
                if self._has_marker(LOGOUT_MARKER):
                    continue
                if time.monotonic() - self.started_at > STABLE_AFTER:
                    backoff = BACKOFF_MIN
                self.restarts += 1
                logging.info("tailscale: tailscaled stopped (exit %s); "
                             "restart in %d s", self.last_exit, int(backoff))
                self._publish()
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        except asyncio.CancelledError:
            pass

    async def _run_once(self) -> None:
        cmd = self._command()
        self.oom_killed = False
        self.login_requested = False
        self.ts_status = {}
        self._set_login_url("")
        oom_before = oom_kill_count(self.vmstat)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(), preexec_fn=self._child_setup)
        except Exception as e:
            self.last_error = "start failed: %s" % e
            logging.info("tailscale: %s", self.last_error)
            self.last_exit = None
            return
        self.started_at = time.monotonic()
        self.last_error = ""
        logging.info("tailscale: tailscaled started (pid %d): %s",
                     self.proc.pid, " ".join(shlex.quote(c) for c in cmd))
        self._publish()
        self.control_task = asyncio.create_task(self._control())
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            self._log_line(line.decode("utf-8", "replace").rstrip())
        self.last_exit = await self.proc.wait()
        self.proc = None
        if self.control_task is not None:
            self.control_task.cancel()
            self.control_task = None
        if not self.closing:
            self.oom_killed = was_oom_killed(
                self.last_exit, oom_before, oom_kill_count(self.vmstat))
        self.ts_status = {}
        self._set_login_url("")

    def _log_line(self, text: str) -> None:
        if not text:
            return
        text = redact(text)[:500]
        self.log_lines.append(text)
        now = time.monotonic()
        start, logged, skipped = self.log_window
        if now - start >= 60.:
            if skipped:
                logging.info("tailscale: (%d tailscaled lines not "
                             "logged)", skipped)
            start, logged, skipped = now, 0, 0
        if logged < LOG_PER_MINUTE:
            logging.info("tailscaled: %s", text)
            logged += 1
        else:
            skipped += 1
        self.log_window = (start, logged, skipped)

    @staticmethod
    def _child_setup() -> None:
        # Runs in the child before exec (no locks, no logging here).
        # Its own session, and SIGTERM when Moonraker dies
        os.setsid()
        try:
            ctypes.CDLL(None, use_errno=True).prctl(
                PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
        except Exception:
            pass
        try:
            # 19 whatever Moonraker's own priority is
            os.setpriority(os.PRIO_PROCESS, 0, NICE)
        except OSError:
            pass
        try:
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write(str(OOM_SCORE_ADJ))
        except OSError:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
            signal.signal(sig, signal.SIG_DFL)

    # LocalAPI side: settings, login link, state
    async def _wait_socket(self) -> bool:
        deadline = time.monotonic() + SOCKET_WAIT
        while time.monotonic() < deadline:
            if os.path.exists(self.socket):
                try:
                    await self.api.request("GET", "status?peers=false")
                    return True
                except (OSError, LocalAPIError, asyncio.TimeoutError):
                    pass
            await asyncio.sleep(1.)
        return False

    async def _control(self) -> None:
        try:
            if not await self._wait_socket():
                self.last_error = "tailscaled's LocalAPI did not answer"
                logging.info("tailscale: %s", self.last_error)
                self._publish()
                return
            await self._refresh()
            await self._bring_up()
            while not self.closing:
                try:
                    async for msg in self.api.watch(WATCH_MASK):
                        await self._on_notify(msg)
                except (OSError, LocalAPIError, asyncio.TimeoutError) as e:
                    logging.info("tailscale: watch-ipn-bus: %s", e)
                await asyncio.sleep(5.)
                await self._refresh()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("tailscale: LocalAPI")

    async def _bring_up(self) -> None:
        st = self.ts_status
        try:
            if not st.get("HaveNodeKey") and \
                    st.get("BackendState") in ("NoState", "NeedsLogin", ""):
                await self.api.request("POST", "start", {
                    "UpdatePrefs": start_prefs(self.hostname,
                                               self.login_server)})
            else:
                prefs = safe_prefs(self.hostname, self.login_server)
                prefs.update({"WantRunning": True, "WantRunningSet": True})
                await self.api.request("PATCH", "prefs", prefs)
        except (OSError, LocalAPIError, asyncio.TimeoutError) as e:
            self.last_error = "settings: %s" % e
            logging.info("tailscale: %s", self.last_error)
        await self._refresh()
        await self._maybe_login()

    async def _maybe_login(self, force: bool = False) -> None:
        st = self.ts_status
        if st.get("BackendState") != "NeedsLogin":
            return
        if st.get("AuthURL") and not force:
            return
        if self.login_requested and not force:
            return
        self.login_requested = True
        try:
            await self.api.request("POST", "login-interactive")
        except (OSError, LocalAPIError, asyncio.TimeoutError) as e:
            self.last_error = "login: %s" % e
            logging.info("tailscale: %s", self.last_error)

    async def _on_notify(self, msg: Dict[str, Any]) -> None:
        url = msg.get("BrowseToURL")
        if isinstance(url, str) and url:
            self._set_login_url(url)
        if msg.get("ErrMessage"):
            self.last_error = redact(str(msg["ErrMessage"]))[:300]
        # Go sends fields without news as null
        if msg.get("State") is not None or \
                msg.get("LoginFinished") is not None or url:
            await self._refresh()
            if self.ts_status.get("BackendState") == "NeedsLogin":
                await self._maybe_login()

    async def _refresh(self) -> None:
        try:
            st = await self.api.request("GET", "status?peers=false")
        except (OSError, LocalAPIError, asyncio.TimeoutError) as e:
            logging.info("tailscale: status: %s", e)
            return
        if not isinstance(st, dict):
            return
        self.ts_status = st
        state = st.get("BackendState")
        if state == "NeedsLogin" and st.get("AuthURL"):
            self._set_login_url(st["AuthURL"])
        elif state != "NeedsLogin":
            self._set_login_url("")
        self._publish()

    def _set_login_url(self, url: str) -> None:
        if url == self.login_url:
            return
        self.login_url = url
        if url:
            self.server.add_warning(
                "kiln-tailscale: to add this printer to your tailnet, open "
                "%s and log in to Tailscale. The link is shown only to you "
                "(on this printer's network); don't share it." % url,
                warn_id="tailscale_login", log=False)
            self._console("to add this printer to your tailnet, open: " + url)
            logging.info("tailscale: login link waiting (see Fluidd's "
                         "notifications or /server/tailscale/status)")
        else:
            self.server.remove_warning("tailscale_login")

    # Status
    def _alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _state(self) -> str:
        blocked = ""
        if not self._alive():
            if self.ports_error:
                blocked = "config_error"
            elif not self.guard.get("ok", True):
                blocked = "memory_guard"
            elif self._has_marker(OOM_MARKER):
                blocked = "oom_off"
            elif self._has_marker(LOGOUT_MARKER):
                blocked = "logged_out"
        if blocked:
            return blocked
        if not self._alive():
            return "error" if self.last_error else "stopped"
        backend = self.ts_status.get("BackendState", "")
        return {
            "": "starting", "NoState": "starting", "Starting": "starting",
            "NeedsLogin": "needs_login",
            "NeedsMachineAuth": "needs_approval",
            "Stopped": "stopped", "Running": "running",
        }.get(backend, backend.lower())

    def get_status(self) -> Dict[str, Any]:
        st = self.ts_status
        ips = [str(ip) for ip in st.get("TailscaleIPs") or []]
        ipv4 = [ip for ip in ips if ":" not in ip]
        self_st = st.get("Self") or {}
        tailnet = st.get("CurrentTailnet") or {}
        running = self._alive()
        return {
            "state": self._state(),
            "backend_state": st.get("BackendState"),
            "login_url": self.login_url or None,
            "tailnet_ip": ipv4[0] if ipv4 else (ips[0] if ips else None),
            "tailnet_ips": ips,
            # The name asked for (Self.HostName is the OS name until the
            # first netmap); dns_name is the name in the tailnet
            "hostname": self.hostname,
            "dns_name": (self_st.get("DNSName") or "").rstrip(".") or None,
            "tailnet": tailnet.get("Name"),
            "version": st.get("Version"),
            "health": st.get("Health") or [],
            "memory_guard": dict(self.guard),
            # None: every local port is reachable from the tailnet
            "ports": list(self.ports) if self.ports else None,
            "daemon": {
                "running": running,
                "pid": self.proc.pid if running and self.proc else None,
                "uptime": (round(time.monotonic() - self.started_at, 1)
                           if running else 0),
                "restarts": self.restarts,
                "last_exit": self.last_exit,
                "oom_off": self._has_marker(OOM_MARKER),
            },
            "error": self.last_error,
            "log": list(self.log_lines)[-15:],
        }

    def _publish(self) -> None:
        status = self.get_status()
        key = dict(status, daemon=None, log=None)
        if key == self.last_sent:
            return
        self.last_sent = key
        self.server.send_event("tailscale:status_changed", status)

    def _summary(self) -> str:
        s = self.get_status()
        state = s["state"]
        if state == "running":
            text = "connected as %s (%s)" % (
                s["dns_name"] or s["hostname"], s["tailnet_ip"] or "no IP")
            text += ". Fluidd: http://%s/" % (s["dns_name"] or s["tailnet_ip"])
        elif state == "needs_login":
            text = ("waiting for login: open %s" % s["login_url"]
                    if s["login_url"] else "waiting for the login link")
        elif state == "needs_approval":
            text = ("logged in; approve the device in your Tailscale "
                    "admin console")
        elif state == "config_error":
            text = "not started: " + self.ports_error
        elif state == "memory_guard":
            text = "not started: " + s["memory_guard"].get("reason", "")
        elif state == "oom_off":
            text = ("off after running out of memory; TAILSCALE_LOGIN "
                    "starts it again")
        elif state == "logged_out":
            text = "logged out; TAILSCALE_LOGIN logs in again"
        else:
            text = state
        if state == "running":
            text += (". Reachable ports: " +
                     ", ".join(str(p) for p in s["ports"])
                     if s["ports"] else ". Every port is reachable")
        if s["memory_guard"].get("override"):
            text += " (without Extended memory: allow_low_memory)"
        if s["error"] and state not in ("running",):
            text += "; last error: " + s["error"]
        return text

    async def _handle_status(self, web_request: WebRequest) -> Dict[str, Any]:
        return self.get_status()

    # Login (also: start again after a logout or the OOM switch-off)
    async def login(self) -> Dict[str, Any]:
        self._clear_marker(OOM_MARKER)
        self._clear_marker(LOGOUT_MARKER)
        self.server.remove_warning("tailscale_oom")
        if self._alive():
            await self._refresh()
            if self.ts_status.get("BackendState") == "NeedsLogin":
                await self._maybe_login(force=True)
            else:
                await self._bring_up()
        else:
            self.wake.set()
        return self.get_status()

    async def logout(self) -> Dict[str, Any]:
        self._set_marker(LOGOUT_MARKER, time.strftime("%Y-%m-%d %H:%M:%S"))
        error = ""
        if self._alive():
            try:
                await self.api.request("POST", "logout", timeout=30.)
            except (OSError, LocalAPIError, asyncio.TimeoutError) as e:
                error = str(e)
            await self._stop_proc()
        self._set_login_url("")
        self.ts_status = {}
        if error:
            self.last_error = "logout: " + error
        self._publish()
        return self.get_status()

    async def _handle_login(self, web_request: WebRequest) -> Dict[str, Any]:
        return await self.login()

    async def _handle_logout(self, web_request: WebRequest) -> Dict[str, Any]:
        return await self.logout()

    async def _remote_status(self) -> None:
        self._console(self._summary())

    async def _remote_login(self) -> None:
        if self.ports_error:
            self._console("not started: " + self.ports_error)
            return
        if not self.guard.get("ok", True):
            self._console("not started: " + guard_message(self.guard))
            return
        await self.login()
        await asyncio.sleep(0)
        self._console("starting; the login link (if one is needed) follows "
                      "here and in Fluidd's notifications")

    async def _remote_logout(self) -> None:
        await self.logout()
        self._console("logged out and stopped. To remove the printer from "
                      "your tailnet, delete it in the Tailscale admin console"
                      " (Machines). TAILSCALE_LOGIN logs in again.")

    async def _stop_proc(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_WAIT)
        except asyncio.TimeoutError:
            logging.info("tailscale: no exit after SIGTERM, killing")
            proc.kill()
            await proc.wait()

    async def close(self) -> None:
        self.closing = True
        self.wake.set()
        await self._stop_proc()
        if self.control_task is not None:
            self.control_task.cancel()
        if self.task is not None:
            self.task.cancel()


def load_component(config: ConfigHelper) -> KilnTailscale:
    return KilnTailscale(config)
