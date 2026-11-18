"""Operator HTTP API + static dashboard."""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .service import Ledger, ServiceError
from .util import iso, json_loads, token_ok


class ApiHandler(BaseHTTPRequestHandler):
    ledger: Ledger = None
    cfg: Config = None
    scheduler: "Scheduler" = None

    server_version = "CatalogLedger/1.0"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ plumbing
    def _send(self, status: int, body: str | bytes, ctype="application/json; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str))

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "at": iso()})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            loaded = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _admin(self) -> bool:
        token = self.headers.get("X-Admin-Token") or ""
        if token_ok(token, self.cfg.admin_token):
            return True
        # Loopback convenience for the local dashboard only.
        client = self.client_address[0] if self.client_address else ""
        if client in ("127.0.0.1", "::1") and self.headers.get("X-Requested-By") == "dashboard":
            return True
        return False

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/health":
            self._json(200, {"ok": True, "service": "catalog-ledger", "at": iso()})
            return
        if path.startswith("/api/"):
            self._api_get(path, query)
            return
        self._static(path)

    def _api_get(self, path: str, query: dict) -> None:
        try:
            if path == "/api/overview":
                self._json(200, self.ledger.overview())
                return
            if path == "/api/items":
                state = (query.get("state", [""])[0] or "").strip()
                q = (query.get("q", [""])[0] or "").strip()
                items = self.ledger.list_items(state=state, query=q)
                self._json(200, {"items": items, "count": len(items), "at": iso()})
                return
            if path.startswith("/api/items/"):
                item_id = path.split("/api/items/", 1)[1].strip("/")
                self._json(200, self.ledger.item_detail(item_id))
                return
            if path == "/api/batches":
                self._json(200, {"batches": self.ledger.list_batches()})
                return
            if path == "/api/tasks":
                self._json(200, {"tasks": self.ledger.list_tasks()})
                return
            if path == "/api/outbox":
                self._json(200, {"outbox": self.ledger.list_outbox()})
                return
            if path == "/api/audit":
                self._json(200, {"audit": self.ledger.list_audit()})
                return
            if path == "/api/policy":
                self._json(200, {"policy": self.cfg.policy.describe(), "regions": list(self.cfg.default_regions)})
                return
            if path == "/api/downstream":
                state = self._downstream_state()
                self._json(200 if state else 502, state or {"error": "下游目录服务不可达"})
                return
            if path == "/api/site/state":
                self._json(200, self._site_call("/admin/api/state", None))
                return
            if path == "/api/site/notices":
                self._json(200, self._site_call("/admin/api/notices", None))
                return
            if path == "/api/site/health":
                self._json(200, self._site_health())
                return
            self._error(404, "未知接口")
        except ServiceError as exc:
            self._error(exc.status, str(exc))
        except Exception as exc:  # never leak a traceback to the browser
            self._error(500, f"内部错误：{type(exc).__name__}: {exc}")

    def _downstream_state(self) -> dict | None:
        try:
            url = f"{self.cfg.downstream_url.rstrip('/')}/public/state"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read(500_000).decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def _site_call(self, api_path: str, payload: dict | None) -> dict:
        """Proxy opt-in site controls from the dashboard."""
        url = f"{self.cfg.site_url.rstrip('/')}{api_path}"
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json", "X-Admin-Token": self.cfg.admin_token},
        )
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return json.loads(resp.read(2_000_000).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read(200_000).decode("utf-8"))
            except (ValueError, OSError):
                return {"error": f"站点返回 {exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"error": f"站点不可达：{exc}"}

    def _site_health(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.cfg.site_url.rstrip('/')}/health", timeout=4) as resp:
                return {"reachable": True, "site": json.loads(resp.read(5000).decode("utf-8"))}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {"reachable": False, "error": str(exc)[:200]}

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/api/"):
            self._error(404, "未知接口")
            return
        if not self._admin():
            self._error(401, "缺少或错误的管理令牌")
            return
        payload = self._read_json()
        try:
            if path == "/api/scan":
                regions = payload.get("regions") or None
                result = self.ledger.run_sweep(regions, mode=str(payload.get("mode") or "manual"), note=str(payload.get("note") or ""))
                self._json(200, {"ok": True, "result": result})
                return
            if path == "/api/notices":
                self._json(201, self.ledger.ingest_notice(payload))
                return
            if path == "/api/tasks":
                self._json(201, self.ledger.queue_task(
                    str(payload.get("kind") or ""),
                    item_id=payload.get("item_id") or None,
                    region=payload.get("region") or None,
                    note=str(payload.get("note") or ""),
                ))
                return
            if path == "/api/dispatch":
                self._json(200, self.ledger.dispatch_outbox(limit=int(payload.get("limit") or 20)))
                return
            if path == "/api/tasks/run":
                self._json(200, self.ledger.run_due_tasks(limit=int(payload.get("limit") or 3)))
                return
            if path == "/api/site/faults":
                self._json(200, self._site_call("/admin/api/faults", payload))
                return
            if path == "/api/site/region":
                self._json(200, self._site_call("/admin/api/region", payload))
                return
            if path == "/api/site/item":
                self._json(200, self._site_call("/admin/api/item", payload))
                return
            if path == "/api/site/reset":
                self._json(200, self._site_call("/admin/api/reset", payload))
                return
            if path.startswith("/api/items/"):
                rest = path.split("/api/items/", 1)[1].strip("/")
                if rest.endswith("/hold"):
                    item_id = rest[: -len("/hold")]
                    self._json(200, self.ledger.set_hold(item_id, bool(payload.get("held")), str(payload.get("reason") or "")))
                    return
                if rest.endswith("/restore"):
                    item_id = rest[: -len("/restore")]
                    self._json(200, self.ledger.restore_item(item_id, str(payload.get("reason") or "")))
                    return
                if rest.endswith("/review"):
                    item_id = rest[: -len("/review")]
                    self._json(200, self.ledger.review_item(item_id, str(payload.get("note") or "")))
                    return
            self._error(404, "未知接口")
        except ServiceError as exc:
            self._error(exc.status, str(exc))
        except Exception as exc:
            self._error(500, f"内部错误：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        if path == "/":
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        base = (self.cfg.web_dir / rel).resolve()
        web_root = self.cfg.web_dir.resolve()
        if web_root not in base.parents and base != web_root:
            self._error(403, "非法路径")
            return
        if not base.is_file():
            self._error(404, "文件不存在")
            return
        ctype = mimetypes.guess_type(str(base))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        if base.name == "index.html":
            html = base.read_text("utf-8")
            html = html.replace("__ADMIN_TOKEN__", self.cfg.admin_token)
            html = html.replace("__SITE_URL__", self.cfg.site_url)
            html = html.replace("__DOWNSTREAM_URL__", self.cfg.downstream_url)
            self._send(200, html, "text/html; charset=utf-8")
            return
        self._send(200, base.read_bytes(), ctype)


class Scheduler:
    """Background loop: auto scans, retry tasks, outbox delivery."""

    def __init__(self, ledger: Ledger, cfg: Config) -> None:
        self.ledger = ledger
        self.cfg = cfg
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        last_tick = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - last_tick >= max(2, self.cfg.task_tick_seconds):
                last_tick = now
                try:
                    self.ledger.dispatch_outbox(limit=25)
                except Exception:
                    pass
                try:
                    self.ledger.run_due_tasks(limit=2)
                except Exception:
                    pass
                try:
                    self.ledger.maybe_auto_scan()
                except Exception:
                    pass
            self.stop.wait(1.0)

    def stop_all(self) -> None:
        self.stop.set()


def build_app_server(host: str, port: int, ledger: Ledger, cfg: Config, scheduler: Scheduler) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"ledger": ledger, "cfg": cfg, "scheduler": scheduler})
    return ThreadingHTTPServer((host, port), handler)


def run_app(host: str, port: int, ledger: Ledger, cfg: Config, scheduler: Scheduler) -> ThreadingHTTPServer:
    server = build_app_server(host, port, ledger, cfg, scheduler)
    threading.Thread(target=server.serve_forever, name="app-api", daemon=True).start()
    return server
