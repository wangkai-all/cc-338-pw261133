"""Ledger orchestration: collection, evidence decisions, tasks and outbox."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Iterable, Iterator

from .collector import Http, fetch_body, fetch_listing, parse_regions
from .config import Config, PolicyConfig
from .db import (
    bump_metric,
    connect,
    init_db,
    row,
    rows,
    set_setting,
    setting,
    tx,
)
from .evidence import Evidence, Verdict, evaluate, evidence_from_observations
from .util import (
    iso,
    json_loads,
    new_id,
    parse_iso,
    plus_seconds,
    sha256_text,
    stable_json,
    utcnow,
    valid_item_id,
)

# Per-region body-fetch budgets. The catalog sweep is cheap; reading every
# article on every sweep is not, so content checking is sampled and rotated.
BODY_BUDGET_PER_SCAN = 80
BODY_BUDGET_FOR_LISTED = 18
BODY_REFRESH_PER_SCAN = 6


def _budgets(cfg) -> tuple[int, int, int]:
    listed = max(0, getattr(cfg, "body_budget", BODY_BUDGET_FOR_LISTED))
    refresh = max(0, listed // 3)
    absent = max(0, int(getattr(cfg, "body_budget_absent", BODY_BUDGET_PER_SCAN)))
    return listed, refresh, absent
NOTICE_SOURCES = ("rights-office", "publisher-notice-board", "legal-notice", "publisher-api")
TERMINAL_NOTICE_SOURCES = ("rights-office", "legal-notice")


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@contextmanager
def _db(path) -> Iterator:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _scan_view(rec) -> dict:
    return {
        "id": rec["id"],
        "batch_id": rec["batch_id"],
        "region": rec["region"],
        "kind": rec["kind"],
        "started_at": rec["started_at"],
        "finished_at": rec["finished_at"],
        "status": rec["status"],
        "http_status": rec["http_status"],
        "listing_count": rec["listing_count"],
        "complete": bool(rec["complete"]),
        "anomaly": bool(rec["anomaly"]),
        "detail": json_loads(rec["detail_json"], {}),
        "note": rec["note"],
    }


class Ledger:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.scan_lock = threading.Lock()
        self.dispatch_lock = threading.Lock()
        init_db(cfg.db_path)
        self._ensure_metrics()

    # ------------------------------------------------------------------ helpers
    def _ensure_metrics(self) -> None:
        with _db(self.cfg.db_path) as conn, tx(conn):
            for key in ("scans_run", "scans_failed", "notices_ingested", "events_sent", "events_failed"):
                conn.execute("INSERT OR IGNORE INTO metrics(key,value) VALUES(?,0)", (key,))
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_scan_interval',?)", (str(self.cfg.auto_scan_interval),))
            # Schedule the first automatic sweep one interval from first launch,
            # not immediately: startup must never race an operator-triggered scan.
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('last_auto_scan_at',?)", (iso(),))

    def policy(self) -> PolicyConfig:
        return self.cfg.policy

    def _audit(self, conn, actor: str, action: str, item_id: str | None, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit(at,actor,action,item_id,detail_json) VALUES(?,?,?,?,?)",
            (iso(), actor, action, item_id, stable_json(detail)),
        )

    def _enqueue_outbox(self, conn, item: dict, event_type: str, reason: str, extra: dict | None = None) -> str | None:
        seq = int(item["version"])
        key = f"{item['id']}:{seq}:{event_type}"
        existing = row(conn, "SELECT id FROM outbox WHERE idempotency_key=?", (key,))
        if existing:
            return None
        payload = {
            "item_id": item["id"],
            "title": item["title"],
            "url": item["url"],
            "seq": seq,
            "event_type": event_type,
            "reason": reason,
            "excerpt": item.get("excerpt") or "",
            "at": iso(),
        }
        if extra:
            payload.update(extra)
        outbox_id = new_id("out")
        conn.execute(
            "INSERT INTO outbox(id,item_id,event_type,seq,payload_json,state,attempts,next_attempt_at,created_at,idempotency_key)"
            " VALUES(?,?,?,?,?, 'queued',0,?,?,?)",
            (outbox_id, item["id"], event_type, seq, stable_json(payload), iso(), iso(), key),
        )
        return outbox_id

    # ------------------------------------------------------------------ scanning
    def run_sweep(self, regions: Iterable[str] | str | None = None, mode: str = "manual", note: str = "") -> dict:
        if not self.scan_lock.acquire(blocking=False):
            raise ServiceError("已有扫描正在进行，请稍后重试", 409)
        try:
            return self._run_sweep(regions, mode, note)
        finally:
            self.scan_lock.release()

    def _run_sweep(self, regions, mode, note) -> dict:
        chosen = parse_regions(regions) if regions else tuple(self.cfg.default_regions)
        if not chosen:
            raise ServiceError("未指定有效地区")
        for region in chosen:
            if region not in self.cfg.default_regions and region not in ("us", "eu", "apac"):
                raise ServiceError(f"未知地区：{region}")
        batch_id = new_id("bat")
        with _db(self.cfg.db_path) as conn, tx(conn):
            conn.execute(
                "INSERT INTO batches(id,started_at,mode,regions_json,status,note) VALUES(?,?,?,?, 'running',?)",
                (batch_id, iso(), mode, stable_json(list(chosen)), note),
            )
            bump_metric(conn, "scans_run")

        http = Http(self.cfg.http_timeout)
        results = []
        for region in chosen:
            results.append(self._scan_region(batch_id, region, "catalog", http))

        recon = self._reconcile_batch(batch_id, chosen, results)
        status = "ok" if all(not r["anomaly"] for r in results) else "partial"
        with _db(self.cfg.db_path) as conn, tx(conn):
            conn.execute(
                "UPDATE batches SET finished_at=?, status=? WHERE id=?",
                (iso(), status, batch_id),
            )
        recon["batch_id"] = batch_id
        recon["regions"] = list(chosen)
        recon["scans"] = results
        recon["status"] = status
        recon["metrics"] = {
            "requests": http.requests,
            "bytes": http.bytes,
            "elapsed_ms": http.elapsed_ms,
        }
        # Publish to downstream promptly. Delivery failures are retried by the
        # outbox scheduler; they never affect the scan verdict.
        try:
            recon["delivery"] = self.dispatch_outbox(limit=200)
        except Exception:
            recon["delivery"] = {"sent": 0, "failed": 0, "error": "dispatcher unavailable"}
        return recon

    def _scan_region(self, batch_id: str, region: str, kind: str, http: Http) -> dict:
        scan_id = new_id("scn")
        started = iso()
        with _db(self.cfg.db_path) as conn, tx(conn):
            conn.execute(
                "INSERT INTO scans(id,batch_id,region,kind,started_at,status) VALUES(?,?,?,?,?, 'running')",
                (scan_id, batch_id, region, kind, started),
            )
            known = rows(conn, "SELECT id,title,url,category,state,last_seen_at,excerpt,last_content_hash FROM items ORDER BY id")

        listing = fetch_listing(http, self.cfg.site_url, region, page_size=self.cfg.page_size)
        if listing.region_header and listing.region_header != region:
            listing.ok = False
            listing.complete = False
            listing.anomaly = "region_mismatch"
            listing.detail["served_region"] = listing.region_header

        if not listing.ok or not listing.complete:
            anomaly = listing.anomaly or ("listing_not_complete" if not listing.complete else "unknown")
            reason_note = self._listing_problem_text(anomaly, listing)
            with _db(self.cfg.db_path) as conn, tx(conn):
                conn.execute(
                    "UPDATE scans SET finished_at=?, status='anomaly', http_status=?, listing_count=?, complete=0,"
                    " anomaly=1, detail_json=?, note=? WHERE id=?",
                    (iso(), listing.status, len(listing.entries), stable_json(listing.detail), reason_note, scan_id),
                )
                bump_metric(conn, "scans_failed")
                self._schedule_rescan(conn, region, reason_note, attempts_hint=1)
                self._audit(conn, "collector", "scan_anomaly", None, {"scan_id": scan_id, "region": region, "anomaly": anomaly, "detail": listing.detail})
            return {
                "id": scan_id,
                "region": region,
                "status": "anomaly",
                "anomaly": True,
                "complete": False,
                "listing_count": len(listing.entries),
                "detail": listing.detail,
                "note": reason_note,
                "http_status": listing.status,
            }

        listed_ids = {entry.item_id for entry in listing.entries}
        now = iso()
        observations = 0
        bodies_fetched = 0
        discovered = 0

        # ---------------------------------------------------------- persist listing
        with _db(self.cfg.db_path) as conn, tx(conn):
            conn.execute(
                "UPDATE scans SET finished_at=?, status='ok', http_status=?, listing_count=?, complete=1,"
                " anomaly=0, detail_json=? WHERE id=?",
                (now, listing.status, len(listing.entries), stable_json(listing.detail), scan_id),
            )
            for entry in listing.entries:
                existing = row(conn, "SELECT * FROM items WHERE id=?", (entry.item_id,))
                if existing is None:
                    conn.execute(
                        "INSERT INTO items(id,title,url,category,state,state_basis,state_reason,state_changed_at,"
                        " review_needed,version,created_at,updated_at,last_seen_at,excerpt)"
                        " VALUES(?,?,?,?, 'active','bootstrap','首次完整扫描发现',?,0,0,?,?,?, '')",
                        (
                            entry.item_id,
                            entry.title,
                            self.cfg.site_url.rstrip("/") + entry.url,
                            entry.category,
                            now,
                            now,
                            now,
                            now,
                        ),
                    )
                    discovered += 1
                    bump_metric(conn, "items_discovered")
                else:
                    conn.execute(
                        "UPDATE items SET title=?, url=?, category=?, last_seen_at=?, updated_at=? WHERE id=?",
                        (
                            entry.title or existing["title"],
                            self.cfg.site_url.rstrip("/") + entry.url,
                            entry.category or existing["category"],
                            now,
                            now,
                            entry.item_id,
                        ),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO observations(scan_id,batch_id,item_id,region,observed_at,catalog,"
                    "listing_http,listing_complete,anomaly,anomaly_reason,body,body_checked_at,detail_json)"
                    " VALUES(?,?,?,?,?, 'present',?,1,0,'','not_checked',NULL,?)",
                    (scan_id, batch_id, entry.item_id, region, now, listing.status, stable_json({"revision": entry.revision})),
                )
                observations += 1

        # ---------------------------------------------------------- body sampling
        with _db(self.cfg.db_path) as conn:
            tracked = rows(conn, "SELECT * FROM items ORDER BY id")
        by_id = {rec["id"]: rec for rec in tracked}

        listed_targets = []
        for entry in listing.entries:
            rec = by_id.get(entry.item_id)
            if rec is None:
                continue
            needs = (
                not rec["last_content_hash"]
                or (rec["excerpt"] or "") == ""
                or entry.revision is not None
                and rec["last_seen_at"] == now
                and rec["state"] != "active"
            )
            if needs or rec["review_needed"]:
                listed_targets.append(rec)
        # Give brand-new items and reviews priority, then refresh a rotating
        # slice of known items so content changes and truncation are detected.
        listed_targets.sort(key=lambda rec: (0 if not rec["last_content_hash"] else 1, rec["id"]))
        budget_listed, budget_refresh, budget_absent = _budgets(self.cfg)
        listed_targets = listed_targets[:budget_listed]
        already = {rec["id"] for rec in listed_targets}
        with _db(self.cfg.db_path) as conn:
            refresh_pool = [
                dict(r)
                for r in rows(
                    conn,
                    "SELECT * FROM items WHERE state IN ('active','quarantined') AND id IN"
                    " (SELECT item_id FROM observations WHERE scan_id=? AND catalog='present') ORDER BY id",
                    (scan_id,),
                )
            ]
        rotate = (int(scan_id.rsplit("_", 1)[-1], 16) if scan_id.rsplit("_", 1)[-1] else 0) % max(1, len(refresh_pool) or 1)
        ordered_pool = refresh_pool[rotate:] + refresh_pool[:rotate]
        for rec in ordered_pool:
            if len(listed_targets) >= budget_listed + budget_refresh:
                break
            if rec["id"] not in already:
                listed_targets.append(rec)
                already.add(rec["id"])

        absent_targets = [
            rec
            for rec in tracked
            if rec["id"] not in listed_ids and rec["state"] in ("active", "quarantined", "retracted")
        ]
        absent_targets = absent_targets[:budget_absent]

        for rec in listed_targets:
            body = fetch_body(http, self.cfg.site_url, rec["id"], region, min_chars=self.cfg.policy.min_body_bytes)
            bodies_fetched += 1
            body_state, anomaly, hashes = self._body_outcome(body)
            excerpt = body.excerpt or rec["excerpt"] or ""
            with _db(self.cfg.db_path) as conn:
                conn.execute(
                    "UPDATE observations SET body=?, body_http=?, body_bytes=?, body_checked_at=?, content_hash=?,"
                    " excerpt=?, anomaly=?, anomaly_reason=?, detail_json=? WHERE scan_id=? AND item_id=?",
                    (
                        body_state,
                        body.http_status,
                        body.bytes,
                        iso(),
                        body.content_hash,
                        excerpt,
                        anomaly,
                        body.anomaly,
                        stable_json(body.detail),
                        scan_id,
                        rec["id"],
                    ),
                )
                if body_state == "ok" and body.content_hash:
                    if rec["last_content_hash"] != body.content_hash or (rec["excerpt"] or "") != excerpt:
                        conn.execute(
                            "UPDATE items SET excerpt=?, last_content_hash=?, updated_at=? WHERE id=?",
                            (excerpt, body.content_hash, iso(), rec["id"]),
                        )
                        conn.execute(
                            "INSERT INTO body_snapshots(item_id,content_hash,excerpt,bytes,region,fetched_at)"
                            " VALUES(?,?,?,?,?,?)",
                            (rec["id"], body.content_hash, excerpt, body.bytes, region, iso()),
                        )
                if body_state == "withdrawn":
                    conn.execute(
                        "UPDATE observations SET catalog='tombstone', listing_complete=1 WHERE scan_id=? AND item_id=?",
                        (scan_id, rec["id"]),
                    )

        for rec in absent_targets:
            body = fetch_body(http, self.cfg.site_url, rec["id"], region, min_chars=self.cfg.policy.min_body_bytes)
            bodies_fetched += 1
            body_state, anomaly, _ = self._body_outcome(body)
            catalog = "absent"
            if body_state == "withdrawn":
                catalog = "tombstone"
            elif body_state == "ok":
                anomaly = "catalog_absent_body_readable"
            elif body_state in ("not_found",):
                anomaly = ""
            elif body_state == "blocked":
                anomaly = "body_region_blocked"
            elif body_state == "error_page":
                anomaly = "body_error_page_200"
            elif body_state == "incomplete":
                anomaly = anomaly or "body_incomplete"
            elif body_state == "error":
                anomaly = anomaly or "body_error"
            with _db(self.cfg.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO observations(scan_id,batch_id,item_id,region,observed_at,catalog,"
                    "listing_http,listing_complete,anomaly,anomaly_reason,body,body_http,body_bytes,body_checked_at,"
                    "content_hash,excerpt,detail_json) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)",
                    (
                        scan_id,
                        batch_id,
                        rec["id"],
                        region,
                        now,
                        catalog,
                        listing.status,
                        1 if anomaly else 0,
                        anomaly,
                        body_state,
                        body.http_status,
                        body.bytes,
                        iso(),
                        body.content_hash,
                        body.excerpt,
                        stable_json(body.detail),
                    ),
                )

        # Record the population signal for this region vs the last healthy sweep.
        with _db(self.cfg.db_path) as conn:
            prev = row(
                conn,
                "SELECT listing_count FROM scans WHERE region=? AND complete=1 AND id<>? AND listing_count IS NOT NULL"
                " ORDER BY finished_at DESC LIMIT 1",
                (region, scan_id),
            )
            previous_total = int(prev["listing_count"]) if prev and prev["listing_count"] is not None else None
        ratio = None
        if previous_total and previous_total > 0:
            ratio = round(len(listing.entries) / previous_total, 4)
        with _db(self.cfg.db_path) as conn, tx(conn):
            detail = dict(listing.detail)
            detail["previous_total"] = previous_total
            detail["population_ratio"] = ratio
            detail["bodies_fetched"] = bodies_fetched
            detail["discovered"] = discovered
            conn.execute("UPDATE scans SET detail_json=? WHERE id=?", (stable_json(detail), scan_id))

        return {
            "id": scan_id,
            "region": region,
            "status": "ok",
            "anomaly": False,
            "complete": True,
            "listing_count": len(listing.entries),
            "detail": {**listing.detail, "bodies_fetched": bodies_fetched, "discovered": discovered},
            "note": "",
            "http_status": listing.status,
        }

    @staticmethod
    def _body_outcome(body) -> tuple[str, str, dict]:
        if body.status == "ok":
            return "ok", "", body.detail
        if body.status == "withdrawn":
            return "tombstone", "", body.detail
        if body.status == "not_found":
            return "not_found", "", body.detail
        if body.status == "blocked":
            return "blocked", "body_region_blocked", body.detail
        if body.status == "error_page":
            return "error_page", "body_error_page_200", body.detail
        if body.status == "incomplete":
            return "incomplete", body.anomaly or "body_incomplete", body.detail
        if body.status == "error":
            return "error", body.anomaly or "body_error", body.detail
        return "not_checked", body.anomaly or "", body.detail

    def _listing_problem_text(self, anomaly: str, listing) -> str:
        return {
            "region_blocked": f"地区 {listing.region_header or ''} 目录被屏蔽，本次不产生删除证据",
            "error_page_200": "目录返回 200 但内容是错误提示页，本次作废",
            "missing_markers": "目录结构不完整（缺少边界标记），本次作废",
            "count_mismatch": "目录条数与声明总数不一致（可能截断），本次作废",
            "empty_catalog": "目录为空或异常收缩，本次不产生删除证据",
            "pagination_loop": "分页重复，可能缓存错误，本次作废",
            "server_error": "目录服务 5xx，本次作废",
            "transport_error": "网络不可达，本次作废",
            "region_mismatch": "响应地区与请求地区不一致（错误地区缓存），本次作废",
        }.get(anomaly, f"扫描异常：{anomaly}")

    # ------------------------------------------------------------------ reconcile
    def _reconcile_batch(self, batch_id: str, regions: tuple[str, ...], results: list[dict]) -> dict:
        with _db(self.cfg.db_path) as conn:
            items = rows(conn, "SELECT * FROM items ORDER BY id")
            scans = {rec["id"]: dict(rec) for rec in rows(conn, "SELECT * FROM scans")}
            batch_scans = rows(conn, "SELECT * FROM scans WHERE batch_id=?", (batch_id,))
        population = self._population_signal(batch_scans)
        changed = {"retracted": 0, "quarantined": 0, "restored": 0, "kept": 0, "review": 0}
        decisions = []

        for rec in items:
            item = dict(rec)
            with _db(self.cfg.db_path) as conn:
                obs = [dict(r) for r in rows(conn, "SELECT * FROM observations WHERE item_id=? ORDER BY observed_at DESC, id DESC LIMIT 24", (item["id"],))]
                notice_rows = rows(conn, "SELECT * FROM notices WHERE item_id=?", (item["id"],))
                batch_ids = sorted({str(o.get("batch_id") or "") for o in obs if o.get("batch_id")})
                batch_scans: dict[str, list[dict]] = {}
                for batch_id in batch_ids:
                    batch_scans[batch_id] = [dict(r) for r in rows(conn, "SELECT * FROM scans WHERE batch_id=?", (batch_id,))]
            evidence = evidence_from_observations(
                item["id"], obs, scans, self.cfg.policy, population=population, batch_scans=batch_scans
            )
            verdict = evaluate(item, evidence, self.cfg.policy, notice_count=len(notice_rows))
            outcome = self._apply_verdict(item, evidence, verdict, len(notice_rows))
            if outcome.get("changed"):
                changed[outcome["state"]] = changed.get(outcome["state"], 0) + 1
            if verdict.decision == "quarantine" and item.get("review_needed"):
                changed["review"] += 1
            decisions.append({"item_id": item["id"], **outcome, "verdict": verdict.to_dict()})
        return {"changed": changed, "items": len(items), "decisions": decisions}

    @staticmethod
    def _population_signal(batch_scans) -> dict:
        ratios = []
        for rec in batch_scans:
            detail = json_loads(rec["detail_json"], {})
            ratio = detail.get("population_ratio")
            if ratio is not None:
                ratios.append(float(ratio))
        if not ratios:
            return {"ratio": None, "note": ""}
        low = min(ratios)
        return {
            "ratio": low,
            "note": f"本批目录规模相对上次完整扫描为 {low:.0%}；低于 60% 时提高删除证据门槛",
        }

    def _apply_verdict(self, item: dict, evidence: Evidence, verdict: Verdict, notice_count: int) -> dict:
        now = iso()
        current = item["state"]
        retracted = current == "retracted"
        decision = verdict.decision
        changed = False
        new_state = current
        reason = verdict.reason
        review_needed = int(item.get("review_needed") or 0)
        review_note = item.get("review_note") or ""
        outbox_event = None
        outbox_reason = ""
        extra = {}
        basis = item.get("state_basis") or ""

        if decision == "retract":
            if not retracted:
                new_state = "retracted"
                changed = True
                basis = "source_notice" if notice_count else "auto_evidence"
                outbox_event = "retract"
                outbox_reason = "发布方撤回通知" if notice_count else "多次完整扫描跨地区确认目录移除"
                extra = {"allow_restore": False}
            if notice_count and evidence.present_complete_scans:
                review_needed = 1
                review_note = "撤回通知已生效，但后续扫描仍能看到内容；已保持撤回，不自动重新发布，请人工核实。"
                if not retracted:
                    changed = True
        elif decision == "hold":
            if current == "active":
                new_state = "quarantined"
                changed = True
                basis = "manual_hold"
                outbox_event = "withhold"
                outbox_reason = item.get("hold_reason") or "运营人工冻结"
        elif decision == "restore":
            if current == "quarantined":
                new_state = "active"
                changed = True
                basis = "scan"
                outbox_event = "publish"
                outbox_reason = "重新在完整扫描中出现"
            elif current == "retracted":
                # Sticky by design: never auto-republish a retracted item.
                review_needed = 1
                review_note = "已撤回资料重新出现在扫描中；按策略不自动重新发布，需运营人工恢复。"
                changed = review_note != (item.get("review_note") or "")
                review_needed = 1
        elif decision == "quarantine":
            if current == "active":
                new_state = "quarantined"
                changed = True
                basis = "scan_ambiguous"
                outbox_event = "withhold"
                outbox_reason = verdict.reason
            elif current == "quarantined":
                basis = "scan_ambiguous"
        elif decision == "keep":
            if current == "active":
                basis = "scan"
            elif current == "quarantined" and evidence.present_complete_scans and not verdict.missing:
                new_state = "active"
                changed = True
                basis = "scan"
                outbox_event = "publish"
                outbox_reason = "证据恢复"
        if retracted and new_state != "retracted":
            new_state = "retracted"

        # Content sync: keep the downstream catalog current without touching the
        # withdrawal state machine. Only a complete, readable body updates
        # last_content_hash, so truncated/error pages can never trigger this.
        sync_event = ""
        if not outbox_event and new_state == "active" and not retracted:
            current_hash = item.get("last_content_hash") or ""
            if current_hash and item.get("published_hash") != current_hash:
                sync_event = "update" if item.get("published_hash") else "publish"
        sync_reason = "首次完整抓取后发布" if sync_event == "publish" else "正文内容同步"

        with _db(self.cfg.db_path) as conn, tx(conn):
            new_version = int(item["version"])
            if changed:
                new_version += 1
                conn.execute(
                    "UPDATE items SET state=?, state_basis=?, state_reason=?, state_changed_at=?, retracted_at=?,"
                    " review_needed=?, review_note=?, version=?, updated_at=? WHERE id=?",
                    (
                        new_state,
                        basis,
                        reason,
                        now,
                        (item.get("retracted_at") or now) if new_state == "retracted" else item.get("retracted_at"),
                        review_needed,
                        review_note,
                        new_version,
                        now,
                        item["id"],
                    ),
                )
                refreshed = dict(item)
                refreshed.update({"state": new_state, "version": new_version, "excerpt": item.get("excerpt") or ""})
                if outbox_event:
                    self._enqueue_outbox(conn, refreshed, outbox_event, outbox_reason, extra)
            elif sync_event:
                new_version += 1
                conn.execute(
                    "UPDATE items SET version=?, updated_at=? WHERE id=?",
                    (new_version, now, item["id"]),
                )
                refreshed = dict(item)
                refreshed.update({"version": new_version, "excerpt": item.get("excerpt") or ""})
                self._enqueue_outbox(conn, refreshed, sync_event, sync_reason)
            elif review_needed != int(item.get("review_needed") or 0) or review_note != (item.get("review_note") or ""):
                conn.execute(
                    "UPDATE items SET review_needed=?, review_note=?, updated_at=? WHERE id=?",
                    (review_needed, review_note, now, item["id"]),
                )
                changed = True
            conn.execute(
                "INSERT INTO evidence_decisions(item_id,at,decision,basis,evidence_json,policy_json,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    item["id"],
                    now,
                    decision,
                    reason,
                    stable_json(evidence.to_dict()),
                    stable_json(self.cfg.policy.describe()),
                    ",".join(verdict.missing),
                ),
            )
            if outbox_event:
                bump_metric(conn, "state_transitions")
        return {
            "changed": changed,
            "state": new_state,
            "previous_state": current,
            "decision": decision,
            "reason": reason,
            "review_needed": bool(review_needed),
            "evidence": evidence.to_dict(),
        }

    # ------------------------------------------------------------------ notices
    def ingest_notice(self, payload: dict, actor: str = "notice-api") -> dict:
        item_id = str(payload.get("item_id") or "").strip()
        source = str(payload.get("source") or "").strip()
        statement = str(payload.get("statement") or "").strip()
        notice_id = str(payload.get("notice_id") or "").strip() or new_id("ntc")
        if not valid_item_id(item_id):
            raise ServiceError("item_id 无效")
        if not source:
            raise ServiceError("通知必须带明确来源 source")
        if source not in NOTICE_SOURCES:
            raise ServiceError(f"来源不在允许列表：{', '.join(NOTICE_SOURCES)}")
        with _db(self.cfg.db_path) as conn, tx(conn):
            item = row(conn, "SELECT * FROM items WHERE id=?", (item_id,))
            if item is None:
                raise ServiceError("资料不存在，无法登记撤回通知", 404)
            existing = row(conn, "SELECT * FROM notices WHERE notice_id=?", (notice_id,))
            if existing:
                return {"ok": True, "duplicate": True, "notice_id": notice_id, "item_id": item_id}
            payload_hash = sha256_text(stable_json({k: payload.get(k) for k in ("item_id", "source", "statement")}))
            conn.execute(
                "INSERT INTO notices(notice_id,item_id,source,statement,effective_at,received_at,payload_hash,payload_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    notice_id,
                    item_id,
                    source,
                    statement,
                    payload.get("effective_at") or iso(),
                    iso(),
                    payload_hash,
                    stable_json(payload),
                ),
            )
            bump_metric(conn, "notices_ingested")
            item_d = dict(item)
            if item_d["state"] != "retracted":
                new_version = int(item_d["version"]) + 1
                conn.execute(
                    "UPDATE items SET state='retracted', state_basis='source_notice', state_reason=?,"
                    " state_changed_at=?, retracted_at=?, version=?, updated_at=?, review_needed=?, review_note=? WHERE id=?",
                    (
                        f"来源 {source} 的撤回通知",
                        iso(),
                        iso(),
                        new_version,
                        iso(),
                        0,
                        "",
                        item_id,
                    ),
                )
                item_d.update({"state": "retracted", "version": new_version})
                self._enqueue_outbox(
                    conn,
                    item_d,
                    "retract",
                    f"来源 {source} 的撤回通知",
                    {"notice_id": notice_id, "allow_restore": False},
                )
            else:
                # A second notice on an already-retracted item is a no-op but
                # still recorded, so downstream cannot be republished by a
                # later stale scan event.
                conn.execute(
                    "UPDATE items SET review_needed=1, review_note=? WHERE id=?",
                    ("收到重复撤回通知，保持撤回状态。", item_id),
                )
            self._audit(conn, actor, "notice_ingested", item_id, {"notice_id": notice_id, "source": source})
        # A withdrawal notice must reach the downstream promptly; the outbox
        # scheduler would otherwise delay it by one tick.
        try:
            self.dispatch_outbox(limit=20)
        except Exception:
            pass
        return {"ok": True, "duplicate": False, "notice_id": notice_id, "item_id": item_id}

    # ------------------------------------------------------------------ tasks
    def _schedule_rescan(self, conn, region: str, reason: str, attempts_hint: int = 1) -> None:
        key = f"rescan:{region}:{utcnow().strftime('%Y%m%d%H')}"
        existing = row(conn, "SELECT * FROM tasks WHERE dedupe_key=?", (key,))
        if existing:
            if existing["state"] in ("done", "cancelled"):
                conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (plus_seconds(self.cfg.task_retry_seconds), existing["id"]))
            return
        conn.execute(
            "INSERT INTO tasks(id,kind,region,state,attempts,next_run_at,created_at,updated_at,last_error,payload_json,dedupe_key)"
            " VALUES(?,?,?, 'queued',0,?,?,?,?,?,?)",
            (
                new_id("tsk"),
                "rescan_region",
                region,
                plus_seconds(self.cfg.task_retry_seconds),
                iso(),
                iso(),
                reason,
                stable_json({"region": region, "reason": reason}),
                key,
            ),
        )

    def queue_task(self, kind: str, item_id: str | None = None, region: str | None = None, note: str = "") -> dict:
        if kind not in ("body_probe", "rescan_region", "full_rescan"):
            raise ServiceError("未知任务类型")
        if item_id and not valid_item_id(item_id):
            raise ServiceError("item_id 无效")
        with _db(self.cfg.db_path) as conn, tx(conn):
            if item_id:
                exists = row(conn, "SELECT id FROM items WHERE id=?", (item_id,))
                if not exists:
                    raise ServiceError("资料不存在", 404)
            task_id = new_id("tsk")
            conn.execute(
                "INSERT INTO tasks(id,kind,item_id,region,state,attempts,next_run_at,created_at,updated_at,last_error,payload_json)"
                " VALUES(?,?,?,?, 'queued',0,?,?,?,?,?)",
                (task_id, kind, item_id, region, iso(), iso(), iso(), note, stable_json({"note": note})),
            )
            self._audit(conn, "operator", "task_queued", item_id, {"kind": kind, "region": region, "task": task_id})
        return {"ok": True, "task_id": task_id}

    def run_due_tasks(self, limit: int = 3) -> dict:
        now = iso()
        with _db(self.cfg.db_path) as conn:
            due = rows(conn, "SELECT * FROM tasks WHERE state='queued' AND next_run_at<=? ORDER BY next_run_at LIMIT ?", (now, limit))
        results = []
        for task in due:
            tid = task["id"]
            with _db(self.cfg.db_path) as conn, tx(conn):
                conn.execute("UPDATE tasks SET state='running', attempts=attempts+1, updated_at=? WHERE id=?", (iso(), tid))
            try:
                if task["kind"] in ("rescan_region", "full_rescan"):
                    regions = [task["region"]] if task["region"] else list(self.cfg.default_regions)
                    res = self.run_sweep(regions, mode="rescan", note=f"补扫任务 {tid}")
                    status, error = "done", ""
                    if res.get("status") != "ok":
                        status = "failed"
                        error = "补扫仍存在异常：" + ", ".join(
                            f"{s['region']}={s['detail'].get('anomaly') or s['status']}" for s in res.get("scans", []) if s.get("anomaly")
                        )
                elif task["kind"] == "body_probe":
                    res = self.probe_item(task["item_id"], task["region"] or (self.cfg.default_regions[0] if self.cfg.default_regions else "us"))
                    status, error = "done", ""
                else:
                    raise ServiceError("不支持的任务")
                with _db(self.cfg.db_path) as conn, tx(conn):
                    if status == "done":
                        conn.execute("UPDATE tasks SET state='done', updated_at=?, last_error='' WHERE id=?", (iso(), tid))
                    else:
                        retry = plus_seconds(min(3600, self.cfg.task_retry_seconds * max(1, int(task["attempts"]) + 1)))
                        final = "failed" if int(task["attempts"]) + 1 >= 5 else "queued"
                        conn.execute(
                            "UPDATE tasks SET state=?, next_run_at=?, updated_at=?, last_error=? WHERE id=?",
                            (final, retry, iso(), error[:300], tid),
                        )
                results.append({"task_id": tid, "kind": task["kind"], "status": status, "error": error})
            except Exception as exc:  # task failures must never kill the runner
                with _db(self.cfg.db_path) as conn, tx(conn):
                    retry = plus_seconds(min(3600, self.cfg.task_retry_seconds * max(1, int(task["attempts"]) + 1)))
                    final = "failed" if int(task["attempts"]) + 1 >= 5 else "queued"
                    conn.execute(
                        "UPDATE tasks SET state=?, next_run_at=?, updated_at=?, last_error=? WHERE id=?",
                        (final, retry, iso(), str(exc)[:300], tid),
                    )
                results.append({"task_id": tid, "kind": task["kind"], "status": "failed", "error": str(exc)[:300]})
        return {"ran": len(results), "results": results}

    def probe_item(self, item_id: str, region: str) -> dict:
        http = Http(self.cfg.http_timeout)
        body = fetch_body(http, self.cfg.site_url, item_id, region, min_chars=self.cfg.policy.min_body_bytes)
        with _db(self.cfg.db_path) as conn, tx(conn):
            self._audit(conn, "task-runner", "body_probe", item_id, {"region": region, "body": body.status, "anomaly": body.anomaly})
        return {"item_id": item_id, "region": region, "body": body.status, "anomaly": body.anomaly, "excerpt": body.excerpt}

    # ------------------------------------------------------------------ operator
    def set_hold(self, item_id: str, held: bool, reason: str) -> dict:
        with _db(self.cfg.db_path) as conn, tx(conn):
            item = row(conn, "SELECT * FROM items WHERE id=?", (item_id,))
            if item is None:
                raise ServiceError("资料不存在", 404)
            item_d = dict(item)
            if held:
                conn.execute(
                    "UPDATE items SET manual_hold=1, hold_reason=?, state='quarantined', state_basis='manual_hold',"
                    " state_reason=?, state_changed_at=?, updated_at=? WHERE id=?",
                    (reason or "运营人工冻结", reason or "运营人工冻结", iso(), iso(), item_id),
                )
                conn.execute("INSERT INTO holds(item_id,reason,created_at,created_by) VALUES(?,?,?,?)", (item_id, reason, iso(), "operator"))
                new_version = int(item_d["version"]) + 1
                conn.execute("UPDATE items SET version=? WHERE id=?", (new_version, item_id))
                item_d.update({"state": "quarantined", "version": new_version})
                self._enqueue_outbox(conn, item_d, "withhold", reason or "运营人工冻结", {"hold": True})
            else:
                conn.execute(
                    "UPDATE items SET manual_hold=0, hold_reason='', updated_at=? WHERE id=?",
                    (iso(), item_id),
                )
                conn.execute("UPDATE holds SET released_at=? WHERE item_id=? AND released_at IS NULL", (iso(), item_id))
            self._audit(conn, "operator", "hold_set" if held else "hold_released", item_id, {"reason": reason})
        try:
            self.dispatch_outbox(limit=20)
        except Exception:
            pass
        return {"ok": True, "item_id": item_id, "held": held}

    def restore_item(self, item_id: str, reason: str, publish: bool = True) -> dict:
        if not reason.strip():
            raise ServiceError("人工恢复必须填写原因")
        with _db(self.cfg.db_path) as conn, tx(conn):
            item = row(conn, "SELECT * FROM items WHERE id=?", (item_id,))
            if item is None:
                raise ServiceError("资料不存在", 404)
            item_d = dict(item)
            new_version = int(item_d["version"]) + 1
            conn.execute(
                "UPDATE items SET state='active', state_basis='operator_restore', state_reason=?, state_changed_at=?,"
                " restored_at=?, retracted_at=NULL, review_needed=0, review_note='', manual_hold=0, version=?, updated_at=? WHERE id=?",
                (reason, iso(), iso(), new_version, iso(), item_id),
            )
            item_d.update({"state": "active", "version": new_version})
            if publish:
                self._enqueue_outbox(conn, item_d, "publish", f"运营人工恢复：{reason}", {"allow_restore": True})
            self._audit(conn, "operator", "operator_restore", item_id, {"reason": reason, "publish": publish})
        # Operator intent is explicit: deliver now rather than on the next tick.
        if publish:
            try:
                self.dispatch_outbox(limit=20)
            except Exception:
                pass
        return {"ok": True, "item_id": item_id, "state": "active"}

    def review_item(self, item_id: str, note: str) -> dict:
        with _db(self.cfg.db_path) as conn, tx(conn):
            item = row(conn, "SELECT id FROM items WHERE id=?", (item_id,))
            if item is None:
                raise ServiceError("资料不存在", 404)
            conn.execute("UPDATE items SET review_needed=1, review_note=?, updated_at=? WHERE id=?", (note[:500], iso(), item_id))
            self._audit(conn, "operator", "review_flagged", item_id, {"note": note[:500]})
        return {"ok": True}

    # ------------------------------------------------------------------ outbox
    def dispatch_outbox(self, limit: int = 20) -> dict:
        if not self.dispatch_lock.acquire(blocking=False):
            return {"sent": 0, "failed": 0, "skipped": "dispatcher busy"}
        try:
            return self._dispatch_outbox(limit)
        finally:
            self.dispatch_lock.release()

    def _dispatch_outbox(self, limit: int) -> dict:
        now = iso()
        with _db(self.cfg.db_path) as conn:
            due = rows(conn, "SELECT * FROM outbox WHERE state IN ('queued','inflight') AND next_attempt_at<=? ORDER BY created_at LIMIT ?", (now, limit))
        sent = failed = 0
        for rec in due:
            with _db(self.cfg.db_path) as conn, tx(conn):
                conn.execute("UPDATE outbox SET state='inflight' WHERE id=?", (rec["id"],))
            payload = json_loads(rec["payload_json"], {})
            ok, status, err = self._post_downstream(payload)
            with _db(self.cfg.db_path) as conn, tx(conn):
                if ok:
                    conn.execute(
                        "UPDATE outbox SET state='delivered', attempts=attempts+1, delivered_at=?, last_error='' WHERE id=?",
                        (iso(), rec["id"]),
                    )
                    conn.execute(
                        "UPDATE items SET published_hash=last_content_hash WHERE id=?",
                        (rec["item_id"],),
                    )
                    bump_metric(conn, "events_sent")
                    sent += 1
                else:
                    attempts = int(rec["attempts"]) + 1
                    delay = min(3600, 5 * (2 ** min(attempts, 8)))
                    state = "dead" if attempts >= 10 else "queued"
                    conn.execute(
                        "UPDATE outbox SET state=?, attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                        (state, attempts, plus_seconds(delay), f"{status}: {err}"[:300], rec["id"]),
                    )
                    bump_metric(conn, "events_failed")
                    failed += 1
            time.sleep(0.01)
        return {"sent": sent, "failed": failed, "considered": len(due)}

    def _post_downstream(self, payload: dict) -> tuple[bool, int, str]:
        url = f"{self.cfg.downstream_url.rstrip('/')}/internal/events"
        data = stable_json(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Downstream-Token": self.cfg.downstream_token,
                "X-Idempotency-Key": f"{payload.get('item_id')}:{payload.get('seq')}:{payload.get('event_type')}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read(4000).decode("utf-8", errors="replace")
                return resp.status in (200, 201), int(resp.status), body[:200]
        except urllib.error.HTTPError as exc:
            return False, int(exc.code), exc.read(400).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, 0, str(exc)[:200]

    # ------------------------------------------------------------------ queries
    def overview(self) -> dict:
        with _db(self.cfg.db_path) as conn:
            counts = {r["state"]: int(r["n"]) for r in rows(conn, "SELECT state, COUNT(*) n FROM items GROUP BY state")}
            total = sum(counts.values())
            review = int(row(conn, "SELECT COUNT(*) n FROM items WHERE review_needed=1")["n"])
            notices = int(row(conn, "SELECT COUNT(*) n FROM notices")["n"])
            scans = [dict(r) for r in rows(conn, "SELECT * FROM scans ORDER BY started_at DESC LIMIT 60")]
            batches = [dict(r) for r in rows(conn, "SELECT * FROM batches ORDER BY started_at DESC LIMIT 12")]
            tasks = [dict(r) for r in rows(conn, "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 40")]
            outbox_rows = [dict(r) for r in rows(conn, "SELECT * FROM outbox ORDER BY created_at DESC LIMIT 80")]
            decisions = [dict(r) for r in rows(conn, "SELECT * FROM evidence_decisions ORDER BY at DESC LIMIT 200")]
            items = [dict(r) for r in rows(conn, "SELECT * FROM items ORDER BY state_changed_at DESC LIMIT 100")]
            policy = self.cfg.policy
            alert_rows = rows(
                conn,
                "SELECT item_id, MAX(at) at FROM evidence_decisions WHERE decision='quarantine' AND at > ? GROUP BY item_id",
                (plus_seconds(-86400 * 3, utcnow()),),
            )
            last_auto = setting(conn, "last_auto_scan_at", "")
        task_counts = {}
        for task in tasks:
            task_counts[task["state"]] = task_counts.get(task["state"], 0) + 1
        outbox_counts = {}
        for ev in outbox_rows:
            outbox_counts[ev["state"]] = outbox_counts.get(ev["state"], 0) + 1
        recent_scans = []
        for scan in scans[:12]:
            recent_scans.append(
                {
                    "id": scan["id"],
                    "batch_id": scan["batch_id"],
                    "region": scan["region"],
                    "started_at": scan["started_at"],
                    "finished_at": scan["finished_at"],
                    "status": scan["status"],
                    "complete": bool(scan["complete"]),
                    "listing_count": scan["listing_count"],
                    "detail": json_loads(scan["detail_json"], {}),
                }
            )
        status_by_region: dict[str, dict] = {}
        for scan in scans:
            if scan["region"] in status_by_region:
                continue
            status_by_region[scan["region"]] = {
                "status": scan["status"],
                "at": scan["started_at"],
                "complete": bool(scan["complete"]),
                "count": scan["listing_count"],
            }
        return {
            "counts": {
                "total": total,
                "active": counts.get("active", 0),
                "quarantined": counts.get("quarantined", 0),
                "retracted": counts.get("retracted", 0),
                "review": review,
                "notices": notices,
            },
            "policy": policy.describe(),
            "regions": {
                "configured": list(self.cfg.default_regions),
                "site_url": self.cfg.site_url,
                "downstream_url": self.cfg.downstream_url,
                "latest": status_by_region,
            },
            "scans": recent_scans,
            "batches": batches,
            "tasks": tasks[:12],
            "task_counts": task_counts,
            "outbox": outbox_rows[:12],
            "outbox_counts": outbox_counts,
            "decisions": decisions[:30],
            "recent_items": items[:12],
            "alerts": len(alert_rows),
            "last_auto_scan_at": last_auto,
            "auto_scan_interval": self.cfg.auto_scan_interval,
            "now": iso(),
        }

    def maybe_auto_scan(self) -> dict | None:
        """Start a scheduled sweep when the interval has elapsed."""
        interval = int(self.cfg.auto_scan_interval or 0)
        if interval <= 0:
            return None
        with _db(self.cfg.db_path) as conn:
            last = setting(conn, "last_auto_scan_at", "")
        last_dt = parse_iso(last)
        if last_dt is None:
            # Clock starts now; never let startup trigger an immediate sweep that
            # could race an operator scan.
            with _db(self.cfg.db_path) as conn, tx(conn):
                set_setting(conn, "last_auto_scan_at", iso())
            return None
        elapsed = (utcnow() - last_dt).total_seconds()
        if elapsed < interval:
            return None
        try:
            result = self.run_sweep(list(self.cfg.default_regions), mode="auto", note="定时全量扫描")
        except ServiceError:
            return None
        with _db(self.cfg.db_path) as conn, tx(conn):
            set_setting(conn, "last_auto_scan_at", iso())
        return result

    def reset_demo(self) -> None:
        """Clear ledger state (seed/demo only)."""
        with _db(self.cfg.db_path) as conn, tx(conn):
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
                conn.execute(f"DELETE FROM {table}")
            self._audit(conn, "system", "demo_reset", None, {})

    def list_items(self, state: str = "", query: str = "", limit: int = 100) -> list[dict]:
        with _db(self.cfg.db_path) as conn:
            sql = "SELECT * FROM items"
            clauses, args = [], []
            if state and state in ("active", "quarantined", "retracted"):
                clauses.append("state=?")
                args.append(state)
            if query:
                clauses.append("(id LIKE ? OR title LIKE ?)")
                args.extend([f"%{query}%", f"%{query}%"])
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY CASE state WHEN 'retracted' THEN 0 WHEN 'quarantined' THEN 1 ELSE 2 END, state_changed_at DESC LIMIT ?"
            args.append(limit)
            records = [dict(r) for r in rows(conn, sql, args)]
            for rec in records:
                ev = row(
                    conn,
                    "SELECT * FROM evidence_decisions WHERE item_id=? ORDER BY at DESC LIMIT 1",
                    (rec["id"],),
                )
                rec["evidence"] = json_loads(ev["evidence_json"], {}) if ev else {}
                rec["requirement"] = self._requirement_view(json_loads(ev["evidence_json"], {})) if ev else {}
                rec["last_decision"] = ev["decision"] if ev else ""
                rec["last_reason"] = ev["basis"] if ev else ""
        return records

    def item_detail(self, item_id: str) -> dict:
        with _db(self.cfg.db_path) as conn:
            item = row(conn, "SELECT * FROM items WHERE id=?", (item_id,))
            if item is None:
                raise ServiceError("资料不存在", 404)
            observations = [
                dict(r)
                for r in rows(
                    conn,
                    "SELECT o.*, s.status scan_status, s.anomaly scan_anomaly FROM observations o"
                    " JOIN scans s ON s.id=o.scan_id WHERE o.item_id=? ORDER BY o.observed_at DESC, o.id DESC LIMIT 60",
                    (item_id,),
                )
            ]
            scans = {r["id"]: dict(r) for r in rows(conn, "SELECT * FROM scans WHERE id IN (SELECT scan_id FROM observations WHERE item_id=?)", (item_id,))}
            notices = [dict(r) for r in rows(conn, "SELECT * FROM notices WHERE item_id=? ORDER BY received_at DESC", (item_id,))]
            decisions = [dict(r) for r in rows(conn, "SELECT * FROM evidence_decisions WHERE item_id=? ORDER BY at DESC LIMIT 30", (item_id,))]
            tasks = [dict(r) for r in rows(conn, "SELECT * FROM tasks WHERE item_id=? ORDER BY created_at DESC LIMIT 20", (item_id,))]
            outbox_rows = [dict(r) for r in rows(conn, "SELECT * FROM outbox WHERE item_id=? ORDER BY created_at DESC LIMIT 20", (item_id,))]
            snapshots = [dict(r) for r in rows(conn, "SELECT * FROM body_snapshots WHERE item_id=? ORDER BY fetched_at DESC LIMIT 10", (item_id,))]
            audit = [dict(r) for r in rows(conn, "SELECT * FROM audit WHERE item_id=? ORDER BY at DESC LIMIT 30", (item_id,))]
        latest_decision = decisions[0] if decisions else None
        evidence = json_loads(latest_decision["evidence_json"], {}) if latest_decision else {}
        policy = json_loads(latest_decision["policy_json"], {}) if latest_decision else self.cfg.policy.describe()
        per_scan = self._per_scan_states(observations)
        for rec in decisions:
            rec["evidence"] = json_loads(rec["evidence_json"], {})
            rec["policy"] = json_loads(rec["policy_json"], {})
        for rec in observations:
            rec["detail"] = json_loads(rec["detail_json"], {})
        return {
            "item": dict(item),
            "observations": observations,
            "per_scan": per_scan,
            "notices": notices,
            "decisions": decisions,
            "tasks": tasks,
            "outbox": [self._outbox_view(r) for r in outbox_rows],
            "snapshots": snapshots,
            "audit": audit,
            "evidence": evidence,
            "policy": policy,
            "requirement": self._requirement_view(evidence, policy),
            "downstream": self._downstream_item(item_id),
            "now": iso(),
        }

    def _per_scan_states(self, observations: list[dict]) -> list[dict]:
        grouped: dict[str, dict] = {}
        for obs in observations:
            sid = obs["scan_id"]
            entry = grouped.setdefault(
                sid,
                {
                    "scan_id": sid,
                    "batch_id": obs["batch_id"],
                    "region": obs["region"],
                    "observed_at": obs["observed_at"],
                    "catalog": obs["catalog"],
                    "body": obs["body"],
                    "anomaly": obs["anomaly_reason"] or "",
                },
            )
            if obs["catalog"] == "present":
                entry["catalog"] = "present"
            if obs["body"] == "ok":
                entry["body"] = "ok"
            if obs["anomaly_reason"]:
                entry["anomaly"] = obs["anomaly_reason"]
        return sorted(grouped.values(), key=lambda x: x["observed_at"], reverse=True)

    def _requirement_view(self, evidence: dict, policy: dict | None = None) -> dict:
        base = policy or self.cfg.policy.describe()
        anomaly = bool(evidence.get("anomaly_heavy")) or bool(int(evidence.get("ambiguity_scans") or 0))
        extra = int(base.get("anomaly_extra_scans") if anomaly else 0)
        scans_need = int(base.get("min_scans") or 3) + extra
        regions_need = int(base.get("min_regions") or 2)
        batches_need = int(base.get("min_scan_batches") or 2)
        return {
            "scans": {"have": int(evidence.get("absent_complete_scans") or 0), "need": scans_need},
            "regions": {"have": len(evidence.get("absent_regions") or []), "need": regions_need},
            "batches": {"have": len(evidence.get("absent_batches") or []), "need": batches_need},
            "presence": {"have": int(evidence.get("present_complete_scans") or 0)},
            "min_scans": scans_need,
            "min_regions": regions_need,
            "min_batches": batches_need,
            "ambiguity": {"have": int(evidence.get("ambiguity_scans") or 0)},
            "span_seconds": evidence.get("span_seconds") or 0,
            "anomaly_heavy": bool(evidence.get("anomaly_heavy")),
            "population_ratio": evidence.get("population_ratio"),
            "population_note": evidence.get("population_note") or "",
        }

    @staticmethod
    def _outbox_view(rec: dict) -> dict:
        return {
            "id": rec["id"],
            "item_id": rec["item_id"],
            "event_type": rec["event_type"],
            "seq": rec["seq"],
            "state": rec["state"],
            "attempts": rec["attempts"],
            "created_at": rec["created_at"],
            "delivered_at": rec["delivered_at"],
            "next_attempt_at": rec["next_attempt_at"],
            "last_error": rec["last_error"],
            "payload": json_loads(rec["payload_json"], {}),
        }

    def _downstream_item(self, item_id: str) -> dict | None:
        try:
            url = f"{self.cfg.downstream_url.rstrip('/')}/internal/items/{item_id}"
            req = urllib.request.Request(url, headers={"X-Downstream-Token": self.cfg.downstream_token})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read(100000).decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def list_tasks(self, limit: int = 100) -> list[dict]:
        with _db(self.cfg.db_path) as conn:
            return [dict(r) for r in rows(conn, "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    def list_outbox(self, limit: int = 100) -> list[dict]:
        with _db(self.cfg.db_path) as conn:
            return [self._outbox_view(dict(r)) for r in rows(conn, "SELECT * FROM outbox ORDER BY created_at DESC LIMIT ?", (limit,))]

    def list_audit(self, limit: int = 200) -> list[dict]:
        with _db(self.cfg.db_path) as conn:
            return [dict(r) for r in rows(conn, "SELECT * FROM audit ORDER BY at DESC LIMIT ?", (limit,))]

    def list_batches(self, limit: int = 50) -> list[dict]:
        with _db(self.cfg.db_path) as conn:
            batches = [dict(r) for r in rows(conn, "SELECT * FROM batches ORDER BY started_at DESC LIMIT ?", (limit,))]
            for batch in batches:
                batch["regions"] = json_loads(batch["regions_json"], [])
                batch["scans"] = [
                    _scan_view(rec) for rec in rows(conn, "SELECT * FROM scans WHERE batch_id=? ORDER BY region", (batch["id"],))
                ]
        return batches
