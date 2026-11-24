# Patches to Tailscale

`tools/build_tailscale.sh` applies these to Tailscale's source (the tag in
the script), in order, runs their tests, and lists them in `bin/SOURCES`.

## 0001: an optional list of reachable ports (`TS_KILN_ALLOWED_PORTS`)

In userspace networking mode, tailscaled passes a connection from a peer
to the printer's tailnet address on to the same port on 127.0.0.1
(`wgengine/netstack/netstack.go`, `acceptTCP` for TCP, `forwardUDP` for
UDP). So peers reach every port the printer listens on, also those open
only to the printer itself.

With the environment variable `TS_KILN_ALLOWED_PORTS` set and not empty
(ports separated by commas or spaces, `80,7912`), only those ports are
passed on: TCP to another port gets a RST, without connecting to
127.0.0.1; UDP to another port is dropped. Entries that are not ports
1-65535 are skipped, so a list without one valid port lets nothing
through (a typo never opens every port). Unset or empty: Tailscale's own
behaviour. The plugin sets the variable from its `ports:` option.

Only connections to the node's own Tailscale addresses are affected.
Tailscale's own services on the node (MagicDNS at 100.100.100.100, and in
other builds SSH, Serve, the web client and PeerAPI) are handled before
this check and are not limited by it; this build has none of those but
MagicDNS' resolver address.

Files: `netstack.go` (22 lines: a field, set from the variable in
`Create`, and one check each in `acceptTCP` and `forwardUDP`),
`netstack_kiln.go` (the parser), `netstack_kiln_test.go` (the tests:
parsing; a SYN from a peer to an allowed port is forwarded to 127.0.0.1,
to another port it gets a RST without a dial; UDP to another port is
dropped; the variable is read at start). They use the same test harness
as Tailscale's `TestAcceptTCPLoopbackForwardVsRST`.
