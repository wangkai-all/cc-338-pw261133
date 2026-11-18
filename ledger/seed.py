"""Seed a realistic demo dataset.

Run through ``python run.py seed`` after the three local services are up.
The seed only uses the public collection/notice APIs, so everything it creates
is exactly the evidence trail an operator would see later.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import Config
from .service import Ledger


def _site_post(cfg: Config, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{cfg.site_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Token": cfg.admin_token},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8"))


def _wait_site(cfg: Config, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cfg.site_url.rstrip('/')}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError("站点未就绪，无法写入演示数据")


def seed(ledger: Ledger, cfg: Config, reset: bool = True) -> dict:
    _wait_site(cfg)
    if reset:
        _site_post(cfg, "/admin/api/reset", {})
        ledger.reset_demo()
    summary = {"steps": []}

    # 1. Two baseline healthy sweeps: everything is present, no evidence for deletion.
    for idx in range(2):
        result = ledger.run_sweep(list(cfg.default_regions), mode="seed")
        summary["steps"].append({"step": f"baseline-{idx + 1}", "status": result.get("status"), "items": result.get("items")})

    # 2. Introduce the scenarios the ledger must tell apart.
    _site_post(cfg, "/admin/api/item", {"item_id": "itm_002", "withdrawn": True})
    _site_post(cfg, "/admin/api/item", {"item_id": "itm_006", "catalog_hidden": ["eu"]})
    _site_post(
        cfg,
        "/admin/api/item",
        {"item_id": "itm_008", "catalog_hidden": ["us", "eu", "apac"], "page_blocked": ["us", "eu", "apac"]},
    )
    # A notice arrives before the body changes: the item stays readable, and
    # must still never be republished by later healthy scans.
    ledger.ingest_notice(
        {
            "notice_id": "ntc_demo_004",
            "item_id": "itm_004",
            "source": "rights-office",
            "statement": "权利方通知：资料 itm_004 已撤回，正文缓存可能短暂可见。",
        },
        actor="seed",
    )
    # A one-off ingestion glitch: item vanishes from every catalog for one
    # sweep only, then returns. This must quarantine, never retract.
    _site_post(cfg, "/admin/api/item", {"item_id": "itm_014", "catalog_hidden": ["us", "eu", "apac"]})

    result = ledger.run_sweep(list(cfg.default_regions), mode="seed")
    summary["steps"].append({"step": "glitch-sweep", "status": result.get("status")})
    _site_post(cfg, "/admin/api/item", {"item_id": "itm_014", "catalog_hidden": []})

    # 3. Two more normal sweeps build the cross-batch evidence for itm_002.
    for idx in range(2):
        result = ledger.run_sweep(list(cfg.default_regions), mode="seed")
        summary["steps"].append({"step": f"evidence-{idx + 1}", "status": result.get("status")})

    ledger.dispatch_outbox(limit=100)
    ledger.run_due_tasks(limit=10)
    ledger.dispatch_outbox(limit=100)

    from .db import connect, rows

    conn = connect(cfg.db_path)
    try:
        states = {r["state"]: int(r["n"]) for r in rows(conn, "SELECT state, COUNT(*) n FROM items GROUP BY state")}
    finally:
        conn.close()
    summary["states"] = states
    return summary


def seed_summary_text(summary: dict) -> str:
    lines = ["演示数据已写入。"]
    for step in summary.get("steps", []):
        lines.append(f"  · {step['step']}: {step.get('status') or step.get('items')}")
    states = summary.get("states") or {}
    lines.append(
        "  状态：active={active} quarantined={quarantined} retracted={retracted}".format(
            active=states.get("active", 0),
            quarantined=states.get("quarantined", 0),
            retracted=states.get("retracted", 0),
        )
    )
    return "\n".join(lines)
