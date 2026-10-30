"""``proofloop mcp``: the tools an assistant gets, over the real protocol, and that none of them can start a print."""

import asyncio
import io
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from proofloop.app.loop_server import create_app
from proofloop.app.mcp import TOOLS, Server, config_entry
from tests.monitor.test_queue import farm

REPO = Path(__file__).resolve().parents[2]
PLATE = REPO / "examples" / "gcode" / "bracket_plate.gcode.3mf"


def rpc(server: Server, *messages: dict) -> list[dict]:
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(server: Server, name: str, **arguments) -> dict:
    (reply,) = rpc(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    return reply["result"]


def test_the_handshake_and_the_tools():
    replies = rpc(Server(), {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 3, "method": "nonsense"})
    assert [r["id"] for r in replies] == [1, 2, 3]  # a notification gets no answer
    init = replies[0]["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["serverInfo"]["name"] == "proofloop"
    assert "Nothing here starts a print" in init["instructions"]
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"check_part", "check_gcode", "make_loop_file", "plan_run", "farm_status", "summary"} <= names
    assert not any(word in n for n in names for word in ("send", "start", "resume", "pause"))
    assert replies[2]["error"]["code"] == -32601
    assert config_entry("/py")["mcpServers"]["proofloop"] == {"command": "/py", "args": ["-m", "proofloop", "mcp"]}


def test_checking_a_part_returns_findings_fixes_and_renders(tmp_path):
    cad = REPO / "examples" / "cad"
    r = call(Server(), "check_part", path=str(cad / "phone_stand_v1.stl"), design=str(cad / "phone_stand.yaml"))
    assert not r.get("isError")
    data = r["structuredContent"]
    assert data["kind"] == "design" and not data["ready"] and all(f["id"] and f["fix"] for f in data["findings"])
    images = [c for c in r["content"] if c["type"] == "image"]
    assert 1 <= len(images) <= 4 and images[0]["mimeType"] == "image/png"
    assert json.loads(r["content"][0]["text"]) == data


def test_gcode_loop_files_and_costs(tmp_path):
    s = Server()
    assert call(s, "check_gcode", path=str(REPO / "examples" / "gcode" / "bracket_good.gcode.3mf"))[
        "structuredContent"]["ready"]
    out = tmp_path / "loop.gcode.3mf"
    made = call(s, "make_loop_file", path=str(PLATE), loops=4, output=str(out))["structuredContent"]
    assert made["written"] == str(out.resolve()) and out.is_file()
    assert call(s, "check_loop_file", path=str(out))["structuredContent"]["ready"]
    assert call(s, "cost_plate", path=str(out))["structuredContent"]["kind"] == "cost"


def test_mistakes_come_back_as_tool_errors():
    s = Server()
    assert "absolute path" in call(s, "check_part", path="part.stl")["content"][0]["text"]
    assert call(s, "check_part", path="/nowhere/part.stl")["isError"]
    assert "no tool" in call(s, "send_to_printer", path="/x")["content"][0]["text"]
    assert "wrong arguments" in call(s, "summary", days=3)["content"][0]["text"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running(tmp_path):
    """A ProofLoop running on this computer, with two printers."""
    app = farm(tmp_path, "a", "b")
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(app), port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield app, Server(port)
    srv.should_exit = True
    app.shutdown()


def test_live_state_and_planning_a_run_never_start_anything(running):
    app, s = running
    status = call(s, "farm_status")["structuredContent"]
    assert {p["printer"] for p in status["printers"]} == {"a", "b"} and status["queue"] == []
    assert call(s, "summary", hours=24)["structuredContent"]["headline"]
    r = call(s, "plan_run", path=str(PLATE), parts=20, keep=True)["structuredContent"]
    assert {row["printer"] for row in r["plan"]["rows"]} == {"a", "b"} and "Nothing starts" in r["note"]
    assert {j["status"] for j in app.queue.runs()[0]["jobs"]} == {"waiting"}
    assert not any(w.link.sent for w in app.watches.values())


def test_without_proofloop_running_it_says_how_to_start_it():
    r = call(Server(free_port()), "farm_status")
    assert r["isError"] and "isn't running" in r["content"][0]["text"]


def test_the_official_mcp_client_can_use_it():
    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "proofloop", "mcp"],
                                       env={**os.environ})
        async with stdio_client(params) as (read, write), mcp.ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == len(TOOLS)
            r = await session.call_tool("check_gcode",
                                        {"path": str(REPO / "examples" / "gcode" / "bracket_good.gcode.3mf")})
            return r

    r = asyncio.run(go())
    error = getattr(r, "is_error", None) if hasattr(r, "is_error") else r.isError  # renamed in SDK 2
    assert not error and json.loads(r.content[0].text)["ready"]
