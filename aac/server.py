"""Loopback-only JSON API and static asset server for the research prototype."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from .config import ROOT, load_env
from .llm import OpenAIProvider
from .prediction import recommend
from .sentences import generate_sentence
from .storage import Store
from .vocabulary import CATEGORIES, CATEGORY_IDS, VOCABULARY, validate_ids

CONTEXTS = {"any", "home", "school", "meal"}


def bounded_int(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("숫자 범위를 확인해 주세요.")
    return value


def name_value(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 30:
        raise ValueError("이름은 1~30자로 입력해 주세요.")
    return value.strip()


class Application:
    def __init__(self, store, provider=None, local_provider=None):
        self.store, self.provider, self.local_provider = store, provider, local_provider

    def handle(self, method, path, body, profile_id):
        if method == "GET" and path == "/api/bootstrap":
            return {"vocabulary": VOCABULARY, "categories": CATEGORIES, "cloud_available": bool(self.provider),
                    "local_model_available": bool(self.local_provider),
                    "recommendation_timeout_ms": max(35000, int(getattr(self.local_provider, "timeout", 25) * 1000) + 5000)}
        if method == "POST" and path == "/api/profiles":
            return self.store.create_profile(name_value(body.get("name", "나")))
        profile = self.store.profile(profile_id)
        provider = self.provider if profile["cloud_enabled"] and self.provider else self.local_provider
        if method == "GET" and path == "/api/profile":
            return profile
        if method == "POST" and path == "/api/profile":
            enabled = body.get("cloud_enabled", False)
            if type(enabled) is not bool:
                raise ValueError("추천 설정을 확인해 주세요.")
            if enabled and not self.provider:
                raise ValueError("서버에 API 키가 설정되어 있지 않아요.")
            return self.store.update_profile(profile_id, name_value(body.get("name")), enabled)
        if method == "GET" and path == "/api/history":
            return {"messages": self.store.history(profile_id), "stats": self.store.stats(profile_id)}
        if method == "POST" and path == "/api/history/clear":
            self.store.clear_history(profile_id)
            return {"cleared": True}
        if method == "GET" and path == "/api/export":
            history = self.store.history(profile_id, limit=10000)
            return {"schema_version": 1, "kind": "user-confirmed-aac-interactions",
                    "records": [{k: v for k, v in row.items() if k not in {"profile_id", "id"}} for row in history],
                    "notice": "직접 수정한 문장도 포함됩니다. 학습에 사용하기 전에 검토하세요."}
        if method == "POST" and path in {"/api/recommend", "/api/sentence", "/api/confirm"}:
            selected = validate_ids(body.get("selected", []), allow_empty=path == "/api/recommend")
            context = body.get("context", "any")
            if not isinstance(context, str) or context not in CONTEXTS:
                raise ValueError("상황을 확인해 주세요.")
            if path == "/api/recommend":
                category = body.get("category")
                if category is not None and (not isinstance(category, str) or category not in CATEGORY_IDS):
                    raise ValueError("분류를 확인해 주세요.")
                excluded = validate_ids(body.get("excluded", []), maximum=len(VOCABULARY))
                return recommend(selected, self.store.history(profile_id), category, context, excluded, provider=provider)
            if path == "/api/sentence":
                return generate_sentence(selected, provider)
            sentence = body.get("sentence")
            if not isinstance(sentence, str) or not 1 <= len(sentence.strip()) <= 300:
                raise ValueError("문장은 1~300자로 입력해 주세요.")
            mode = body.get("mode", "adaptive")
            if mode not in ("adaptive", "static"):
                raise ValueError("선택 모드를 확인해 주세요.")
            message_id = body.get("id")
            try:
                message_id = str(uuid.UUID(message_id))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("메시지 식별자를 확인해 주세요.") from None
            return self.store.confirm(profile_id, message_id, selected, sentence.strip(), mode, context,
                                      bounded_int(body.get("selection_count"), len(selected), 10000),
                                      bounded_int(body.get("elapsed_ms"), 0, 86400000))
        raise FileNotFoundError("요청한 기능을 찾을 수 없어요.")


def make_handler(app, allowed_hosts=(), allowed_origins=()):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AdaptiveAAC/0.1"

        def log_message(self, *_args):
            pass  # Communication content and profile tokens never enter access logs.

        def respond(self, status, content, mime="application/json; charset=utf-8"):
            data = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self):
            try:
                host = self.headers.get("Host", "")
                allowed = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}", *allowed_hosts}
                if host not in allowed:
                    self.respond(403, {"error": "로컬 주소로 접속해 주세요."})
                    return
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host}" and origin not in allowed_origins:
                    self.respond(403, {"error": "다른 사이트의 요청은 허용하지 않아요."})
                    return
                path = urlsplit(self.path).path
                body = {}
                if self.command == "POST":
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.respond(415, {"error": "JSON 형식으로 요청해 주세요."})
                        return
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 20000:
                        self.respond(413, {"error": "요청 크기를 확인해 주세요."})
                        return
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("JSON 객체가 필요해요.")
                if path.startswith("/api/"):
                    result = app.handle(self.command, path, body, self.headers.get("X-Profile-ID", ""))
                    self.respond(200, result)
                    return
                assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
                if self.command != "GET" or path not in assets:
                    raise FileNotFoundError()
                filename, mime = assets[path]
                self.respond(200, (ROOT / "web" / filename).read_bytes(), mime + "; charset=utf-8")
            except (ValueError, UnicodeError):
                self.respond(400, {"error": "요청 내용을 확인해 주세요. 그림 선택이나 입력값이 올바르지 않아요."})
            except LookupError as error:
                self.respond(401, {"error": str(error)})
            except FileNotFoundError:
                self.respond(404, {"error": "요청한 주소를 찾을 수 없어요."})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logging.exception("AAC request failed")
                self.respond(500, {"error": "저장 또는 연결에 문제가 있어요. 잠시 후 다시 시도해 주세요."})

        do_GET = dispatch
        do_POST = dispatch

    return Handler


def env_list(name):
    return frozenset(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def main():
    load_env()
    key = os.environ.get("OPENAI_API_KEY", "")
    provider = OpenAIProvider(key, os.environ.get("OPENAI_MODEL", "gpt-4o-mini")) if key else None
    from .local_model import configured_local_provider
    try:
        local_provider = configured_local_provider()
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"Local model configuration error: {error}") from None
    path = Path(os.environ.get("AAC_DB_PATH", "var/aac.sqlite3"))
    if not path.is_absolute():
        path = ROOT / path
    port = int(os.environ.get("AAC_PORT", "8765"))
    # Deployment behind a reverse proxy only: localhost stays the default.
    bind = os.environ.get("AAC_HOST", "127.0.0.1")
    hosts = env_list("AAC_ALLOWED_HOSTS")
    origins = env_list("AAC_ALLOWED_ORIGINS")
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(Application(Store(path), provider, local_provider), hosts, origins))
    except OSError as error:
        raise SystemExit(f"Cannot open localhost:{port}: {error}. Check local server permissions or set AAC_PORT to another port.") from None
    print(f"Adaptive AAC: http://localhost:{server.server_port}", flush=True)
    print("Press Ctrl+C to stop. Cloud recommendations require opt-in in Settings.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if local_provider:
            local_provider.close()
