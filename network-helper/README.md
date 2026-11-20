# Brioche embedded network helper

Pinned engine: `tailscale.com v1.104.7`; toolchain: Go 1.27.4 or later.

This executable is distributed with Brioche, launched by its GUI, and communicates
only through private stdin/stdout JSON lines. It does not install a system VPN,
modify routes/DNS, or expose a local management HTTP port. tsnet state is private
user application data. Existing system Tailscale profiles are independent.

Ports **inside the tailnet**:

- TCP 47994: `/info` returns the active share's certificate pin and temporary token
  only to an authenticated, untagged node owned by the same Tailscale user.
- TCP 47995: same-owner check, then opaque byte proxy to the explicitly enabled
  Brioche TLS host on `127.0.0.1:<ephemeral port>`.

Both ports remain subject to tailnet access policies. No Funnel, exit node,
subnet routes, tagged unattended credentials or shared auth key is enabled.
Device discovery lists online `brioche-` hostnames owned by the same user; hostname
is only a display/discovery filter, never authorization.

The network helper cannot grant desktop control by itself: the local user must
start an external share and choose view/control permission. The native protocol
still verifies the exact certificate pin before sending its temporary token.
Stopping sharing removes discovery credentials and closes inbound proxies;
logout closes all network streams. Closing the parent stdin shuts down the node.

Build on each target OS with `scripts/build_network.py`; users don't install Go.
Build tools and caches are not distributed. Dependency licenses and linked module
metadata are collected beside the generated executable.

**macOS 12:** this latest SDK requires Go 1.27, whose minimum macOS version is 13.
Do not label this helper macOS 12 compatible or try to fix it just by changing a
minimum deployment variable. A separate compatibility version/backend and a
security review plus real-machine tests are still required. Windows 11 and
macOS 13+ helper builds also need target-machine validation.

Tests use a local test coordination server and local DERP/STUN with two real tsnet
nodes. They validate same-owner transfer and different-owner rejection, without
logging into the user's real account. This does not establish real Internet NAT
traversal performance. The separate official-login smoke test checks obtaining a
valid login URL; the user must finish authorization in their browser.
