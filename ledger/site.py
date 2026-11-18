"""Local mock publisher site with switchable regions and faults.

Failure modes it can produce on demand:
  * region-scoped page blocks          (403 / 451 / 200-with-error, temporary)
  * fake "not found" without withdrawal
  * 200 OK error pages for catalog or content
  * truncated listings and truncated bodies
  * wrong-region caches (region flips mid-scan)
  * transient 5xx / latency
  * genuine withdrawal (catalog removal + 410 tombstone)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .util import iso, json_loads, stable_json, token_ok, utcnow

CATALOG_MARKER = '<!--CATALOG-LIST-->'
CATALOG_END = '<!--CATALOG-END-->'
BODY_START = '<!--ARTICLE-BODY-START-->'
BODY_END = '<!--ARTICLE-BODY-END-->'

TITLES = [
    "区域市场监管季度观察",
    "跨境数据流动合规指引",
    "新能源供应链风险图谱",
    "城市更新投融资案例集",
    "半导体出口管制追踪",
    "医疗设备准入审查要点",
    "碳边境调节机制解读",
    "东南亚数字支付合规概览",
    "人工智能治理地方立法汇编",
    "港口物流韧性评估",
    "光伏组件价格监测",
    "区域电力市场改革进展",
    "消费品安全召回统计",
    "医疗器械不良事件年报",
    "汽车数据合规操作手册",
    "农业补贴政策年度评估",
    "金融消费者保护执法案例",
    "云计算采购安全要求",
    "生物医药临床审批趋势",
    "航运排放监管动态",
    "关键矿产供应链审查",
    "食品标签新规实务问答",
    "个人征信业务检查通报",
    "工业机器人安全标准解析",
    "数据中心能效约束政策",
    "跨境电商税务处理指引",
    "中小银行资本补充观察",
    "卫星通信频谱分配进展",
    "绿色债券信息披露案例",
    "化学品进出口监管清单",
]
CATEGORIES = ["政策", "报告", "数据", "公告", "案例"]

REGIONS = ("us", "eu", "apac")

FAULT_KEYS = {
    "error_rate": 0.0,
    "latency_ms": 0,
    "body_truncate": False,
    "listing_truncate": False,
    "wrong_region": False,
    "listing_error_200": False,
    "json_error": False,
}


def _default_state() -> dict:
    items = {}
    for idx, title in enumerate(TITLES, start=1):
        item_id = f"itm_{idx:03d}"
        items[item_id] = {
            "title": title,
            "category": CATEGORIES[(idx - 1) % len(CATEGORIES)],
            "withdrawn": False,
            "withdrawn_at": None,
            "catalog_hidden": [],          # regions hiding it from the catalog only
            "page_blocked": {},            # region -> 451
            "page_error": {},              # region -> 200 error page
            "page_fake_404": {},           # region -> 404 without withdrawal
            "revision": 1,
            "changed_at": iso(),
        }
    return {
        "regions": {r: {"catalog_mode": "ok"} for r in REGIONS},
        "items": items,
        "faults": dict(FAULT_KEYS),
        "notices": [],
        "updated_at": iso(),
    }


class SiteState:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict:
        if self.path and self.path.exists():
            try:
                loaded = json_loads(self.path.read_text("utf-8"), None)
                if isinstance(loaded, dict) and "items" in loaded:
                    base = _default_state()
                    base.update(loaded)
                    for key, val in FAULT_KEYS.items():
                        base["faults"].setdefault(key, val)
                    return base
            except (OSError, ValueError):
                pass
        return _default_state()

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def reset(self) -> None:
        with self.lock:
            self.state = _default_state()
        self.save()

    def update(self, mutate) -> dict:
        with self.lock:
            mutate(self.state)
            self.state["updated_at"] = iso()
            snapshot = json.loads(json.dumps(self.state))
        self.save()
        return snapshot

    def item(self, item_id: str) -> dict | None:
        with self.lock:
            item = self.state["items"].get(item_id)
            return json.loads(json.dumps(item)) if item else None

    def items(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state["items"]))

    def faults(self) -> dict:
        with self.lock:
            return dict(self.state["faults"])

    def region_mode(self, region: str) -> str:
        with self.lock:
            return self.state["regions"].get(region, {}).get("catalog_mode", "ok")


def body_text(item_id: str, title: str, revision: int) -> list[str]:
    base = (
        f"{title}基于公开资料与区域调研整理，供目录服务编排与内容核对使用。"
        "本文档版本号为 {rev}，内容标识 {iid}。"
    ).format(rev=revision, iid=item_id)
    paragraphs = [
        base,
        "第一节说明适用范围与数据口径。目录服务只发布经过完整性校验的正文，"
        "任何截断、错误提示页或地区限制页都不会被登记为内容更新。",
        "第二节记录本期的关键变化。若条目从目录中消失，采集端需要多次完整目录扫描，"
        "并结合正文探测来判断是真实撤回、暂时不可见还是抓取不全。",
        "第三节给出操作建议。对疑似撤回的条目应先进入待复核状态，"
        "自动删除必须满足跨地区、跨批次的证据门槛。",
        f"文档校验尾标：{item_id}-{revision}-END。",
    ]
    return paragraphs


def render_catalog(region: str, page: int, size: int, state: SiteState) -> tuple[int, str]:
    faults = state.faults()
    mode = state.region_mode(region)
    if mode == "blocked403":
        return 403, error_page(region, "region_blocked", "该地区目录暂时不可访问。", 403)
    if mode == "error200" or faults.get("listing_error_200"):
        return 200, error_page(region, "catalog_error", "目录服务暂时不可用，请稍后重试。", 200)

    items = state.items()
    visible = []
    for item_id in sorted(items):
        item = items[item_id]
        if item.get("withdrawn"):
            continue
        if region in (item.get("catalog_hidden") or []):
            continue
        visible.append(item_id)

    true_total = len(visible)
    if faults.get("listing_truncate"):
        # Serve a short page while still declaring the real total: this is the
        # "silent truncation" a naive collector would read as withdrawals.
        visible = visible[:-3] if len(visible) > 3 else visible[:1]

    total = true_total
    start = max(0, (page - 1) * size)
    page_items = visible[start : start + size]
    rows = []
    for item_id in page_items:
        item = items[item_id]
        rows.append(
            '<article class="entry" data-item-id="{iid}" data-revision="{rev}">'
            '<h2><a href="/content/{iid}">{title}</a></h2>'
            '<p class="meta"><span class="category">{cat}</span> · 更新于 {at}</p>'
            "</article>".format(
                iid=item_id,
                rev=item.get("revision", 1),
                title=item["title"],
                cat=item["category"],
                at=(item.get("changed_at") or "")[:10],
            )
        )
    if not rows and page <= 1:
        rows.append('<p class="empty">本期目录暂无条目。</p>')

    html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>公开资料目录 · {region}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><header><h1>公开资料目录</h1><p>地区：{region}</p></header>
<main id="catalog" data-region="{region}" data-page="{page}" data-size="{size}" data-total="{total}">
{catalog_marker}
{rows}
{end_marker}
</main>
<footer><p>本地演示站点 · 第 {page} 页 · 共 {total} 条</p></footer></body></html>""".format(
        region=region,
        page=page,
        size=size,
        total=total,
        catalog_marker=CATALOG_MARKER,
        rows="\n".join(rows),
        end_marker=CATALOG_END,
    )
    return 200, html


def error_page(region: str, code: str, message: str, status: int) -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>服务提示 · {region}</title></head><body>
<main class="service-message" data-error="{code}" data-region="{region}" data-status="{status}">
<h1>无法显示内容</h1><p>{message}</p>
<p>提示代码：{code}</p>
</main></body></html>""".format(region=region, code=code, message=message, status=status)


def render_body(item_id: str, region: str, state: SiteState) -> tuple[int, str, dict]:
    faults = state.faults()
    item = state.item(item_id)
    if not item:
        return 404, error_page(region, "not_found", "未找到该资料。", 404), {}

    if item.get("withdrawn"):
        html = """<!doctype html><html><head><meta charset="utf-8">
<meta name="catalog-status" content="withdrawn"></head><body>
<main data-status="withdrawn" data-item-id="{iid}"><h1>资料已撤回</h1>
<p>发布方已从目录中撤回该资料，内容不再提供。</p></main></body></html>""".format(iid=item_id)
        return 410, html, {"catalog_status": "withdrawn"}

    if region in (item.get("page_blocked") or {}):
        return 451, error_page(region, "region_blocked", "依据当地规定，该页面在您所在地区不可用。", 451), {}
    if region in (item.get("page_error") or {}):
        return 200, error_page(region, "content_unavailable", "内容暂时无法加载，请稍后重试。", 200), {}
    if region in (item.get("page_fake_404") or {}):
        return 404, error_page(region, "not_found", "未找到该资料。", 404), {}

    paragraphs = body_text(item_id, item["title"], int(item.get("revision", 1)))
    body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><meta name="catalog-item" content="{iid}">
<meta name="catalog-revision" content="{rev}"></head><body>
<article><h1>{title}</h1><p class="category">{cat}</p>
{start}
{body}
{end}
</article>
<footer>来源：本地演示站点 · 地区 {region}</footer></body></html>""".format(
        title=item["title"],
        iid=item_id,
        rev=item.get("revision", 1),
        cat=item["category"],
        start=BODY_START,
        body=body,
        end=BODY_END,
        region=region,
    )
    if faults.get("body_truncate"):
        cut = max(120, int(len(html) * 0.55))
        html = html[:cut]
    return 200, html, {"catalog_status": "active", "revision": int(item.get("revision", 1))}


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class SiteHandler(BaseHTTPRequestHandler):
    server_version = "MockPublisher/1.0"
    state: SiteState = None  # injected
    admin_token = "demo-admin-secret"
    page_size = 20

    def log_message(self, fmt, *args):  # quiet
        pass

    # ------------------------------------------------------------------ util
    def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, val in (extra or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, obj: object) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            return {}
        try:
            loaded = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _admin_ok(self) -> bool:
        token = self.headers.get("X-Admin-Token") or ""
        return token_ok(token, self.admin_token)

    def _maybe_fault(self) -> bool:
        faults = self.state.faults()
        latency = int(faults.get("latency_ms") or 0)
        if latency > 0:
            time.sleep(min(latency, 3000) / 1000.0)
        rate = float(faults.get("error_rate") or 0)
        if rate > 0:
            import random

            if random.random() < rate:
                self._send(503, error_page("", "server_busy", "服务暂时不可用。", 503), extra={"Retry-After": "2"})
                return True
        return False

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send(200, LANDING_HTML)
            return
        if path == "/health":
            self._json(200, {"ok": True, "service": "publisher-site", "at": iso()})
            return
        if path == "/robots.txt":
            self._send(200, "User-agent: *\nDisallow: /admin\n", "text/plain; charset=utf-8")
            return
        if path == "/catalog":
            if self._maybe_fault():
                return
            region = (query.get("region", ["us"])[0] or "us").lower()
            if region not in REGIONS:
                self._json(400, {"error": "unknown_region", "regions": list(REGIONS)})
                return
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
                size = max(1, min(100, int(query.get("size", [str(self.page_size)])[0])))
            except ValueError:
                self._json(400, {"error": "bad_pagination"})
                return
            served_region = region
            faults = self.state.faults()
            if faults.get("wrong_region"):
                served_region = "us"
            status, html = render_catalog(served_region, page, size, self.state)
            self._send(
                status,
                html,
                extra={
                    "X-Site-Region": served_region,
                    "X-Request-Region": region,
                    "X-Site-Page": page,
                    "X-Site-Size": size,
                },
            )
            return
        if path.startswith("/content/"):
            if self._maybe_fault():
                return
            item_id = path.split("/content/", 1)[1].strip("/")
            region = (query.get("region", ["us"])[0] or "us").lower()
            if region not in REGIONS:
                self._json(400, {"error": "unknown_region", "regions": list(REGIONS)})
                return
            status, html, _meta = render_body(item_id, region, self.state)
            self._send(status, html, extra={"X-Site-Region": region})
            return
        if path == "/admin":
            self._send(200, ADMIN_HTML)
            return
        if path == "/admin/api/state":
            if not self._admin_ok():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, self.state.snapshot())
            return
        if path == "/admin/api/notices":
            if not self._admin_ok():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"notices": list(reversed(self.state.snapshot().get("notices", [])))})
            return
        self._send(404, error_page("", "not_found", "页面不存在。", 404))

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/admin/api/"):
            self._send(404, error_page("", "not_found", "页面不存在。", 404))
            return
        if not self._admin_ok():
            self._json(401, {"error": "unauthorized"})
            return
        payload = self._read_json()
        action = path[len("/admin/api/") :]

        if action == "reset":
            self.state.reset()
            self._json(200, {"ok": True, "state": self.state.snapshot()})
            return

        if action == "region":
            region = str(payload.get("region", "")).lower()
            mode = str(payload.get("catalog_mode", "ok"))
            if region not in REGIONS or mode not in ("ok", "blocked403", "error200"):
                self._json(400, {"error": "bad_region_or_mode"})
                return

            def mutate(state):
                state["regions"].setdefault(region, {})["catalog_mode"] = mode

            self.state.update(mutate)
            self._json(200, {"ok": True, "region": region, "catalog_mode": mode})
            return

        if action == "item":
            item_id = str(payload.get("item_id", ""))
            if self.state.item(item_id) is None:
                self._json(404, {"error": "unknown_item"})
                return
            allowed = {"withdrawn", "catalog_hidden", "page_blocked", "page_error", "page_fake_404", "revision"}
            changes = {k: payload[k] for k in payload if k in allowed}

            def mutate(state):
                item = state["items"][item_id]
                if "withdrawn" in changes and bool(changes["withdrawn"]) != bool(item.get("withdrawn")):
                    item["withdrawn"] = bool(changes["withdrawn"])
                    item["withdrawn_at"] = iso() if item["withdrawn"] else None
                    if not item["withdrawn"]:
                        item["revision"] = int(item.get("revision", 1)) + 1
                    item["changed_at"] = iso()
                if "revision" in changes:
                    item["revision"] = max(1, int(changes["revision"]))
                for key in ("catalog_hidden", "page_blocked", "page_error", "page_fake_404"):
                    if key in changes:
                        if isinstance(changes[key], list):
                            item[key] = [str(x).lower() for x in changes[key] if str(x).lower() in REGIONS]
                        elif isinstance(changes[key], dict):
                            item[key] = {str(k).lower(): True for k in changes[key] if str(k).lower() in REGIONS}

            self.state.update(mutate)
            self._json(200, {"ok": True, "item": self.state.item(item_id)})
            return

        if action == "faults":
            def mutate(state):
                for key, val in payload.items():
                    if key not in FAULT_KEYS:
                        continue
                    if key in ("body_truncate", "listing_truncate", "wrong_region", "listing_error_200", "json_error"):
                        state["faults"][key] = bool(val)
                    else:
                        state["faults"][key] = max(0, min(1, float(val))) if key == "error_rate" else max(0, min(3000, int(val)))

            self.state.update(mutate)
            self._json(200, {"ok": True, "faults": self.state.faults()})
            return

        if action == "notice":
            # Publisher posts a withdrawal notice on its public notice board; the
            # collector can ingest it through its own authenticated intake.
            item_id = str(payload.get("item_id", ""))
            if self.state.item(item_id) is None:
                self._json(404, {"error": "unknown_item"})
                return
            notice = {
                "notice_id": str(payload.get("notice_id") or f"ntc_{int(time.time()*1000)}"),
                "item_id": item_id,
                "source": str(payload.get("source") or "publisher-notice-board"),
                "statement": str(payload.get("statement") or "发布方声明撤回该资料。"),
                "effective_at": payload.get("effective_at") or iso(),
                "received_at": iso(),
            }

            def mutate(state):
                state["notices"].append(notice)

            self.state.update(mutate)
            self._json(201, {"ok": True, "notice": notice})
            return

        self._json(404, {"error": "unknown_action"})


LANDING_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>本地演示站点</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f7f7f4;color:#1b1b19}
main{max-width:640px;margin:40px auto;padding:0 20px}h1{font-size:22px}
a{color:#2a78d6}code{background:#eceae3;padding:2px 6px;border-radius:4px}
.card{background:#fff;border:1px solid #e6e4dc;border-radius:10px;padding:18px 20px;margin:16px 0}
</style></head><body><main>
<h1>区域发布站点（本地演示）</h1>
<div class="card"><h2>采集入口</h2>
<p><a href="/catalog?region=us">/catalog?region=us</a> · <a href="/catalog?region=eu">eu</a> · <a href="/catalog?region=apac">apac</a></p>
<p>支持 <code>page</code> 与 <code>size</code> 参数。</p></div>
<div class="card"><h2>故障与地区控制</h2>
<p><a href="/admin">打开站点控制台</a>（需管理令牌，演示环境已预填）。</p>
<p>可以按地区屏蔽目录、按地区屏蔽单个页面、制造 200 错误页、截断列表或正文、错误地区缓存等。</p></div>
<div class="card"><h2>阅读顺序</h2><p>先在控制台制造故障，再回到台账后台发起扫描，观察判定证据。</p></div>
</main></body></html>"""


ADMIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>站点控制台</title>
<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f7f7f4;color:#1b1b19}
main{max-width:900px;margin:32px auto;padding:0 20px}button{font:inherit;padding:6px 10px;border:1px solid #c9c6bb;background:#fff;border-radius:6px;cursor:pointer}
button:hover{background:#f0eee7}select,input{font:inherit;padding:5px 8px;border:1px solid #c9c6bb;border-radius:6px;background:#fff}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #e6e4dc;padding:6px;text-align:left}
code{background:#eceae3;padding:1px 5px;border-radius:4px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
.card{background:#fff;border:1px solid #e6e4dc;border-radius:10px;padding:16px 18px;margin:14px 0}
.ok{color:#006300}.bad{color:#b3261e}pre{background:#161614;color:#e8e6df;padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:220px}
</style></head><body><main>
<h1>区域发布站点 · 控制台</h1>
<p>令牌 <input id="token" value="demo-admin-secret" size="20"> <button onclick="loadState()">连接</button>
<span id="conn"></span></p>
<div class="card"><h2>目录故障</h2><div id="faults"></div></div>
<div class="card"><h2>地区目录</h2><div id="regions"></div></div>
<div class="card"><h2>条目（前 12 条）</h2><div id="items"></div></div>
<div class="card"><h2>撤回通知板</h2><div id="notices"></div></div>
<script>
const R=['us','eu','apac'];
const $=s=>document.querySelector(s);
function tok(){return $('#token').value}
async function api(path,body){
  const r=await fetch('/admin/api/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Admin-Token':tok()},body:body?JSON.stringify(body):undefined});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||r.status); return j;
}
async function loadState(){
  try{
    const s=await api('state'); window.S=s; $('#conn').textContent='已连接'; $('#conn').className='ok'; render(); }
  catch(e){$('#conn').textContent='连接失败：'+e.message; $('#conn').className='bad'}
}
async function setFault(k,v){await api('faults',{[k]:v}); await loadState()}
async function setItem(id,patch){await api('item',{item_id:id,...patch}); await loadState()}
async function setRegion(r,m){await api('region',{region:r,catalog_mode:m}); await loadState()}
async function postNotice(id){
  const src=prompt('通知来源','rights-office'); if(!src) return;
  await api('notice',{item_id:id,source:src,statement:'发布方撤回该资料，通知先于正文状态到达。'});
  await loadState();
  alert('通知已写入站点通知板。请在台账后台通过“撤回通知”入口录入同一来源的通知。');
}
function render(){
  const s=window.S,f=s.faults;
  $('#faults').innerHTML=`
   <div class="row">错误率 <input id="er" type="number" min="0" max="1" step="0.05" value="${f.error_rate}" style="width:80px"> <button onclick="setFault('error_rate',+$('#er').value)">应用</button>
   延迟(ms) <input id="lat" type="number" min="0" max="3000" step="100" value="${f.latency_ms}" style="width:90px"> <button onclick="setFault('latency_ms',+$('#lat').value)">应用</button></div>
   <div class="row">
    <label><input type="checkbox" ${f.listing_truncate?'checked':''} onchange="setFault('listing_truncate',this.checked)"> 列表截断（丢尾部 3 条）</label>
    <label><input type="checkbox" ${f.body_truncate?'checked':''} onchange="setFault('body_truncate',this.checked)"> 正文截断</label>
    <label><input type="checkbox" ${f.listing_error_200?'checked':''} onchange="setFault('listing_error_200',this.checked)"> 列表返回 200 错误页</label>
    <label><input type="checkbox" ${f.wrong_region?'checked':''} onchange="setFault('wrong_region',this.checked)"> 错误地区缓存</label>
   </div>`;
  $('#regions').innerHTML=R.map(r=>`<div class="row"><b>${r}</b>
    <select onchange="setRegion('${r}',this.value)">${['ok','blocked403','error200'].map(m=>`<option value="${m}" ${s.regions[r].catalog_mode===m?'selected':''}>${m==='ok'?'正常':m==='blocked403'?'403 屏蔽':'200 错误页'}</option>`).join('')}</select></div>`).join('');
  const ids=Object.keys(s.items).slice(0,12);
  $('#items').innerHTML=`<table><tr><th>条目</th><th>撤回</th><th>隐藏目录(地区)</th><th>页面451</th><th>200错误页</th><th>假404</th><th></th></tr>`+
   ids.map(id=>{const it=s.items[id];return `<tr><td title="${id}">${it.title.slice(0,14)}</td>
    <td><input type="checkbox" ${it.withdrawn?'checked':''} onchange="setItem('${id}',{withdrawn:this.checked})"></td>
    <td>${R.map(r=>`<label><input type="checkbox" ${(it.catalog_hidden||[]).includes(r)?'checked':''} onchange="toggleRegion('${id}','catalog_hidden','${r}',this.checked)">${r}</label>`).join(' ')}</td>
    <td>${R.map(r=>`<label><input type="checkbox" ${(it.page_blocked||{})[r]?'checked':''} onchange="toggleRegion('${id}','page_blocked','${r}',this.checked)">${r}</label>`).join(' ')}</td>
    <td>${R.map(r=>`<label><input type="checkbox" ${(it.page_error||{})[r]?'checked':''} onchange="toggleRegion('${id}','page_error','${r}',this.checked)">${r}</label>`).join(' ')}</td>
    <td>${R.map(r=>`<label><input type="checkbox" ${(it.page_fake_404||{})[r]?'checked':''} onchange="toggleRegion('${id}','page_fake_404','${r}',this.checked)">${r}</label>`).join(' ')}</td>
    <td><button onclick="postNotice('${id}')">发布撤回通知</button></td></tr>`}).join('')+`</table>`;
  $('#notices').innerHTML=(s.notices||[]).length?`<table>`+s.notices.slice(-5).reverse().map(n=>`<tr><td>${n.notice_id}</td><td>${n.item_id}</td><td>${n.source}</td><td>${n.received_at}</td></tr>`).join('')+`</table>`:'<p>暂无通知。</p>';
}
async function toggleRegion(id,key,r,on){
  const it=window.S.items[id]; const cur=Array.isArray(it[key])?[...it[key]]:Object.keys(it[key]||{});
  const next=on?[...new Set([...cur,r])]:cur.filter(x=>x!==r);
  await setItem(id,{[key]:next});
}
loadState();
</script></main></body></html>"""


def build_site_server(host: str, port: int, state: SiteState, admin_token: str, page_size: int = 20) -> ThreadingHTTPServer:
    handler = type(
        "BoundSiteHandler",
        (SiteHandler,),
        {"state": state, "admin_token": admin_token, "page_size": page_size},
    )
    return ThreadingHTTPServer((host, port), handler)


def run_site(host: str, port: int, state: SiteState, admin_token: str, page_size: int = 20) -> ThreadingHTTPServer:
    server = build_site_server(host, port, state, admin_token, page_size)
    thread = threading.Thread(target=server.serve_forever, name="publisher-site", daemon=True)
    thread.start()
    return server


def empty_state() -> dict:
    return _default_state()
