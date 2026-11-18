"""Evidence evaluation: decide whether an item may be marked retracted.

Design rules encoded here:

1. **A single empty or failed scan never deletes anything.** Notices alone can
   retract; empty scans alone cannot.
2. **Deletion evidence is built from repeated *complete* catalog observations
   across multiple regions and multiple scan batches.** A regional blind spot
   therefore cannot look like withdrawal.
3. **Ambiguity is asymmetry-aware.** A 451/403, a 200 error page, a count
   mismatch, a truncated body or a wrong-region cache all count as *not
   evidence of deletion*; some of them count as *evidence of presence* when the
   item is still listed or readable elsewhere.
4. **Quarantine is reversible, retraction is sticky.** Once retracted, a later
   accidental sighting can never auto-republish; that needs an explicit,
   audited operator record.
5. **Notices are durable.** A valid, source-attributed withdrawal notice
   retracts immediately and cannot be undone by a later healthy scan, even if
   the body page appears after the notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import PolicyConfig
from .util import iso, json_loads, parse_iso


@dataclass
class Evidence:
    item_id: str
    absent_complete_scans: int = 0
    absent_regions: list[str] = field(default_factory=list)
    absent_batches: list[str] = field(default_factory=list)
    present_complete_scans: int = 0
    ambiguity_scans: int = 0
    ambiguity_kinds: dict[str, int] = field(default_factory=dict)
    last_present_at: str | None = None
    oldest_absent_at: str | None = None
    newest_absent_at: str | None = None
    span_seconds: int = 0
    anomaly_heavy: bool = False
    population_ratio: float | None = None
    population_note: str = ""
    latest_present: bool = False
    latest_ambiguous: bool = False
    scan_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "absent_complete_scans": self.absent_complete_scans,
            "absent_regions": self.absent_regions,
            "absent_batches": self.absent_batches,
            "present_complete_scans": self.present_complete_scans,
            "ambiguity_scans": self.ambiguity_scans,
            "ambiguity_kinds": self.ambiguity_kinds,
            "last_present_at": self.last_present_at,
            "oldest_absent_at": self.oldest_absent_at,
            "newest_absent_at": self.newest_absent_at,
            "span_seconds": self.span_seconds,
            "anomaly_heavy": self.anomaly_heavy,
            "population_ratio": self.population_ratio,
            "population_note": self.population_note,
            "latest_present": self.latest_present,
            "latest_ambiguous": self.latest_ambiguous,
            "scan_ids": self.scan_ids,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        ev = cls(item_id=str(data.get("item_id", "")))
        for key in (
            "absent_complete_scans",
            "present_complete_scans",
            "ambiguity_scans",
            "span_seconds",
        ):
            setattr(ev, key, int(data.get(key) or 0))
        for key in ("absent_regions", "absent_batches", "scan_ids"):
            setattr(ev, key, [str(x) for x in (data.get(key) or [])])
        ev.ambiguity_kinds = {str(k): int(v) for k, v in (data.get("ambiguity_kinds") or {}).items()}
        ev.last_present_at = data.get("last_present_at")
        ev.oldest_absent_at = data.get("oldest_absent_at")
        ev.newest_absent_at = data.get("newest_absent_at")
        ev.anomaly_heavy = bool(data.get("anomaly_heavy"))
        ratio = data.get("population_ratio")
        ev.population_ratio = float(ratio) if ratio is not None else None
        ev.population_note = str(data.get("population_note") or "")
        ev.latest_present = bool(data.get("latest_present"))
        ev.latest_ambiguous = bool(data.get("latest_ambiguous"))
        return ev


@dataclass
class Verdict:
    decision: str  # retract | quarantine | restore | keep | hold
    reason: str
    requirement: dict
    evidence: Evidence
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "requirement": self.requirement,
            "evidence": self.evidence.to_dict(),
            "missing": self.missing,
        }


def _requirement(evidence: Evidence, policy: PolicyConfig) -> dict:
    extra = policy.anomaly_extra_scans if evidence.anomaly_heavy else 0
    return {
        "min_scans": policy.min_scans + extra,
        "min_regions": policy.min_regions,
        "min_scan_batches": policy.min_scan_batches,
        "min_span_seconds": policy.min_span_seconds,
        "anomaly_extra_scans": extra,
    }


def _missing(evidence: Evidence, policy: PolicyConfig, req: dict) -> list[str]:
    gaps: list[str] = []
    if evidence.absent_complete_scans < req["min_scans"]:
        gaps.append(f"scans {evidence.absent_complete_scans}/{req['min_scans']}")
    if len(evidence.absent_regions) < req["min_regions"]:
        gaps.append(f"regions {len(evidence.absent_regions)}/{req['min_regions']}")
    if len(evidence.absent_batches) < req["min_scan_batches"]:
        gaps.append(f"batches {len(evidence.absent_batches)}/{req['min_scan_batches']}")
    if req["min_span_seconds"] > 0 and evidence.span_seconds < req["min_span_seconds"]:
        gaps.append(f"span {evidence.span_seconds}s/{req['min_span_seconds']}s")
    return gaps


def evaluate(
    item: dict,
    evidence: Evidence,
    policy: PolicyConfig,
    *,
    notice_count: int = 0,
    notices_durable: bool = True,
    now: str | None = None,
) -> Verdict:
    """Return the verdict for one item given its accumulated evidence."""

    item_id = item.get("id", "")
    now = now or iso()

    if notice_count > 0 and notices_durable:
        return Verdict(
            decision="retract",
            reason="source_withdrawal_notice",
            requirement={"notice_required": 1, "notices_present": notice_count},
            evidence=evidence,
        )

    if item.get("manual_hold"):
        return Verdict(
            decision="hold",
            reason="manual_hold",
            requirement={"note": item.get("hold_reason") or "operator hold"},
            evidence=evidence,
        )

    req = _requirement(evidence, policy)
    gaps = _missing(evidence, policy, req)

    # Enough durable evidence: retract regardless of historic sightings.
    if evidence.absent_complete_scans > 0 and not gaps:
        return Verdict(
            decision="retract",
            reason="multi_scan_multi_region_complete_absence",
            requirement=req,
            evidence=evidence,
        )

    if evidence.latest_present:
        return Verdict(
            decision="restore",
            reason="present_in_complete_scan",
            requirement=req,
            evidence=evidence,
        )

    if evidence.latest_ambiguous or evidence.ambiguity_scans > 0:
        return Verdict(
            decision="quarantine",
            reason="ambiguous_observation:" + ",".join(sorted(evidence.ambiguity_kinds)),
            requirement=req,
            evidence=evidence,
            missing=gaps,
        )

    if evidence.absent_complete_scans > 0:
        return Verdict(
            decision="quarantine",
            reason="insufficient_evidence",
            requirement=req,
            evidence=evidence,
            missing=gaps,
        )

    return Verdict(decision="keep", reason="no_signal", requirement=req, evidence=evidence)


def evidence_from_observations(
    item_id: str,
    observations: list[dict],
    scans: dict[str, dict],
    policy: PolicyConfig,
    *,
    population: dict[str, dict] | None = None,
    batch_scans: dict[str, list[dict]] | None = None,
) -> Evidence:
    """Fold item observations into deletion evidence.

    Evidence is aggregated per **batch** (one sweep across all regions) and
    classified **per region observation**:

    * ``present``  — the item was listed or its full body was readable;
    * ``absence``  — the catalog was complete and healthy, the item was not
      listed, and the body probe returned a clean tombstone or 404;
    * ``ambiguous``— anything else (region block, 200 error page, truncated or
      unreadable body, incomplete scan, wrong-region cache).

    A single region being unreadable does **not** discard the clean absences
    from the other regions; it raises the deletion bar instead. But a region
    whose *body* is region-blocked can never count as a clean absence, because
    the same block may explain the catalog omission.

    Deletion evidence is the **tail streak** after the most recent presence
    batch: one sighting anywhere in the streak resets it.
    """
    ev = Evidence(item_id=item_id)
    per_batch: dict[str, dict] = {}

    for obs in observations:
        scan_id = str(obs.get("scan_id") or "")
        if not scan_id:
            continue
        scan_meta = scans.get(scan_id, {})
        catalog = str(obs.get("catalog") or "")
        anomaly = str(obs.get("anomaly_reason") or obs.get("anomaly") or "").strip()
        healthy_scan = (
            bool(scan_meta.get("complete"))
            and not scan_meta.get("anomaly")
            and str(scan_meta.get("status")) in ("ok", "complete")
        )
        batch_id = str(obs.get("batch_id") or scan_meta.get("batch_id") or scan_id)
        entry = per_batch.setdefault(
            batch_id,
            {
                "at": str(obs.get("observed_at") or scan_meta.get("started_at") or ""),
                "last_at": "",
                "presence": False,
                "ambiguity": False,
                "absence_regions": set(),
                "kinds": set(),
                "scan_ids": set(),
            },
        )
        at = str(obs.get("observed_at") or "")
        if at and (not entry["at"] or at < entry["at"]):
            entry["at"] = at
        if at and at > entry["last_at"]:
            entry["last_at"] = at
        entry["scan_ids"].add(scan_id)

        if catalog == "present":
            entry["presence"] = True
            continue

        clean_absence = (
            catalog in ("absent", "tombstone")
            and not anomaly
            and healthy_scan
            and bool(obs.get("listing_complete"))
        )
        if clean_absence:
            entry["absence_regions"].add(str(obs.get("region") or ""))
        else:
            entry["ambiguity"] = True
            if anomaly:
                entry["kinds"].add(anomaly)
            elif not healthy_scan:
                entry["kinds"].add("scan_not_complete")
            else:
                entry["kinds"].add(catalog or "ambiguous")

    # A regional scan that failed outright (403/451, 200 error page, truncation,
    # network) creates no observations at all, yet it is exactly the case where
    # that region's absence cannot be trusted. Surface it as batch ambiguity so
    # it raises the deletion bar instead of silently shrinking the sample.
    for batch_id, scans_for_batch in (batch_scans or {}).items():
        bad = []
        for scan in scans_for_batch:
            if scan.get("complete") and not scan.get("anomaly") and str(scan.get("status")) in ("ok", "complete"):
                continue
            bad.append(str(scan.get("region") or "unknown"))
        if not bad:
            continue
        entry = per_batch.setdefault(
            batch_id,
            {
                "at": str(scans_for_batch[0].get("started_at") or ""),
                "last_at": str(scans_for_batch[0].get("started_at") or ""),
                "presence": False,
                "ambiguity": False,
                "absence_regions": set(),
                "kinds": set(),
                "scan_ids": set(),
            },
        )
        if entry["presence"]:
            continue
        entry["ambiguity"] = True
        for region in bad:
            entry["kinds"].add(f"region_scan_failed:{region}")

    ordered = sorted(per_batch.items(), key=lambda kv: (kv[1]["last_at"] or kv[1]["at"], kv[0]))
    last_presence_idx = -1
    for idx, (_batch, entry) in enumerate(ordered):
        if entry["presence"]:
            last_presence_idx = idx
    tail = ordered[last_presence_idx + 1 :]

    if ordered:
        last = ordered[-1][1]
        ev.latest_present = bool(last["presence"])
        ev.latest_ambiguous = bool(last["ambiguity"])

    for batch_id, entry in tail:
        ev.scan_ids.extend(sorted(entry["scan_ids"]))
        ev.ambiguity_scans += len(entry["kinds"]) if entry["ambiguity"] else 0
        if entry["ambiguity"]:
            for kind in entry["kinds"]:
                ev.ambiguity_kinds[str(kind)] = ev.ambiguity_kinds.get(str(kind), 0) + 1
        regions = sorted(r for r in entry["absence_regions"] if r)
        if not regions:
            continue
        ev.absent_complete_scans += len(regions)
        for region in regions:
            if region not in ev.absent_regions:
                ev.absent_regions.append(region)
        if batch_id not in ev.absent_batches:
            ev.absent_batches.append(batch_id)
        at = entry["at"]
        if at:
            if not ev.oldest_absent_at or at < ev.oldest_absent_at:
                ev.oldest_absent_at = at
            if not ev.newest_absent_at or at > ev.newest_absent_at:
                ev.newest_absent_at = at

    # Presence inside the tail cannot happen by construction; presence before
    # the tail is historic and recorded for the UI only.
    ev.present_complete_scans = sum(1 for _b, e in ordered[: last_presence_idx + 1] if e["presence"])
    if last_presence_idx >= 0:
        ev.last_present_at = ordered[last_presence_idx][1].get("last_at") or ordered[last_presence_idx][1]["at"]

    oldest = parse_iso(ev.oldest_absent_at)
    newest = parse_iso(ev.newest_absent_at)
    if oldest and newest:
        ev.span_seconds = max(0, int((newest - oldest).total_seconds()))

    # Population-level anomaly: a sweep that loses a large fraction of the
    # catalog between two complete scans is a publishing/ingestion fault, not
    # 200 individual withdrawals.
    if population:
        ev.population_ratio = population.get("ratio")
        ev.population_note = str(population.get("note") or "")
        if ev.population_ratio is not None and ev.population_ratio < 0.6:
            ev.anomaly_heavy = True
    # Any ambiguity in the tail streak raises the deletion bar: the collector
    # proved it cannot reliably read every region for this item, so more
    # independent complete observations are required before deleting.
    if ev.ambiguity_scans:
        ev.anomaly_heavy = True
    return ev
