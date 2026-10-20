"""Minimal v2 fixture, reachable only inside a network-none test namespace."""
import base64
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import struct
import threading

LOCK = threading.Lock()
OUT = Path("/fixture/events.jsonl")


def record(message):
    with LOCK, OUT.open("a") as stream:
        stream.write(json.dumps(message) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_): pass

    def reply(self, body):
        body = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
        data = json.loads(raw)
        # Older releases POST task results without a JSON-RPC wrapper.
        if "method" not in data and "task_id" in data:
            data = {"method": "agent.taskResult", "params": data}
        record(data)
        self.reply({"jsonrpc": "2.0", "result": {"status": "ok"}})

    def frame(self, value, opcode=1):
        payload = json.dumps(value).encode() if opcode == 1 else value
        size = len(payload)
        header = bytes([0x80 | opcode, size]) if size < 126 else bytes([0x80 | opcode, 126]) + struct.pack("!H", size)
        self.wfile.write(header + payload); self.wfile.flush()

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.reply({"ok": True}); return
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        # No compression extension: the client must send uncompressed frames.
        self.end_headers()
        for task, kind, target in [(1, "icmp", "127.0.0.1"), (2, "tcp", "127.0.0.1:8080"),
                                   (3, "http", "http://127.0.0.1:8080/ping")]:
            self.frame({"jsonrpc": "2.0", "method": "agent.ping", "params": {
                "ping_task_id": task, "ping_type": kind, "ping_target": target}})
        self.frame({"jsonrpc": "2.0", "method": "agent.exec", "params": {
            "task_id": "disabled-test", "command": "echo this-command-must-not-run"}})
        self.frame({"jsonrpc": "2.0", "method": "agent.file", "params": {
            "uuid": "test-only", "request_id": "disabled-files", "op": "list", "args": {"path": "/"}}})
        message = bytearray()
        try:
            while True:
                header = self.rfile.read(2)
                if len(header) != 2: break
                first, second = header
                opcode, size = first & 15, second & 127
                if size == 126: size = struct.unpack("!H", self.rfile.read(2))[0]
                elif size == 127: size = struct.unpack("!Q", self.rfile.read(8))[0]
                if size > 4 * 1024 * 1024: raise ValueError("Fixture frame too large")
                mask = self.rfile.read(4) if second & 128 else None
                payload = self.rfile.read(size)
                if mask: payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 8: break
                if opcode == 9: self.frame(payload, 10); continue
                if opcode == 10: continue
                message.extend(payload)
                if first & 128:
                    record(json.loads(message)); message.clear()
        except (ConnectionError, OSError):
            pass
        self.close_connection = True


plain = ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
threading.Thread(target=plain.serve_forever, daemon=True).start()
tls = ThreadingHTTPServer(("127.0.0.1", 8443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/fixture/cert.pem", "/fixture/key.pem")
tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
Path("/fixture/ready").touch()
tls.serve_forever()
