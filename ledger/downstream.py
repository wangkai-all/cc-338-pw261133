"""Downstream catalog service.

Receives append-only events from the ledger and maintains the published
catalog. Two invariants make it safe against reordering and replays:

* ``seq`` is the ledger item version. An event with a lower seq than the stored
  one is ignored (stale/out-of-order delivery cannot resurrect content).
* A ``retract`` event can only be undone by an explicit ``publish`` carrying
  ``allow_restore: true`` (operator action). Health-check "publish" events from
  scans never set that flag, so a stale rescan cannot republish.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import connect, init_downstream_db, row, rows, tx
from .util import iso, json_loads, stable_json, token_ok

DOWNSTREAM_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>下游目录服务</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f7f7f4;color:#1b1b19}
main{max-width:980px;margin:32px auto;padding:0 20px}h1{font-size:22px}.card{background:#fff;border:1px solid #e6e4dc;border-radius:10px;padding:16px 18px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e6e4dc;padding:7px;text-align:left;vertical-align:top}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px}.pub{background:#e3f2e1;color:#0a5c0a}.with{background:#fbe7e7;color:#a31f1f}
code{background:#eceae3;padding:1px 5px;border-radius:4px}.muted{color:#6d6b63}
</style></head><body><main>
<h1>下游目录服务 <span class="muted" style="font-size:14px">（租户当前可见内容）</span></h1>
<div class="card" id="summary"></div>
<div class="card"><h2>目录</h2><div id="items"></div></div>
<div class="card"><h2>最近事件</h2><div id="events"></div></div>
<p class="muted">本页只读。事件接口 <code>/internal/events</code> 需要下游令牌。</p>
<script>
async function load(){
 const r=await fetch('/public/state'); const s=await r.json();
 document.querySelector('#summary').innerHTML=`<b>${s.counts.published}</b> 条已发布 · <b>${s.counts.withheld}</b> 条已撤下 · 最近更新 ${s.updated_at||'—'}`;
 document.querySelector('#items').innerHTML='<table><tr><th>条目</th><th>状态</th><th>序号</th><th>原因</th><th>更新时间</th></tr>'+
   s.items.map(i=>`<tr><td><b>${i.title||i.item_id}</b><div class="muted">${i.item_id}</div></td>
   <td><span class="pill ${i.status==='published'?'pub':'with'}">${i.status==='published'?'已发布':'已撤下'}</span></td>
   <td>${i.seq}</td><td>${i.reason||''}</td><td class="muted">${i.updated_at}</td></tr>`).join('')+'</table>';
 document.querySelector('#events').innerHTML='<table><tr><th>事件</th><th>条目</th><th>类型</th><th>序号</th><th>结果</th><th>时间</th></tr>'+
   s.events.map(e=>`<tr><td><code>${e.event_id}</code></td><td>${e.item_id}</td><td>${e.event_type}</td><td>${e.seq}</td><td>${e.outcome}</td><td class="muted">${e.received_at}</td></tr>`).join('')+'</table>';
}
load();
</script></main></body></html>"""


class Downstream:
    def __init__(self, db_path: Path, token: str) -> None:
        self.db_path = db_path
        self.token = token
        self.lock = threading.Lock()
        init_downstream_db(db_path)

    def _conn(self):
        return connect(self.db_path)

    def apply_event(self, event: dict) -> dict:
        item_id = str(event.get("item_id") or "")
        event_type = str(event.get("event_type") or "")
        event_id = str(event.get("event_id") or f"{item_id}:{event.get('seq')}:{event_type}")
        try:
            seq = int(event.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if not item_id or event_type not in ("publish", "retract", "withhold", "update"):
            return {"ok": False, "error": "bad_event", "status": 400}

        with self.lock:
            conn = self._conn()
            try:
                existing = row(conn, "SELECT * FROM d_events WHERE event_id=?", (event_id,))
                if existing:
                    return {"ok": True, "outcome": "duplicate", "event_id": event_id}
                current = row(conn, "SELECT * FROM d_items WHERE item_id=?", (item_id,))
                if current is not None and seq < int(current["seq"]):
                    outcome = "stale_ignored"
                elif (
                    current is not None
                    and event_type == "publish"
                    and not event.get("allow_restore")
                    and current["status"] == "withheld"
                    and int(current["permanently_restricted"] or 0)
                ):
                    # Only a retraction is permanent. A provisional withhold
                    # (quarantine) may be lifted by later complete evidence.
                    outcome = "blocked_restore_requires_operator"
                else:
                    status = "withheld" if event_type in ("retract", "withhold") else "published"
                    if current is not None and event_type == "update" and current["status"] == "withheld":
                        # An update carries content, not permission: it must
                        # never lift a withhold or retraction.
                        status = "withheld"
                    if current is not None and int(current["permanently_restricted"] or 0) and event_type != "publish":
                        status = "withheld"
                    permanent = 1 if event_type == "retract" or (current is not None and int(current["permanently_restricted"] or 0)) else 0
                    if event_type == "publish" and event.get("allow_restore"):
                        permanent = 0
                    with tx(conn):
                        if current is None:
                            conn.execute(
                                "INSERT INTO d_items(item_id,title,url,status,seq,event_id,reason,excerpt,updated_at,"
                                " published_at,last_event,permanently_restricted)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    item_id,
                                    str(event.get("title") or ""),
                                    str(event.get("url") or ""),
                                    status,
                                    seq,
                                    event_id,
                                    str(event.get("reason") or ""),
                                    str(event.get("excerpt") or ""),
                                    iso(),
                                    iso() if status == "published" else None,
                                    event_type,
                                    permanent,
                                ),
                            )
                        else:
                            conn.execute(
                                "UPDATE d_items SET title=?, url=?, status=?, seq=?, event_id=?, reason=?, excerpt=?,"
                                " updated_at=?, last_event=?, permanently_restricted=?,"
                                " published_at=CASE WHEN ?='published' AND published_at IS NULL THEN ? ELSE published_at END"
                                " WHERE item_id=?",
                                (
                                    str(event.get("title") or current["title"]),
                                    str(event.get("url") or current["url"]),
                                    status,
                                    seq,
                                    event_id,
                                    str(event.get("reason") or ""),
                                    str(event.get("excerpt") or current["excerpt"] or ""),
                                    iso(),
                                    event_type,
                                    permanent,
                                    status,
                                    iso(),
                                    item_id,
                                ),
                            )
                        conn.execute(
                            "INSERT INTO d_events(event_id,item_id,event_type,seq,outcome,payload_json,received_at)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (event_id, item_id, event_type, seq, "applied", stable_json(event), iso()),
                        )
                    outcome = "applied"
                if outcome != "applied":
                    existing_event = row(conn, "SELECT event_id FROM d_events WHERE event_id=?", (event_id,))
                    if not existing_event:
                        with tx(conn):
                            conn.execute(
                                "INSERT INTO d_events(event_id,item_id,event_type,seq,outcome,payload_json,received_at)"
                                " VALUES(?,?,?,?,?,?,?)",
                                (event_id, item_id, event_type, seq, outcome, stable_json(event), iso()),
                            )
                return {"ok": True, "outcome": outcome, "event_id": event_id, "seq": seq}
            finally:
                conn.close()

    def public_state(self) -> dict:
        conn = self._conn()
        try:
            item_rows = rows(conn, "SELECT * FROM d_items ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END, item_id")
            events = rows(conn, "SELECT * FROM d_events ORDER BY id DESC LIMIT 40")
            counts = {"published": 0, "withheld": 0}
            for rec in item_rows:
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            items = [
                {
                    "item_id": rec["item_id"],
                    "title": rec["title"],
                    "url": rec["url"],
                    "status": rec["status"],
                    "seq": rec["seq"],
                    "reason": rec["reason"],
                    "last_event": rec["last_event"],
                    "permanently_restricted": bool(rec["permanently_restricted"]),
                    "excerpt": rec["excerpt"],
                    "updated_at": rec["updated_at"],
                    "published_at": rec["published_at"],
                }
                for rec in item_rows
            ]
            return {
                "counts": counts,
                "items": items,
                "events": [
                    {
                        "event_id": rec["event_id"],
                        "item_id": rec["item_id"],
                        "event_type": rec["event_type"],
                        "seq": rec["seq"],
                        "outcome": rec["outcome"],
                        "received_at": rec["received_at"],
                    }
                    for rec in events
                ],
                "updated_at": items[0]["updated_at"] if items else None,
            }
        finally:
            conn.close()

    def item(self, item_id: str) -> dict:
        conn = self._conn()
        try:
            rec = row(conn, "SELECT * FROM d_items WHERE item_id=?", (item_id,))
            if rec is None:
                return {"item_id": item_id, "present": False}
            events = rows(conn, "SELECT * FROM d_events WHERE item_id=? ORDER BY id DESC LIMIT 20", (item_id,))
            return {
                "item_id": item_id,
                "present": True,
                "title": rec["title"],
                "status": rec["status"],
                "seq": rec["seq"],
                "reason": rec["reason"],
                "last_event": rec["last_event"],
                "permanently_restricted": bool(rec["permanently_restricted"]),
                "excerpt": rec["excerpt"],
                "updated_at": rec["updated_at"],
                "events": [
                    {
                        "event_id": e["event_id"],
                        "event_type": e["event_type"],
                        "seq": e["seq"],
                        "outcome": e["outcome"],
                        "received_at": e["received_at"],
                    }
                    for e in events
                ],
            }
        finally:
            conn.close()


class DownstreamHandler(BaseHTTPRequestHandler):
    service: Downstream = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: str | bytes, ctype="application/json; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, obj: object) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False))

    def _token_ok(self) -> bool:
        return token_ok(self.headers.get("X-Downstream-Token"), self.service.token)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, DOWNSTREAM_HTML, "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"ok": True, "service": "downstream-catalog", "at": iso()})
            return
        if path == "/public/state":
            self._json(200, self.service.public_state())
            return
        if path == "/public/items":
            state = self.service.public_state()
            status = (query.get("status", [""])[0] or "").strip()
            items = state["items"]
            if status:
                items = [i for i in items if i["status"] == status]
            self._json(200, {"items": items, "count": len(items), "at": iso()})
            return
        if path.startswith("/public/items/"):
            self._json(200, self.service.item(path.split("/public/items/", 1)[1]))
            return
        if path.startswith("/internal/items/"):
            if not self._token_ok():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, self.service.item(path.split("/internal/items/", 1)[1]))
            return
        if path == "/internal/events":
            if not self._token_ok():
                self._json(401, {"error": "unauthorized"})
                return
            state = self.service.public_state()
            self._json(200, {"events": state["events"]})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != "/internal/events":
            self._json(404, {"error": "not_found"})
            return
        if not self._token_ok():
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            self._json(400, {"error": "empty_body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "bad_json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad_payload"})
            return
        result = self.service.apply_event(payload)
        self._json(200 if result.get("ok") else 400, result)


def build_downstream_server(host: str, port: int, service: Downstream) -> ThreadingHTTPServer:
    handler = type("BoundDownstreamHandler", (DownstreamHandler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def run_downstream(host: str, port: int, service: Downstream) -> ThreadingHTTPServer:
    server = build_downstream_server(host, port, service)
    threading.Thread(target=server.serve_forever, name="downstream", daemon=True).start()
    return server
