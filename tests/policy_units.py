"""Pure-function checks for the evidence policy (no servers, no clock)."""

from __future__ import annotations

from ledger.config import PolicyConfig
from ledger.evidence import Evidence, evaluate, evidence_from_observations
from tests.selfcheck import check

POLICY = PolicyConfig(min_scans=3, min_regions=2, min_scan_batches=2, anomaly_extra_scans=2)


def _scan(complete=True, anomaly=0, status="ok", started="2026-01-01T00:00:00Z"):
    return {"complete": 1 if complete else 0, "anomaly": anomaly, "status": status, "started_at": started, "batch_id": ""}


def _obs(scan_id, batch_id, region, catalog, *, anomaly="", at="2026-01-01T00:00:00Z", complete=True):
    return {
        "scan_id": scan_id,
        "batch_id": batch_id,
        "region": region,
        "catalog": catalog,
        "listing_complete": 1 if complete else 0,
        "anomaly_reason": anomaly,
        "observed_at": at,
        "body": "not_found",
    }


def _scans(*batch_map):
    """batch_map: (scan_id, batch_id, complete, anomaly, status)"""
    out = {}
    for scan_id, batch_id, complete, anomaly, status in batch_map:
        out[scan_id] = {
            "complete": 1 if complete else 0,
            "anomaly": anomaly,
            "status": status,
            "started_at": "2026-01-01T00:00:00Z",
            "batch_id": batch_id,
        }
    return out


@check("[单元] 一个批次不算删除证据")
def unit_one_batch(h) -> None:
    scans = _scans(("s1", "b1", True, 0, "ok"), ("s2", "b1", True, 0, "ok"), ("s3", "b1", True, 0, "ok"))
    obs = [_obs("s1", "b1", "us", "absent"), _obs("s2", "b1", "eu", "absent"), _obs("s3", "b1", "apac", "absent")]
    ev = evidence_from_observations("x", obs, scans, POLICY)
    verdict = evaluate({"id": "x", "state": "active"}, ev, POLICY)
    assert ev.absent_complete_scans == 3 and ev.absent_regions == ["apac", "eu", "us"]
    assert len(ev.absent_batches) == 1
    assert verdict.decision == "quarantine", verdict.decision


@check("[单元] 两个批次 × 两个地区达到门槛")
def unit_two_batches(h) -> None:
    scans = _scans(
        ("a1", "b1", True, 0, "ok"), ("a2", "b1", True, 0, "ok"),
        ("c1", "b2", True, 0, "ok"), ("c2", "b2", True, 0, "ok"),
    )
    obs = [
        _obs("a1", "b1", "us", "absent", at="2026-01-01T00:00:00Z"),
        _obs("a2", "b1", "eu", "absent", at="2026-01-01T00:00:00Z"),
        _obs("c1", "b2", "us", "absent", at="2026-01-02T00:00:00Z"),
        _obs("c2", "b2", "eu", "absent", at="2026-01-02T00:00:00Z"),
    ]
    ev = evidence_from_observations("x", obs, scans, POLICY)
    assert ev.absent_complete_scans == 4
    assert ev.span_seconds == 86400
    verdict = evaluate({"id": "x", "state": "active"}, ev, POLICY)
    assert verdict.decision == "retract", (verdict.decision, verdict.missing)


@check("[单元] 尾段中任何一次可见都打断连胜")
def unit_presence_resets(h) -> None:
    scans = _scans(
        ("a1", "b1", True, 0, "ok"), ("a2", "b1", True, 0, "ok"),
        ("b1", "b2", True, 0, "ok"), ("b2", "b2", True, 0, "ok"),
        ("c1", "b3", True, 0, "ok"), ("c2", "b3", True, 0, "ok"),
    )
    obs = [
        _obs("a1", "b1", "us", "absent", at="2026-01-01T00:00:00Z"),
        _obs("a2", "b1", "eu", "absent", at="2026-01-01T00:00:00Z"),
        _obs("b1", "b2", "us", "absent", at="2026-01-02T00:00:00Z"),
        _obs("b2", "b2", "eu", "absent", at="2026-01-02T00:00:00Z"),
        _obs("c1", "b3", "us", "absent", at="2026-01-03T00:00:00Z"),
        _obs("c2", "b3", "eu", "absent", at="2026-01-03T00:00:00Z"),
    ]
    first = evidence_from_observations("x", obs, scans, POLICY)
    assert evaluate({"id": "x", "state": "active"}, first, POLICY).decision == "retract"
    # Same history but the middle batch still saw the item: tail restarts at b3.
    obs[2] = _obs("b1", "b2", "us", "present", at="2026-01-02T00:00:00Z")
    second = evidence_from_observations("x", obs, scans, POLICY)
    assert second.absent_complete_scans == 2
    assert second.latest_present is False
    assert evaluate({"id": "x", "state": "active"}, second, POLICY).decision != "retract"


@check("[单元] 最近一次扫描仍可见则恢复而不是撤回")
def unit_latest_visible(h) -> None:
    scans = _scans(
        ("a1", "b1", True, 0, "ok"), ("a2", "b1", True, 0, "ok"),
        ("a3", "b1", True, 0, "ok"),
    )
    obs = [
        _obs("a1", "b1", "us", "absent"),
        _obs("a2", "b1", "eu", "absent"),
        _obs("a3", "b1", "apac", "present"),
    ]
    ev = evidence_from_observations("x", obs, scans, POLICY)
    assert ev.latest_present is True
    assert evaluate({"id": "x", "state": "quarantined"}, ev, POLICY).decision == "restore"


@check("[单元] 异常批次抬高门槛")
def unit_anomaly_raises_bar(h) -> None:
    scans = _scans(
        ("a1", "b1", True, 0, "ok"), ("a2", "b1", True, 0, "ok"),
        ("b1", "b2", True, 0, "ok"), ("b2", "b2", True, 0, "ok"),
        ("c1", "b3", True, 0, "ok"), ("c2", "b3", True, 0, "ok"),
    )
    obs = [
        _obs("a1", "b1", "us", "absent"), _obs("a2", "b1", "eu", "absent"),
        _obs("b1", "b2", "us", "absent", anomaly="body_error_page_200"),
        _obs("b2", "b2", "eu", "absent", anomaly="body_region_blocked"),
        _obs("c1", "b3", "us", "absent"), _obs("c2", "b3", "eu", "absent"),
    ]
    ev = evidence_from_observations("x", obs, scans, POLICY)
    assert ev.ambiguity_scans >= 1
    assert ev.anomaly_heavy is True
    verdict = evaluate({"id": "x", "state": "active"}, ev, POLICY)
    assert verdict.decision != "retract", verdict.decision
    assert verdict.requirement["min_scans"] == POLICY.min_scans + POLICY.anomaly_extra_scans


@check("[单元] 人工冻结优先，且不产生删除")
def unit_manual_hold(h) -> None:
    ev = Evidence("x", absent_complete_scans=9, absent_regions=["us", "eu", "apac"], absent_batches=["b1", "b2", "b3"])
    verdict = evaluate({"id": "x", "state": "quarantined", "manual_hold": 1, "hold_reason": "法务"}, ev, POLICY)
    assert verdict.decision == "hold"


@check("[单元] 来源通知优先于任何扫描证据")
def unit_notice_priority(h) -> None:
    ev = Evidence("x", present_complete_scans=5, latest_present=True)
    verdict = evaluate({"id": "x", "state": "active"}, ev, POLICY, notice_count=1)
    assert verdict.decision == "retract"
    assert verdict.reason == "source_withdrawal_notice"


@check("[单元] 证据结构可序列化往返")
def unit_evidence_roundtrip(h) -> None:
    ev = Evidence("x", absent_complete_scans=4, absent_regions=["us"], absent_batches=["b1"], span_seconds=60,
                  ambiguity_kinds={"body_region_blocked": 2}, population_ratio=0.5, latest_ambiguous=True)
    clone = Evidence.from_dict(ev.to_dict())
    assert clone.to_dict() == ev.to_dict()
