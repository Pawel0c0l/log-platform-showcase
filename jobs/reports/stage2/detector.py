from __future__ import annotations

from jobs.reports.stage2.models import DetectionResult
from jobs.reports.stage2.registry import REGISTERED_REPORTS

DETECT_THRESHOLD = 0.70


def detect_report_type(tables) -> DetectionResult:
    candidates: list[dict] = []
    for report_cls in REGISTERED_REPORTS:
        try:
            score = float(report_cls.detect(tables))
        except Exception:
            score = 0.0
        candidates.append({"report_type": report_cls.TYPE, "score": round(max(0.0, min(score, 1.0)), 4)})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0] if candidates else {"report_type": None, "score": 0.0}

    pending_reason = None
    if best["score"] < DETECT_THRESHOLD:
        pending_reason = "low_detection_confidence"

    return DetectionResult(
        report_type=best["report_type"],
        detect_score=best["score"],
        candidates_top3=candidates[:3],
        pending_reason=pending_reason,
    )
