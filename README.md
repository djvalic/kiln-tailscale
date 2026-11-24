# kiln-tailscale

Fluidd, the camera and Moonraker from anywhere, through your own
[Tailscale](https://tailscale.com) network (your *tailnet*). A Kiln OS
plugin that runs Tailscale's `tailscaled` on the printer. Your phone or
laptop, logged in to the same tailnet, then opens the printer as if it
were at home, without opening any port on your router.

**Status: in development, not in the Kiln OS catalog yet.**

Tailscale is a trademark of Tailscale Inc. This plugin is not made or
endorsed by Tailscale; it runs their open-source client, built from their
source code.

## Needs Extended memory

`tailscaled` needs a good share of the printer's memory. Measured on an
AD5X (512 MiB, Extended memory, Kiln OS v1.0.1-rc1): about 21 MB while
connected and idle, up to 27 MB while Fluidd and the camera were open
remotely over mobile data.
Most AD5X printers have about 110-130 MB free while printing, and the
printer's own programs need that room. So the plugin starts Tailscale
only when the printer runs with **Extended memory** (printers with 512 MiB:
`KILN_MEMORY_MODE ENABLE=1` in the console, then restart the printer;
about 360 MB free). Without it, Fluidd shows a notice saying why Tailscale
didn't start.

You can run it without Extended memory anyway, at your own risk:

```
[tailscale]
allow_low_memory: True
```

in your `moonraker.conf`. Fluidd then always shows a warning. Be aware of
what this risks: when memory runs short, the printer gets slow, and the
system may have to stop programs to free memory. tailscaled is always the
first one it stops (it is set up that way), but a print can still stutter
or, in a bad case, fail. If the system ever has to stop tailscaled for
lack of memory, the plugin switches Tailscale off, also after restarts,
and Fluidd says so; `TAILSCALE_LOGIN` starts it again.

## Install

Until it is in the Kiln OS catalog, clone it once over SSH, as user
`klipper` so Fluidd's Software Updates can keep it up to date:

```
cd /data/ad5x/plugins
su klipper -s /bin/sh -c 'git clone https://github.com/djvalic/kiln-tailscale.git tailscale'
```

Then switch it on: Settings > Plugins on the touch screen, or
`ENABLE_PLUGIN NAME=tailscale` in Fluidd's console. Moonraker restarts (a
few seconds) and starts Tailscale.

## First login

You need a Tailscale account (free for personal use). The first time,
the printer asks to be added to your tailnet:

1. Fluidd shows a notification with a login link
   (`https://login.tailscale.com/a/...`), and the console prints it too.
   `TAILSCALE_STATUS` in the console shows it again.
2. Open the link and log in with your Tailscale account. That adds the
   printer to your tailnet, as `kiln-<printer name>`.
3. `TAILSCALE_STATUS` now says "connected as ...", with the
   printer's tailnet address (100.x.y.z) and name.

Keep the link to yourself: whoever opens it first adds the printer to
*their* tailnet. It is shown only in Fluidd (on your own network) and is
left out of the logs. A link that has expired: `TAILSCALE_LOGIN`
makes a new one.

If your tailnet requires device approval, approve the printer in the
admin console (Machines) afterwards.

## Using it

On a device in your tailnet, open `http://kiln-<printer name>/` (with
MagicDNS on, Tailscale's default) or `http://100.x.y.z/`. You get Fluidd
with the camera, as at home. Slicers that upload to the printer can use
the same address (OrcaSlicer: host `100.x.y.z`, port 80, or 7125 for
Moonraker directly).

CPU: idle, Tailscale uses next to nothing. Everything you look at remotely
is encrypted on the printer, and the camera stream is the heavy part:
watching it remotely used up to about 70% of one of the printer's two
cores in testing. Tailscale runs at the lowest priority, so Klipper still
comes first; during a long print, check in on the camera now and then
rather than leaving it streaming.

### What your tailnet can reach

By default, **everything** on the printer. Tailscale here runs without a
network interface of its own ("userspace networking": Kiln's kernel has
no TUN), so a connection that comes in over Tailscale to the printer's
tailnet address is passed on to the same port on the printer itself
(127.0.0.1). Devices in your tailnet reach every port the printer listens
on, also those normally open only to the printer itself: Fluidd and the
API (80), Mainsail if you run it (81), Moonraker (7125), the
spool inventory of kiln-microspool (7912), the cameras (8090, 8091), the
screen stream (8092), and other plugins' ports. That is on purpose, so
plugins' pages work over Tailscale as at home. To those programs the
connection looks local, so Moonraker treats it like a device on your home
network (Kiln OS has no login for Fluidd). SSH (22) is reachable too if
it runs on the printer; it still needs the root password.

Anyone your tailnet lets reach the printer can control it, as anyone on
your home network can. Don't share the printer with other people's
tailnets unless you would hand them the printer.

To limit what is reachable, list the ports in your `moonraker.conf`:

```
[tailscale]
ports: 80, 7912
```

Only those ports are passed on; a connection to another port is refused
(TCP) or dropped (UDP) by Tailscale itself, before anything on the
printer sees it. Fluidd needs only 80 (its camera and Moonraker go
through 80); add 7125 for a slicer that talks to Moonraker directly.
Ports are separated by commas or spaces, 1 to 65535. A list with anything
else in it is not guessed at: Tailscale doesn't start and Fluidd says
which entry is wrong. A changed list applies when Moonraker restarts
(Fluidd's restart button for Moonraker, or a reboot), which restarts
Tailscale with it. `TAILSCALE_STATUS` and
`/server/tailscale/status` (`ports`, `null` for every port) show the
list in use. This is a small patch of ours to Tailscale
(`patches/0001-...patch`, `patches/README.md`).

The other way, or both: your tailnet's access rules in Tailscale's admin
console (Access controls) decide which of your devices may reach which
ports, for example with a tag:

```
"grants": [
  {"src": ["autogroup:member"], "dst": ["tag:printer"], "ip": ["tcp:80", "tcp:7912"]}
]
```

(give the printer the tag `tag:printer` in the admin console). With the
default rules, every device of yours reaches every port.

## Privacy and what is left out

- **No Tailscale SSH**: no remote shell through Tailscale (it isn't even
  in the program).
- **No exit node**: the printer neither sends its traffic through another
  device nor offers itself as one. No subnet routes either way. Only
  connections to the printer itself use Tailscale; the printer's own
  internet traffic goes out as before.
- **No ports opened on your router**: no UPnP, NAT-PMP or PCP. Devices
  connect directly when their networks allow it (STUN and hole punching),
  otherwise through Tailscale's relays (DERP), encrypted end to end
  either way.
- **No logs sent to Tailscale** (`--no-logs-no-support`, and the log
  upload isn't in the program). Tailscale's coordination server still
  knows the device (its name, addresses, OS and version), as for any
  device in a tailnet.
- Left out of the program as well: Taildrop, Taildrive, Serve and Funnel,
  the web client, app connectors, the relay server, auto-update.

## Commands

From Fluidd's console, HelixScreen's macros or any macro:

| Command | |
|---|---|
| `TAILSCALE_STATUS` | Connected or not, the tailnet name and address, the login link |
| `TAILSCALE_LOGIN` | A new login link; also starts Tailscale again after a logout or the out-of-memory switch-off |
| `TAILSCALE_LOGOUT` | Logs the printer out of the tailnet and stops Tailscale |

Moonraker API: `GET /server/tailscale/status` (state, login link,
tailnet address and name, the memory guard, tailscaled's last lines),
`POST /server/tailscale/login`, `POST /server/tailscale/logout`.
Websocket clients get `notify_tailscale_status_changed`.

## Settings

In your `moonraker.conf`, a `[tailscale]` section of your own (the
plugin's section is empty, so yours count):

| Option | Default | |
|---|---|---|
| `allow_low_memory` | `False` | `True` runs Tailscale without Extended memory ("Needs Extended memory" above) |
| `hostname` | `kiln-<printer name>` | The printer's name in your tailnet |
| `ports` | every port | Only these local ports reachable from the tailnet, `80, 7912` ("What your tailnet can reach") |
| `login_server` | Tailscale's | Another coordination server, such as your own Headscale (`https://headscale.example.com`) |
| `memory_limit` | `48MiB` | tailscaled's memory target (Go's `GOMEMLIMIT`; a soft limit) |
| `state_dir` | `/home/klipper/printer_data/tailscale` | The printer's Tailscale identity (keys) |
| `socket` | `/run/moonraker/kiln-tailscale/tailscaled.sock` | tailscaled's local control socket |
| `binary` | the plugin's `bin/tailscaled` | |

## Removing it from your tailnet

1. `TAILSCALE_LOGOUT` in the console: the printer logs out and
   Tailscale stops (it stays off until `TAILSCALE_LOGIN`).
2. In Tailscale's admin console (Machines), remove the printer if it is
   still listed.
3. Switch the plugin off (`DISABLE_PLUGIN NAME=tailscale`, or Settings >
   Plugins), or remove it (`REMOVE_PLUGIN NAME=tailscale`, or delete
   `/data/ad5x/plugins/tailscale`).
4. To leave nothing behind, delete `/home/klipper/printer_data/tailscale`
   (the printer's Tailscale keys).

## How it runs

The plugin's Moonraker component (`moonraker/components/tailscale.py`)
starts `bin/tailscaled` as user `klipper` (no root part) with
`--tun=userspace-networking`, its state in `printer_data/tailscale`
(readable only by `klipper`, not reachable from Fluidd's file manager) and
its control socket in `/run/moonraker`. It runs at the lowest CPU priority
(nice 19) and is the first program the system stops if memory runs out
(`oom_score_adj` 1000). If it stops, it is started again after 5 s,
doubling up to 10 min; it stops with Moonraker (also when Moonraker is
killed). The component talks to tailscaled's LocalAPI over the socket: it
sets the safe settings above at each start, asks for the login link when
one is needed and watches for changes.

## The tailscaled binary

`bin/tailscaled` is built from Tailscale's source by
`tools/build_tailscale.sh`: Tailscale's release tag (checked against its
commit), the Go version its `go.mod` names (downloaded from go.dev, its
sha256 checked against the pinned one and the one go.dev publishes),
with Kiln's patches in `patches/` (one: the `ports` option) and their
tests, built for the printer's processor (MIPS32 little-endian, software
floating point) with the features above left out, and Tailscale's own
version stamp. The printer's kernel only runs MIPS programs marked
"NaN2008", so the script sets that flag in the file header afterwards
(Go's software floating point has no FPU instructions, so this changes
nothing in how it runs). `bin/SOURCES` lists the versions, options and the
sha256; the same script gives the same file again.

Each new `bin/tailscaled` adds about 6 MB to the plugin's git history on
the printer.

## Testing on a printer

With Extended memory on and the plugin enabled:

1. `TAILSCALE_STATUS`: waiting for login, with the link; open it.
2. `TAILSCALE_STATUS`: connected; open `http://<tailnet name>/` from a
   phone with mobile data (not your Wi-Fi): Fluidd, camera, a file upload.
3. Memory and CPU: `grep -E 'VmRSS|VmHWM' /proc/$(pidof tailscaled)/status`
   and `top -b -n 1 | grep tailscaled`, idle and while Fluidd shows the
   camera over Tailscale.
4. `TAILSCALE_LOGOUT`, then remove it in the admin console.

## Licence and credits

- tailscaled (`bin/tailscaled`): BSD-3-Clause, Tailscale Inc and
  contributors (`LICENSE-tailscale`); its source is
  https://github.com/tailscale/tailscale at the tag in `bin/SOURCES`,
  with the patches in `patches/` (BSD-3-Clause, like Tailscale).
- This plugin (the Moonraker component, `plugin.cfg`, the build script,
  the tests): GPLv3 (`LICENSE`), like Moonraker.
