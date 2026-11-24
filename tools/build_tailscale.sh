#!/bin/sh
# Builds bin/tailscaled for Kiln OS from Tailscale's source.
#
#   tools/build_tailscale.sh
#
# What it does:
# - downloads the Go toolchain that Tailscale's go.mod asks for from go.dev
#   (the sha256 checked against the pinned value and against the one go.dev
#   publishes in https://go.dev/dl/?mode=json&include=all);
# - clones https://github.com/tailscale/tailscale at the pinned tag and
#   checks that the tag points at the pinned commit;
# - builds ./cmd/tailscaled for the printer (MIPS32 little-endian, soft
#   float, no cgo) with Tailscale's own version stamp (as build_dist.sh
#   sets it) and with the features this plugin doesn't use left out;
# - sets the NaN2008 flag in the ELF header (Kiln's kernel refuses MIPS
#   binaries without it; Go's soft-float code has no FPU instructions, so
#   the flag changes nothing in how it runs);
# - writes bin/tailscaled and bin/SOURCES.
#
# Downloads and the Go build cache go to $KILN_TS_CACHE
# (default ~/.cache/kiln-tailscale-build), outside this repository.
# Needs: sh, curl, git, tar, sha256sum, python3. Optional:
# qemu-mipsel-static, to run the result once (--version).
set -eu

GO_VERSION=1.27.4
GO_SHA256=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
TS_REPO=https://github.com/tailscale/tailscale.git
TS_TAG=v1.104.7
TS_COMMIT=cbc74fe48ef1b1649f69a4d9a602319427e25102

# Features kept (everything else is left out with ts_omit_* tags, from
# Tailscale's own list: go run ./cmd/featuretags --min --add=...):
#   netstack            userspace networking (Kiln's kernel has no TUN)
#   nattraversal        STUN and hole punching for direct connections
#                       (with udptransport); without it all traffic goes
#                       through Tailscale's DERP relays
#   ipnbus              watch-ipn-bus: the login link as soon as it exists
#   unixsocketidentity  LocalAPI write access only for root and the user
#                       tailscaled runs as (klipper); others read-only
#   bakedroots          Let's Encrypt roots built in, for a printer whose
#                       CA bundle is old
#   health              health warnings in the status
# Left out among others: Tailscale SSH, Taildrive, Taildrop, serve and
# funnel, the web client, exit nodes (using and offering), subnet routes
# (using and offering), app connectors, peer relay server, port mapping
# (UPnP/NAT-PMP/PCP: nothing asks your router to open a port), log upload
# to log.tailscale.com, client auto-update, C2N, kube, aws, bird, tap,
# wake-on-LAN, capture, systray, D-Bus, iptables, TPM, posture checks.
TS_KEEP=netstack,nattraversal,ipnbus,unixsocketidentity,bakedroots,health

cd "$(dirname "$0")/.."
REPO_DIR=$(pwd)
CACHE=${KILN_TS_CACHE:-$HOME/.cache/kiln-tailscale-build}
mkdir -p "$CACHE"

# Go toolchain
GO_TGZ=go$GO_VERSION.linux-amd64.tar.gz
case "$(uname -m)" in
	x86_64) ;;
	*) echo "build_tailscale.sh: needs an x86_64 Linux host" >&2; exit 1 ;;
esac
curl -fsSL 'https://go.dev/dl/?mode=json&include=all' -o "$CACHE/go-dl.json"
PUBLISHED=$(python3 -I - "$CACHE/go-dl.json" "$GO_TGZ" <<'EOF'
import json, sys
for rel in json.load(open(sys.argv[1])):
    for f in rel.get("files", []):
        if f.get("filename") == sys.argv[2]:
            print(f["sha256"])
            sys.exit(0)
sys.exit("not listed on go.dev: " + sys.argv[2])
EOF
)
if [ "$PUBLISHED" != "$GO_SHA256" ]; then
	echo "build_tailscale.sh: go.dev publishes sha256 $PUBLISHED for $GO_TGZ, pinned is $GO_SHA256" >&2
	exit 1
fi
if [ ! -x "$CACHE/go$GO_VERSION/go/bin/go" ]; then
	if [ ! -f "$CACHE/$GO_TGZ" ]; then
		curl -fsSL -o "$CACHE/$GO_TGZ.part" "https://go.dev/dl/$GO_TGZ"
		mv "$CACHE/$GO_TGZ.part" "$CACHE/$GO_TGZ"
	fi
	echo "$GO_SHA256  $CACHE/$GO_TGZ" | sha256sum -c -
	rm -rf "$CACHE/go$GO_VERSION"
	mkdir -p "$CACHE/go$GO_VERSION"
	tar -C "$CACHE/go$GO_VERSION" -xzf "$CACHE/$GO_TGZ"
fi
GO=$CACHE/go$GO_VERSION/go/bin/go
export GOTOOLCHAIN=local GOPATH="$CACHE/gopath" GOCACHE="$CACHE/gocache" \
	GOFLAGS=-modcacherw GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org
"$GO" version

# Tailscale source at the pinned tag
SRC=$CACHE/tailscale-src
if [ ! -d "$SRC/.git" ]; then
	git clone --quiet "$TS_REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet --tags origin
GOT=$(git -C "$SRC" rev-parse "$TS_TAG^{commit}")
if [ "$GOT" != "$TS_COMMIT" ]; then
	echo "build_tailscale.sh: tag $TS_TAG is $GOT, pinned is $TS_COMMIT" >&2
	exit 1
fi
git -C "$SRC" checkout --quiet --force "$TS_COMMIT"
git -C "$SRC" clean -qfdx
WANT_GO=$(sed -n 's/^go \([0-9.]*\)$/\1/p' "$SRC/go.mod")
if [ "$WANT_GO" != "$GO_VERSION" ]; then
	echo "build_tailscale.sh: $TS_TAG's go.mod wants Go $WANT_GO, pinned is $GO_VERSION" >&2
	exit 1
fi

# Tags and version stamp (both with the host's GOOS/GOARCH)
cd "$SRC"
TAGS=$(GOOS= GOARCH= CGO_ENABLED=0 "$GO" run ./cmd/featuretags --min --add="$TS_KEEP")
eval "$(CGO_ENABLED=0 GOOS= GOARCH= "$GO" run ./cmd/mkversion)"
LDFLAGS="-s -w -X tailscale.com/version.longStamp=$VERSION_LONG -X tailscale.com/version.shortStamp=$VERSION_SHORT"

OUT=$CACHE/tailscaled.mipsle
rm -f "$OUT"
CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat \
	"$GO" build -tags="$TAGS" -trimpath -ldflags "$LDFLAGS" -o "$OUT" ./cmd/tailscaled

# NaN2008 flag (EF_MIPS_NAN2008, 0x400 in e_flags)
python3 -I - "$OUT" <<'EOF'
import struct, sys
path = sys.argv[1]
with open(path, "r+b") as f:
    hdr = f.read(52)
    if hdr[:4] != b"\x7fELF" or hdr[4] != 1 or hdr[5] != 1:
        sys.exit("not a 32-bit little-endian ELF: " + path)
    machine, = struct.unpack_from("<H", hdr, 18)
    if machine != 8:
        sys.exit("not a MIPS ELF (e_machine %d)" % machine)
    flags, = struct.unpack_from("<I", hdr, 36)
    f.seek(36)
    f.write(struct.pack("<I", flags | 0x400))
    print("e_flags 0x%08x -> 0x%08x" % (flags, flags | 0x400))
EOF

cd "$REPO_DIR"
mkdir -p bin
cp "$OUT" bin/tailscaled.new
chmod 755 bin/tailscaled.new
mv bin/tailscaled.new bin/tailscaled
SHA=$(sha256sum bin/tailscaled | cut -d' ' -f1)
SIZE=$(wc -c < bin/tailscaled)
{
	echo "bin/tailscaled: Tailscale $VERSION_LONG (tailscaled only, no CLI), built from source by tools/build_tailscale.sh"
	echo "licence: BSD-3-Clause, Tailscale Inc & contributors (LICENSE-tailscale)"
	echo "source: $TS_REPO tag $TS_TAG, commit $TS_COMMIT"
	echo "go: go$GO_VERSION linux-amd64 from https://go.dev/dl/$GO_TGZ, sha256 $GO_SHA256"
	echo "target: CGO_ENABLED=0 GOOS=linux GOARCH=mipsle GOMIPS=softfloat"
	echo "flags: -trimpath -ldflags \"$LDFLAGS\""
	echo "features kept: $TS_KEEP"
	echo "tags: $TAGS"
	echo "elf: EF_MIPS_NAN2008 (0x400) set in e_flags after the build"
	echo "size: $SIZE bytes"
	echo "sha256: $SHA"
} > bin/SOURCES
cat bin/SOURCES

if command -v qemu-mipsel-static >/dev/null 2>&1; then
	echo "qemu (P5600, NaN2008): $(QEMU_CPU=P5600 qemu-mipsel-static bin/tailscaled --version | head -1)"
fi
