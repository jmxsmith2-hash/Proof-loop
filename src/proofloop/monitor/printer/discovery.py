"""Find Bambu Lab printers on the local network.

Printers announce themselves with SSDP on UDP port 2021 (not the usual 1900),
multicast 239.255.255.250, search target ``urn:bambulab-com:device:3dprinter:1``.
The announcement carries the serial number (``USN``), the name you gave the
printer (``DevName.bambu.com``) and its model id (``DevModel.bambu.com``, the
same codes Bambu Studio writes as ``printer_model_id``). This matches
Bambuddy's discovery service. The access code is never broadcast; you still
read it from the printer's screen.
"""

from __future__ import annotations

import re
import socket
import struct
import time

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 2021
SEARCH_TARGET = "urn:bambulab-com:device:3dprinter:1"
MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 3\r\n"
    f"ST: {SEARCH_TARGET}\r\n"
    "\r\n"
)


class DiscoveryError(RuntimeError):
    pass


def parse(message: str, ip: str) -> dict | None:
    if SEARCH_TARGET not in message and "bambulab" not in message.lower():
        return None
    usn = re.search(r"USN:\s*(?:uuid:)?([^\s\r\n]+)", message, re.I)
    if not usn:
        return None
    name = re.search(r"DevName\.bambu\.com:\s*(.+?)(?:\r\n|\n|$)", message, re.I)
    model = re.search(r"DevModel\.bambu\.com:\s*(.+?)(?:\r\n|\n|$)", message, re.I)
    serial = usn.group(1).strip()
    return {
        "serial": serial,
        "name": name.group(1).strip() if name else serial,
        "model_id": model.group(1).strip() if model else "",
        "host": ip,
    }


def discover(seconds: float = 6.0) -> list[dict]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        try:
            sock.bind(("", SSDP_PORT))
        except OSError as e:
            raise DiscoveryError(
                f"can't listen on UDP port {SSDP_PORT} ({e.strerror}). Another program (often Bambu Studio) "
                "is using it; close it and try again, or type the printer's IP address."
            ) from e
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass  # still receives unicast replies to the search
        sock.settimeout(0.3)
        found: dict[str, dict] = {}
        end = time.monotonic() + seconds
        last_search = 0.0
        while time.monotonic() < end:
            if time.monotonic() - last_search > 2.5:
                try:
                    sock.sendto(MSEARCH.encode(), (SSDP_ADDR, SSDP_PORT))
                except OSError:
                    pass
                last_search = time.monotonic()
            try:
                data, addr = sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            p = parse(data.decode("utf-8", errors="ignore"), addr[0])
            if p and p["serial"] not in found:
                found[p["serial"]] = p
        return sorted(found.values(), key=lambda p: p["name"])
    finally:
        sock.close()
