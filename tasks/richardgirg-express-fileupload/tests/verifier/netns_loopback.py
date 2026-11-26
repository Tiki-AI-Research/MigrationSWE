#!/usr/bin/env python3
"""Bring ``lo`` up inside a freshly created network namespace.

``unshare --net`` creates a namespace whose only interface (``lo``) starts
*down*. Every HTTP-based test in this task (supertest, ``app.listen(0)``,
``http.request`` against ``127.0.0.1``) fails with ``EADDRNOTAVAIL`` while it is
down, which would masquerade as a candidate regression.

The reference implementation calls ``ip link set lo up``. The frozen
environment image does not ship iproute2, and adding an apt dependency to the
verifier image would make the build depend on network access, so the same
``SIOCSIFFLAGS`` ioctl is issued directly. No output on success; a failure is
printed to stderr so the caller can classify it as an infrastructure error.
"""

from __future__ import annotations

import fcntl
import socket
import struct
import sys

SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
IFF_UP = 0x1


def main() -> int:
    request = struct.pack("16sH", b"lo", 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        flags = struct.unpack("16sH", fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, request))[1]
        if not flags & IFF_UP:
            fcntl.ioctl(sock.fileno(), SIOCSIFFLAGS, struct.pack("16sH", b"lo", flags | IFF_UP))
        if not struct.unpack("16sH", fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, request))[1] & IFF_UP:
            raise OSError("loopback did not come up")
    except OSError as err:
        print(f"loopback-up failed: {err}", file=sys.stderr)
        return 1
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
