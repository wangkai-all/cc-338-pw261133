"""Scenario checks for the withdrawal-evidence policy.

Each check starts from a fresh harness and asserts an end-to-end outcome.
"""

from __future__ import annotations

import json
import time

from tests.harness import Harness
from tests.selfcheck import check


@check("首次采集建立基线，全部为在用")
def baseline(h: Harness) -> None:
    h.sweep()
    assert h.count("SELECT COUNT(*) FROM items") == 30, "应发现 30 条资料"
    assert h.state_of("itm_001") == "active"
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0


@check("单次空结果绝不删除")
def single_empty(h: Harness) -> None:
    h.sweep()
    h.set_item("itm_003", withdrawn=True)
    h.sweep()
    row = h.item_row("itm_003")
    assert row["state"] == "quarantined", "一次消失只能进入待复核，当前=" + row["state"]
    assert h.downstream_status("itm_003") == "withheld"
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0


@check("三次完整扫描、跨地区跨批次后自动撤回")
def full_evidence(h: Harness) -> None:
    h.sweeps(3)
    h.set_item("itm_003", withdrawn=True)
    h.sweeps(3)
    h.flush()
    row = h.item_row("itm_003")
    assert row["state"] == "retracted", "应自动撤回，当前=" + row["state"]
    assert row["state_basis"] == "auto_evidence"
    assert h.downstream_status("itm_003") == "withheld"


@check("目录被屏蔽（403/451）不构成删除证据")
def regional_block(h: Harness) -> None:
    h.sweeps(2)
    h.set_region("eu", "blocked403")
    h.sweeps(3)
    row = h.item_row("itm_005")
    assert row["state"] == "active", "地区屏蔽不应影响结论，当前=" + row["state"]
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0


@check("200 错误页与截断目录都不算完整扫描")
def fake_200_and_truncation(h: Harness) -> None:
    h.sweeps(2)
    h.set_faults(listing_error_200=True)
    h.sweeps(2)
    h.set_faults(listing_error_200=False, listing_truncate=True)
    for _ in range(4):
        h.sweep()
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0
    assert h.count("SELECT COUNT(*) FROM items WHERE state='active'") == 30


@check("正文按地区限制（451）只进入复核，不产生删除")
def per_page_region_block(h: Harness) -> None:
    h.sweeps(2)
    h.set_item("itm_007", catalog_hidden=["us", "eu", "apac"], page_blocked=["us", "eu", "apac"])
    h.sweeps(4)
    row = h.item_row("itm_007")
    assert row["state"] == "quarantined", "正文地区限制只能待复核，当前=" + row["state"]
    assert h.downstream_status("itm_007") == "withheld"


@check("部分地区暂时不可见时，其余地区仍可形成证据但它不会降低门槛")
def partial_region_blind(h: Harness) -> None:
    h.sweeps(2)
    h.set_region("eu", "blocked403")
    h.set_item("itm_016", withdrawn=True)
    h.sweeps(4)
    row = h.item_row("itm_016")
    # us + apac are clean absences, so the item can still reach the bar; the
    # eu blind spot shows up as an anomaly and extra required scans.
    detail = h.ledger.item_detail("itm_016")
    assert detail["evidence"]["ambiguity_scans"] >= 1, "eu 屏蔽应记为歧义"
    assert row["state"] in ("quarantined", "retracted")
    if row["state"] == "retracted":
        assert detail["requirement"]["scans"]["need"] >= 5, "存在歧义时必须提高门槛"
        assert len(detail["evidence"]["absent_regions"]) >= 2


@check("撤回通知先到、正文后到也不能重新发布")
def notice_before_body(h: Harness) -> None:
    h.sweep()
    h.ledger.ingest_notice(
        {"notice_id": "n1", "item_id": "itm_002", "source": "rights-office", "statement": "撤回"},
        actor="test",
    )
    h.flush()
    assert h.item_row("itm_002")["state"] == "retracted"
    assert h.downstream_status("itm_002") == "withheld"
    # Body is still readable; several healthy sweeps follow.
    h.sweeps(3)
    h.flush()
    assert h.item_row("itm_002")["state"] == "retracted", "撤回后不得因看到正文而恢复"
    assert h.downstream_status("itm_002") == "withheld"
    assert h.item_row("itm_002")["review_needed"] == 1
    # Even a publish event at the current sequence cannot restore it: only an
    # operator publish carrying allow_restore may do that.
    held = h.down.item("itm_002")
    result = h.down.apply_event(
        {"item_id": "itm_002", "event_type": "publish", "seq": held["seq"], "title": "x", "reason": "stale"}
    )
    assert result["outcome"] == "blocked_restore_requires_operator", result
    assert h.downstream_status("itm_002") == "withheld"


@check("撤回通知来自未登记来源时被拒绝")
def bad_notice_source(h: Harness) -> None:
    h.sweep()
    from ledger.service import ServiceError

    try:
        h.ledger.ingest_notice({"item_id": "itm_001", "source": "anonymous-tip", "statement": "x"})
    except ServiceError:
        pass
    else:
        raise AssertionError("未登记来源不应被接受")
    assert h.item_row("itm_001")["state"] == "active"


@check("地区中途改变不产生删除结论")
def region_change_midway(h: Harness) -> None:
    h.sweeps(2)
    h.set_faults(wrong_region=True)  # eu requests get us content
    h.sweeps(3)
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0
    h.set_faults(wrong_region=False)
    h.set_region("apac", "error200")
    h.sweeps(3)
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0


@check("一次扫描失败后补扫成功，结论只基于完整扫描")
def rescan_after_failure(h: Harness) -> None:
    h.sweeps(2)
    h.set_region("us", "blocked403")
    h.sweep(["us"])
    tasks = h.ledger.list_tasks()
    assert any(t["kind"] == "rescan_region" and t["region"] == "us" for t in tasks), "失败扫描应排队补扫"
    h.set_region("us", "ok")
    h.ledger.run_due_tasks(limit=5)
    assert h.count("SELECT COUNT(*) FROM items WHERE state='retracted'") == 0


@check("目录整体异常收缩时提高门槛，不造成批量误删")
def population_collapse(h: Harness) -> None:
    h.sweeps(2)
    for idx in range(1, 16):
        h.set_item(f"itm_{idx:03d}", catalog_hidden=["us", "eu", "apac"])
    h.sweeps(4)
    # Without the population guard this would be 15 retractions. At most the
    # baseline items that never reappear may be retracted after enough evidence;
    # the collapse itself must be treated as an anomaly.
    retracted = h.count("SELECT COUNT(*) FROM items WHERE state='retracted'")
    assert retracted <= 3, f"异常收缩不得批量撤回，实际 {retracted}"


@check("截断正文不登记为内容更新")
def truncated_body(h: Harness) -> None:
    h.sweeps(2)
    before = h.item_row("itm_001")["last_content_hash"]
    h.set_faults(body_truncate=True)
    h.sweep(["us"])
    after = h.item_row("itm_001")["last_content_hash"]
    assert before == after, "截断正文不应更新内容哈希"
    assert h.count("SELECT COUNT(*) FROM observations WHERE body='incomplete'") > 0


@check("撤回具有粘性：重现后需人工恢复")
def sticky_retraction(h: Harness) -> None:
    h.sweeps(3)
    h.set_item("itm_009", withdrawn=True)
    h.sweeps(3)
    assert h.item_row("itm_009")["state"] == "retracted"
    h.reset_item("itm_009")
    h.sweeps(2)
    row = h.item_row("itm_009")
    assert row["state"] == "retracted", "自动恢复被禁止，当前=" + row["state"]
    assert row["review_needed"] == 1
    h.ledger.restore_item("itm_009", "发布方书面恢复函，人工复核通过")
    h.flush()
    assert h.item_row("itm_009")["state"] == "active"
    assert h.downstream_status("itm_009") == "published"


@check("暂时不可见：待复核可随证据恢复重新发布")
def provisional_withhold_restores(h: Harness) -> None:
    h.sweep()
    h.flush()
    assert h.downstream_status("itm_015") == "published"
    # One sweep of absence quarantines; the downstream catalog is temporarily
    # more conservative (withheld) but not permanently restricted.
    h.set_item("itm_015", catalog_hidden=["us", "eu", "apac"])
    h.sweep()
    h.flush()
    assert h.item_row("itm_015")["state"] == "quarantined"
    held = h.down.item("itm_015")
    assert held["status"] == "withheld"
    assert held["permanently_restricted"] is False, held
    # It comes back: complete scans restore it and the downstream republishes.
    h.reset_item("itm_015")
    h.sweep()
    h.flush()
    assert h.item_row("itm_015")["state"] == "active"
    detail = h.down.item("itm_015")
    assert detail["status"] == "published", detail
    assert detail["permanently_restricted"] is False


@check("真实撤回永久受限：任何自动发布都被拒绝，只有人工恢复可解")
def retraction_stays_permanent_downstream(h: Harness) -> None:
    h.sweeps(3)
    h.set_item("itm_017", withdrawn=True)
    h.sweeps(3)
    h.flush()
    assert h.item_row("itm_017")["state"] == "retracted"
    held = h.down.item("itm_017")
    assert held["status"] == "withheld"
    assert held["permanently_restricted"] is True, held
    # Simulate a stale scan-driven publish at a newer seq: still refused.
    result = h.down.apply_event(
        {"event_id": "auto-publish-newer", "item_id": "itm_017", "event_type": "publish", "seq": held["seq"] + 5}
    )
    assert result["outcome"] == "blocked_restore_requires_operator", result
    # An operator restore carries allow_restore and is the only path back.
    h.ledger.restore_item("itm_017", "权利方书面恢复，人工复核")
    h.flush()
    detail = h.down.item("itm_017")
    assert detail["status"] == "published", detail
    assert detail["permanently_restricted"] is False, detail


@check("下游按序号拒绝陈旧事件")
def downstream_ordering(h: Harness) -> None:
    h.sweep()
    h.flush()
    # A healthy first sweep publishes the item to the downstream catalog.
    assert h.downstream_status("itm_001") == "published"
    h.ledger.set_hold("itm_001", True, "人工抽查")
    h.flush()
    held = h.down.item("itm_001")
    assert held["status"] == "withheld"
    seq_held = held["seq"]
    stale = h.down.apply_event(
        {"event_id": "stale-publish-1", "item_id": "itm_001", "event_type": "publish", "seq": seq_held - 1, "title": "old"}
    )
    assert stale["outcome"] == "stale_ignored", stale
    assert h.downstream_status("itm_001") == "withheld"
    # A stale content update is equally ignored and cannot lift the withhold.
    stale_update = h.down.apply_event(
        {"event_id": "stale-update-1", "item_id": "itm_001", "event_type": "update", "seq": seq_held - 1, "title": "old"}
    )
    assert stale_update["outcome"] == "stale_ignored", stale_update
    assert h.downstream_status("itm_001") == "withheld"


@check("证据与决策可追溯到具体扫描")
def evidence_traceability(h: Harness) -> None:
    h.sweeps(3)
    h.set_item("itm_010", withdrawn=True)
    h.sweeps(3)
    detail = h.ledger.item_detail("itm_010")
    assert detail["item"]["state"] == "retracted"
    assert detail["evidence"]["absent_complete_scans"] >= 3
    assert len(detail["evidence"]["absent_regions"]) >= 2
    assert len(detail["evidence"]["absent_batches"]) >= 2
    assert len(detail["decisions"]) >= 1
    assert detail["requirement"]["scans"]["need"] >= 3
    # Every scan id cited in evidence exists and is complete.
    conn = h._conn()
    try:
        for scan_id in detail["evidence"]["scan_ids"]:
            rec = conn.execute("SELECT complete FROM scans WHERE id=?", (scan_id,)).fetchone()
            assert rec is not None, "证据引用了不存在的扫描"
            assert rec["complete"] == 1, "证据引用了不完整扫描"
    finally:
        conn.close()


@check("重复事件幂等")
def idempotent_events(h: Harness) -> None:
    h.sweep()
    h.ledger.set_hold("itm_011", True, "人工抽查")
    h.flush()
    payload = {
        "item_id": "itm_011",
        "event_type": "withhold",
        "seq": h.item_row("itm_011")["version"],
        "reason": "dup",
    }
    first = h.down.apply_event({**payload, "event_id": "evt-fixed"})
    second = h.down.apply_event({**payload, "event_id": "evt-fixed"})
    assert first["outcome"] == "applied", first
    assert second["outcome"] == "duplicate", second


@check("撤回通知幂等且不会重复发布")
def notice_idempotent(h: Harness) -> None:
    h.sweep()
    n = {"notice_id": "same", "item_id": "itm_012", "source": "legal-notice", "statement": "撤回"}
    a = h.ledger.ingest_notice(n)
    b = h.ledger.ingest_notice(n)
    assert a["duplicate"] is False and b["duplicate"] is True
    assert h.count("SELECT COUNT(*) FROM outbox WHERE item_id='itm_012' AND event_type='retract'") == 1


@check("人工冻结优先于自动判定")
def manual_hold(h: Harness) -> None:
    h.sweeps(3)
    h.ledger.set_hold("itm_013", True, "法务复核中")
    h.sweeps(2)
    row = h.item_row("itm_013")
    assert row["state"] == "quarantined"
    assert row["manual_hold"] == 1
    # Held item present in scans must stay quarantined, not become active.
    assert h.downstream_status("itm_013") in (None, "withheld")


@check("容量与请求预算：完整扫描只读有限正文")
def body_budget(h: Harness) -> None:
    h.sweep()
    detail = h.ledger.list_batches(1)[0]
    scans = detail["scans"]
    total_bodies = sum((s.get("detail") or {}).get("bodies_fetched", 0) for s in scans)
    assert total_bodies <= 30 * 3, f"正文抓取超出预算：{total_bodies}"
    assert all(s["detail"].get("bodies_fetched", 0) <= 12 for s in scans), "单地区正文预算应受控"
