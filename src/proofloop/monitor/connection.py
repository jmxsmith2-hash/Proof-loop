"""Test a printer's connection the way a send uses it, without printing anything.

Three steps, each with what to do when it fails:

1. **MQTT login** (TLS, user ``bblp`` and the LAN access code): the access code is right.
2. **Status reports** on ``device/<serial>/report``: the serial number is right, and the printer
   accepts commands (it reports "MQTT command verification failed" when Developer Mode is off).
3. **FTPS login** (TLS from the first byte): files can be copied to it. Nothing is uploaded.
"""

from __future__ import annotations

import ftplib
import json
import secrets
import ssl
import threading
from typing import Any

from proofloop.monitor.printer.state import PrinterState


def _context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # printers present a self-signed certificate
    return ctx


def _step(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"step": name, "ok": ok, "detail": detail}


def _mqtt(host: str, serial: str, code: str, port: int, timeout: float) -> list[dict[str, Any]]:
    import paho.mqtt.client as mqtt

    connected, reported = threading.Event(), threading.Event()
    result: dict[str, Any] = {}
    state = PrinterState()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"proofloop-test-{secrets.token_hex(3)}")
    client.username_pw_set("bblp", code)
    client.tls_set_context(_context())

    def on_connect(c, userdata, flags, reason_code, properties=None):
        result["rc"] = reason_code
        connected.set()
        if reason_code == 0:
            c.subscribe(f"device/{serial}/report")
            c.publish(f"device/{serial}/request", json.dumps({"pushing": {"command": "pushall"}}))

    def on_message(c, userdata, msg):
        try:
            state.merge(json.loads(msg.payload))
        except ValueError:
            return
        reported.set()

    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(host, port, keepalive=15)
    except OSError as e:
        return [_step("mqtt", False, f"Can't reach {host}: {e}. Is the printer on and on this network, with "
                                     "LAN-only mode on? Check the IP address on its screen.")]
    client.loop_start()
    try:
        if not connected.wait(timeout):
            return [_step("mqtt", False, f"{host} didn't answer the login. Turn on LAN-only mode on the printer.")]
        rc = result.get("rc")
        if rc != 0:
            return [_step("mqtt", False, f"The printer refused the login ({rc}): check the access code on its "
                                         "screen (Settings › LAN-only mode).")]
        out = [_step("mqtt", True, "Logged in with the access code.")]
        if not reported.wait(timeout):
            out.append(_step("reports", False, f"No status reports for serial {serial}: check the serial "
                                              "number (on the printer's screen, or under Find printers)."))
        elif state.commands_rejected:
            out.append(_step("reports", False, "The printer rejects commands: turn on Developer Mode "
                                              "(Settings › LAN-only mode › Developer Mode)."))
        else:
            now = (state.gcode_state or "idle").lower()
            out.append(_step("reports", True, f"Status reports arrive (it is {now})."))
        return out
    finally:
        client.loop_stop()
        client.disconnect()


def _ftps(host: str, code: str, port: int, timeout: float) -> dict[str, Any]:
    from proofloop.monitor.send import _ImplicitFTPS

    ftp = _ImplicitFTPS(context=_context(), timeout=timeout)
    try:
        ftp.connect(host, port, timeout)
        ftp.login("bblp", code)
        return _step("ftps", True, "Files can be copied to it (nothing was copied).")
    except ftplib.error_perm as e:
        return _step("ftps", False, f"The printer refused the file login ({str(e)[:3]}): check the access code.")
    except (OSError, ftplib.Error) as e:
        return _step("ftps", False, f"Can't open the printer's file storage: {e}. Is an SD card or storage in it?")
    finally:
        if ftp.sock is not None:
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()


def check_connection(host: str, serial: str, access_code: str, mqtt_port: int = 8883, ftp_port: int = 990,
         timeout: float = 10.0) -> dict[str, Any]:
    """The printer's connection as a send uses it: ``ok`` when every step works, and each step."""
    steps = _mqtt(host, serial, access_code, mqtt_port, timeout)
    if steps[0]["ok"]:
        steps.append(_ftps(host, access_code, ftp_port, timeout))
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


def test_printer(p, link=None, timeout: float = 10.0) -> dict[str, Any]:
    """``p`` is a printer from settings; ``link`` its connection, for its ports."""
    mqtt_port = getattr(link, "mqtt_port", 8883)
    ftp_port = getattr(link, "ftp_port", 990)
    return {"printer": p.name, **check_connection(p.host, p.serial, p.access_code, mqtt_port, ftp_port, timeout)}
