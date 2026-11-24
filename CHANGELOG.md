# Changelog

## kiln-tailscale 0.1.0 (unreleased)

- The Kiln OS plugin: a Moonraker component starts Tailscale's tailscaled as user `klipper` in
  userspace networking mode (nice 19, first in line if memory runs out, parent death signal),
  restarts it with a back-off (5 s to 10 min) and stops it with Moonraker.
- Memory guard: starts only with Extended memory; `allow_low_memory: True` runs it anyway, with a
  warning in Fluidd. If the system stops tailscaled for lack of memory, Tailscale stays off (also
  after restarts) until `TAILSCALE_LOGIN`.
- Safe settings at each start through tailscaled's LocalAPI: no exit node, no routes, no Tailscale
  SSH, no web client, shields down (so Fluidd is reachable), the name `kiln-<printer name>`.
- The login link in Fluidd's notifications and console, kept out of the logs; `TAILSCALE_STATUS`,
  `TAILSCALE_LOGIN`, `TAILSCALE_LOGOUT`; `GET /server/tailscale/status`,
  `POST /server/tailscale/login` and `/logout`; `notify_tailscale_status_changed`.
- `ports:` (`ports: 80, 7912`): only these local ports are reachable from the tailnet; default every
  port. An invalid list keeps Tailscale off with a warning. Shown in the status (`ports`). Implemented
  as a patch to Tailscale (`patches/0001`, `TS_KILN_ALLOWED_PORTS`: TCP to other ports gets a RST
  without connecting to 127.0.0.1, UDP is dropped), with Go tests run by the build script.
- `bin/tailscaled`: Tailscale 1.104.7 with Kiln's patch, built from source by `tools/build_tailscale.sh` (Go 1.27.4,
  MIPS32 soft float, NaN2008 flag), without SSH, Taildrive, Taildrop, Serve/Funnel, exit nodes,
  routes, port mapping, log upload and the other features the plugin doesn't use.
