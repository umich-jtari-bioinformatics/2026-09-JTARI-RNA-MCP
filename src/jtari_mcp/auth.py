"""Per-person API keys for the deployed (HTTP) server. Ported from DSS3.

Every HTTP request must carry `Authorization: Bearer <key>`. Keys are stored only as
SHA-256 hashes in a JSON file outside the repo (JTARI_MCP_KEYS_FILE, default
~/.jtari-mcp/keys.json), so reading that file does not reveal anyone's key. Each
request is logged with the key owner's name and the tool called. The file is
re-read when it changes, so adding or revoking a key takes effect without a restart.
stdio mode needs no key: it is launched by the user's own client on their machine.

This is a stopgap until U-M Okta login (the SDK's OAuth support).

Manage keys (on the server):
    uv run jtari-keys add <name>      # prints the new key ONCE
    uv run jtari-keys remove <name>
    uv run jtari-keys list
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
from pathlib import Path

log = logging.getLogger("jtari_mcp.auth")

DEFAULT_KEYS_FILE = Path.home() / ".jtari-mcp" / "keys.json"


def keys_file() -> Path:
    return Path(os.environ.get("JTARI_MCP_KEYS_FILE", DEFAULT_KEYS_FILE))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    """name -> sha256(key), reloaded whenever the file's modification time changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._by_hash: dict[str, str] = {}
        self._lock = threading.Lock()

    def _reload(self) -> None:
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        data = json.loads(self.path.read_text())
        self._by_hash = {h: name for name, h in data.items()}
        self._mtime = mtime
        log.info("loaded %d API key(s) from %s", len(self._by_hash), self.path)

    def owner(self, key: str) -> str | None:
        """Name of the key's owner, or None. Constant-time comparison."""
        with self._lock:
            try:
                self._reload()
            except FileNotFoundError:
                return None  # file deleted while running: refuse everyone
            h = _hash(key)
            for known, name in self._by_hash.items():
                if hmac.compare_digest(known, h):
                    return name
        return None


async def _send_json(send, status: int, payload: dict, extra_headers=()) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *extra_headers],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


class BearerKeyMiddleware:
    """ASGI middleware: reject requests without a valid key; log who called what.
    GET /health answers without a key (it reveals nothing) for container health checks."""

    def __init__(self, app, store: KeyStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan events pass straight through
            return await self.app(scope, receive, send)
        if scope.get("path") == "/health":
            return await _send_json(send, 200, {"status": "ok"})

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        name = self.store.owner(key) if key else None
        if name is None:
            log.warning(
                "refused %s %s: %s",
                scope.get("method"),
                scope.get("path"),
                "no key" if not key else "invalid key",
            )
            return await _send_json(
                send,
                401,
                {
                    "error": "unauthorized",
                    "detail": "A valid JTARI API key is required (Authorization: Bearer <key>).",
                },
                [(b"www-authenticate", b'Bearer realm="jtari"')],
            )

        # Read the body once to log the JSON-RPC method and tool name, then replay it.
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        raw = b"".join(chunks)
        try:
            rpc = json.loads(raw) if raw else {}
            for item in rpc if isinstance(rpc, list) else [rpc]:
                method = item.get("method")
                tool = (item.get("params") or {}).get("name") if method == "tools/call" else None
                if method:
                    log.info("user=%s method=%s%s", name, method, f" tool={tool}" if tool else "")
        except (ValueError, AttributeError):
            pass

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# ---------------------------------------------------------------- key management CLI


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = keys_file()
    cmd = argv[0] if argv else "list"
    data = _load(path)
    if cmd == "add" and len(argv) == 2:
        name = argv[1]
        if name in data:
            print(f"'{name}' already has a key; remove it first to issue a new one.")
            return 1
        key = "jtari_" + secrets.token_urlsafe(32)
        data[name] = _hash(key)
        _save(path, data)
        print(f"API key for {name} (shown once; send it privately, never by email or chat):\n{key}")
        return 0
    if cmd == "remove" and len(argv) == 2:
        if data.pop(argv[1], None) is None:
            print(f"No key for '{argv[1]}'.")
            return 1
        _save(path, data)
        print(f"Revoked the key for {argv[1]}.")
        return 0
    if cmd == "list":
        print(f"{len(data)} key(s) in {path}:", *sorted(data), sep="\n  ")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
