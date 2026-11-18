"""In-process harness for the offline self-test."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
from pathlib import Path

from ledger.config import Config, PolicyConfig
from ledger.downstream import Downstream, build_downstream_server
from ledger.service import Ledger
from ledger.site import SiteState, build_site_server


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-check-")
        base = Path(self.tmp.name)
        policy = PolicyConfig(
            min_scans=3,
            min_regions=2,
            min_scan_batches=2,
            anomaly_extra_scans=2,
            min_body_bytes=40,
        )
        self.site_state = SiteState(None)
        self.site = build_site_server("127.0.0.1", 0, self.site_state, "t-admin", 5)
        self.down = Downstream(base / "downstream.db", "t-down")
        self.down_server = build_downstream_server("127.0.0.1", 0, self.down)
        site_port = self.site.server_address[1]
        down_port = self.down_server.server_address[1]
        self.cfg = Config(
            db_path=base / "ledger.db",
            downstream_db_path=base / "downstream.db",
            site_url=f"http://127.0.0.1:{site_port}",
            downstream_url=f"http://127.0.0.1:{down_port}",
            admin_token="t-admin",
            notice_token="t-notice",
            downstream_token="t-down",
            page_size=5,
            body_budget=6,
            default_regions=("us", "eu", "apac"),
            policy=policy,
            auto_scan_interval=0,
        )
        self.ledger = Ledger(self.cfg)
        for server in (self.site, self.down_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def reset(self) -> None:
        self.site_state.reset()
        self.down = Downstream(self.cfg.downstream_db_path, "t-down")
        down_conn = sqlite3.connect(str(self.cfg.downstream_db_path))
        try:
            down_conn.execute("DELETE FROM d_events")
            down_conn.execute("DELETE FROM d_items")
            down_conn.commit()
        finally:
            down_conn.close()
        for table in (
            "observations",
            "scans",
            "batches",
            "notices",
            "tasks",
            "outbox",
            "audit",
            "body_snapshots",
            "evidence_decisions",
            "holds",
            "items",
            "metrics",
            "settings",
        ):
            conn = sqlite3.connect(str(self.cfg.db_path))
            try:
                conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()
        self.down_state = self.down.public_state()

    def close(self) -> None:
        for server in (self.site, self.down_server):
            try:
                server.shutdown()
            except Exception:
                pass
        self.tmp.cleanup()

    # ---------------------------------------------------------------- site control
    def item(self, item_id: str) -> dict:
        return self.site_state.state["items"][item_id]

    def set_item(self, item_id: str, **changes) -> None:
        self.item(item_id).update(changes)

    def reset_item(self, item_id: str) -> None:
        item = self.item(item_id)
        for key in ("catalog_hidden", "page_blocked", "page_error", "page_fake_404"):
            item[key] = [] if key == "catalog_hidden" else {}
        item["withdrawn"] = False
        item["withdrawn_at"] = None

    def set_region(self, region: str, mode: str) -> None:
        self.site_state.state["regions"][region]["catalog_mode"] = mode

    def set_faults(self, **faults) -> None:
        self.site_state.state["faults"].update(faults)

    # ---------------------------------------------------------------- ledger views
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.cfg.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def state_of(self, item_id: str) -> str:
        conn = self._conn()
        try:
            rec = conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
            return rec["state"] if rec else "missing"
        finally:
            conn.close()

    def item_row(self, item_id: str) -> dict:
        conn = self._conn()
        try:
            rec = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(rec) if rec else {}
        finally:
            conn.close()

    def count(self, sql: str, args: tuple = ()) -> int:
        conn = self._conn()
        try:
            return int(conn.execute(sql, args).fetchone()[0])
        finally:
            conn.close()

    def downstream_status(self, item_id: str) -> str | None:
        detail = self.down.item(item_id)
        return detail.get("status") if detail.get("present") else None

    # ---------------------------------------------------------------- actions
    def sweep(self, regions=None) -> dict:
        return self.ledger.run_sweep(regions or list(self.cfg.default_regions), mode="test")

    def sweeps(self, n: int, regions=None) -> None:
        for _ in range(n):
            self.sweep(regions)

    def flush(self) -> None:
        self.ledger.dispatch_outbox(limit=200)
        self.ledger.dispatch_outbox(limit=200)
