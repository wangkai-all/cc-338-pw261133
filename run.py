#!/usr/bin/env python3
"""One-command launcher for the local demo stack.

    python3 run.py            # site + downstream + ledger + dashboard
    python3 run.py --seed     # same, plus a scripted evidence demo
    python3 run.py --check    # policy self-test, no servers

Ports: dashboard 8080 · publisher site 8090 · downstream 8091.
No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ledger.app import Scheduler, run_app
from ledger.config import load_config
from ledger.downstream import Downstream, run_downstream
from ledger.service import Ledger
from ledger.site import SiteState, run_site


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.15)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catalog withdrawal ledger (local demo)")
    parser.add_argument("--seed", action="store_true", help="load the scripted demo dataset")
    parser.add_argument("--check", action="store_true", help="run the offline policy/collector self-test and exit")
    parser.add_argument("--no-browser", action="store_true", help="do not print the dashboard URL banner")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.check:
        from tests.selfcheck import run_selfcheck

        return run_selfcheck(cfg)

    ledger = Ledger(cfg)
    _wait_http(f"{cfg.downstream_url}/health", timeout=1)  # no-op if not running
    site_state = SiteState(cfg.db_path.parent / "site_state.json")
    site_server = run_site("127.0.0.1", cfg.site_port, site_state, cfg.admin_token, cfg.page_size)
    downstream = Downstream(cfg.downstream_db_path, cfg.downstream_token)
    run_downstream("127.0.0.1", cfg.downstream_port, downstream)
    scheduler = Scheduler(ledger, cfg)
    app_server = run_app("127.0.0.1", cfg.app_port, ledger, cfg, scheduler)

    ok = _wait_http(f"{cfg.site_url}/health") and _wait_http(f"{cfg.downstream_url}/health")
    if not ok:
        print("服务启动超时", file=sys.stderr)
        return 1

    if args.seed:
        from ledger.seed import seed, seed_summary_text

        print(seed_summary_text(seed(ledger, cfg)))

    # Start background work only after seeding so the scheduled sweep cannot
    # collide with the scripted demo scans.
    scheduler.start()

    if not args.no_browser:
        print("=" * 68)
        print("  删除依据台账 —— 本地演示系统")
        print("=" * 68)
        print(f"  台账后台     http://127.0.0.1:{cfg.app_port}/")
        print(f"  站点（可切换地区/故障） http://127.0.0.1:{cfg.site_port}/admin")
        print(f"  站点目录     http://127.0.0.1:{cfg.site_port}/catalog?region=us")
        print(f"  下游目录     http://127.0.0.1:{cfg.downstream_port}/")
        print()
        print("  管理令牌在演示环境已预填；生产请设置 LEDGER_ADMIN_TOKEN 等环境变量。")
        print("  按 Ctrl+C 退出。")
        sys.stdout.flush()

    stop = threading.Event()

    def _shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        scheduler.stop_all()
        for server in (app_server, site_server):
            try:
                server.shutdown()
            except Exception:
                pass
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
