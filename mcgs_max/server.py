"""mcgs-max MCP server (stdio transport).

The MCP client spawns this process and speaks newline-delimited JSON-RPC over
stdin/stdout. Nothing needs to be started by hand and no port is used.

    python -m mcgs_max.server          # when cwd is the project root
    python D:\\mcgs-max\\run.py         # from anywhere

stdout carries protocol messages only; every log line goes to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import compat
from .tools import TOOLS

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mcgs-max"
SERVER_VERSION = "0.1.0"


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _tools_list():
    return {
        "tools": [
            {"name": name,
             "description": spec["description"],
             "inputSchema": spec["input_schema"]}
            for name, spec in TOOLS.items()
        ]
    }


def _tools_call(params):
    """Return (result, error_message)."""
    name = params.get("name")
    arguments = params.get("arguments") or {}
    spec = TOOLS.get(name)
    if spec is None:
        return None, f"unknown tool: {name}"
    try:
        payload = spec["fn"](**arguments)
    except TypeError as exc:
        return None, f"invalid arguments for {name}: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return {
        "content": [{"type": "text",
                     "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": False,
    }, None


def dispatch(message):
    """Handle one JSON-RPC message. Returns the response dict or None."""
    if not isinstance(message, dict):
        return None

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method is None:
        return None                       # a response, not a request
    if method.startswith("notifications/"):
        return None                       # notifications get no reply

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, _tools_list())

    if method == "tools/call":
        result, error = _tools_call(params)
        if error:
            return _error(request_id, -32603, error)
        return _result(request_id, result)

    return _error(request_id, -32601, f"method not found: {method}")


def _write(payload):
    # ensure_ascii keeps the wire bytes pure ASCII: JSON-RPC over stdio is UTF-8
    # by spec, but escaping sidesteps any client that assumes the local code page.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _log(text):
    print(text, file=sys.stderr, flush=True)


def _force_utf8():
    """Pin the stdio streams to UTF-8.

    On Windows the default text encoding is the ANSI code page (GBK on a Chinese
    system), so Chinese window names written to stdout arrive at the client as
    mojibake, and Chinese arguments read from stdin are decoded wrong. JSON-RPC
    over stdio is UTF-8 by definition, so say so explicitly.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    # keep newline framing LF-only; CRLF would leave a stray \r on each message
    try:
        sys.stdout.reconfigure(newline="\n")
    except (AttributeError, ValueError):
        pass


def serve():
    _force_utf8()
    _log(f"{SERVER_NAME} {SERVER_VERSION} (stdio)")
    _log(f"  tools : {', '.join(TOOLS)}")
    env = compat.check_environment()
    _log(f"  python: {env['python']}")
    if env["missing"]:
        _log(f"  MISSING: {env['missing']} -> pip install --target {env['vendor_dir']} "
             f"access-parser")

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(_error(None, -32700, f"parse error: {exc}"))
            continue

        try:
            if isinstance(message, list):
                for item in message:
                    response = dispatch(item)
                    if response is not None:
                        _write(response)
            else:
                response = dispatch(message)
                if response is not None:
                    _write(response)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _write(_error(message.get("id") if isinstance(message, dict) else None,
                          -32603, f"internal error: {exc}"))


def main():
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
