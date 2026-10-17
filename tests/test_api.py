from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from aac.server import Application, make_handler
from aac.storage import Store


class MemorySocket:
    """Exercise the real HTTP handler without opening a network port."""
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, *_args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")
        self.app = Application(self.store)
        self.profile = self.app.handle("POST", "/api/profiles", {"name": "test"}, "")

    def call(self, method, path, body=None):
        return self.app.handle(method, path, body or {}, self.profile["id"])

    def http(self, path="/api/bootstrap", method="GET", body=None, headers=None, hosts=(), origins=()):
        payload = json.dumps(body).encode() if body is not None else b""
        fields = {"Host": "localhost:8765", "Content-Type": "application/json", "Content-Length": str(len(payload)), "X-Profile-ID": self.profile["id"]}
        fields.update(headers or {})
        request = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        connection = MemorySocket(request.encode() + payload)
        make_handler(self.app, hosts, origins)(connection, ("127.0.0.1", 12345), SimpleNamespace(server_port=8765))
        head, content = connection.output.getvalue().split(b"\r\n\r\n", 1)
        return int(head.split(b" ")[1]), head.decode(), content

    def test_end_to_end_confirm_export_and_delete(self):
        selection = {"selected": ["eat", "bread"]}
        self.assertTrue(self.call("POST", "/api/recommend", {"selected": ["eat"]})["symbols"])
        sentence = self.call("POST", "/api/sentence", selection)["sentence"]
        self.assertEqual(self.call("GET", "/api/history")["messages"], [])
        message = dict(selection, id=str(uuid.uuid4()), sentence=sentence, selection_count=2, elapsed_ms=2500)
        self.call("POST", "/api/confirm", message)
        self.call("POST", "/api/confirm", message)
        export = self.call("GET", "/api/export")
        self.assertEqual(len(export["records"]), 1)
        self.assertNotIn("profile_id", export["records"][0])
        self.call("POST", "/api/history/clear")
        self.assertEqual(self.call("GET", "/api/history")["messages"], [])

    def test_no_cloud_calls_without_profile_opt_in(self):
        class Bomb:
            def recommend(self, *_):
                raise AssertionError("Cloud called without consent")
        self.app.provider = Bomb()
        self.assertEqual(self.call("POST", "/api/recommend", {"selected": ["eat"]})["source"], "local")

    def test_cloud_opt_in_requires_key(self):
        with self.assertRaises(ValueError):
            self.call("POST", "/api/profile", {"name": "나", "cloud_enabled": True})

    def test_http_rejects_cross_origin_host_and_path_traversal(self):
        self.assertEqual(self.http(headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.http(headers={"Host": "attacker.example:8765"})[0], 403)
        self.assertEqual(self.http(path="/../.env")[0], 404)
        self.assertEqual(self.http(path="/.env")[0], 404)

    def test_http_allows_only_configured_proxy_host_and_origin(self):
        proxy = {"hosts": {"aac.example"}, "origins": {"https://aac.example"}}
        self.assertEqual(self.http(headers={"Host": "aac.example", "Origin": "https://aac.example"}, **proxy)[0], 200)
        self.assertEqual(self.http(headers={"Host": "aac.example"})[0], 403)
        self.assertEqual(self.http(headers={"Host": "aac.example", "Origin": "https://attacker.example"}, **proxy)[0], 403)
        self.assertEqual(self.http(headers={"Host": "attacker.example"}, **proxy)[0], 403)

    def test_http_validation_and_missing_profile(self):
        self.assertEqual(self.http(path="/api/recommend", method="POST", body={"selected": ["unknown"]})[0], 400)
        self.assertEqual(self.http(path="/api/recommend", method="POST", body=[], headers={})[0], 400)
        self.assertEqual(self.http(path="/api/recommend", method="POST", body={}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.http(path="/api/profile", headers={"X-Profile-ID": "unknown"})[0], 401)
        self.assertEqual(self.http(path="/api/recommend", method="POST", body={}, headers={"Content-Length": "99999"})[0], 413)

    def test_static_assets_and_security_headers(self):
        for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
            status, headers, content = self.http(path=path)
            self.assertEqual(status, 200)
            self.assertIn("Content-Security-Policy", headers)
            self.assertTrue(content)


if __name__ == "__main__":
    unittest.main()
