"""
diff_engine.py
==============
전일 스냅샷 대비 신규(ADDED) / 삭제(REMOVED) / 변경(MODIFIED) 탐지.

식별 키 설계
-----------
    key = f"{source}|{uid or NAME_UPPER}|{type}"

    * OFAC / UNSC : 기관 고유번호(ent_num, DATAID)가 있으므로 uid 사용 → 개명·표기변경 추적 가능
    * KoFIU       : 고유번호가 없어 정규화된 이름을 키로 사용 → 표기 변경 시 '삭제+신규'로 잡힘(한계 명시)

MODIFIED 판정 대상 필드는 WATCHED_FIELDS로 한정합니다.
collected_at 같은 메타 필드는 매일 바뀌므로 반드시 제외해야 합니다.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

LOGGER = logging.getLogger("sanctions.diff")

# 변경 감지 대상 필드 (collected_at 등 메타 필드는 제외)
WATCHED_FIELDS = [
    "name", "type", "nationality", "birth_date",
    "address", "program", "listed_on", "aliases",
]


def normalize_name_key(name: str) -> str:
    """이름을 키로 쓰기 위한 정규화: 대문자화 + 특수문자/공백 제거."""
    return re.sub(r"[^A-Z0-9가-힣]", "", (name or "").upper())


def record_key(record: Dict[str, Any]) -> str:
    source = record.get("source", "")
    uid = (record.get("uid") or "").strip()
    identifier = uid if uid else normalize_name_key(record.get("name", ""))
    return f"{source}|{identifier}|{record.get('type', '')}"


def _field_value(record: Dict[str, Any], field: str) -> str:
    value = record.get(field)
    if isinstance(value, list):
        return " | ".join(sorted(str(v) for v in value))
    return (str(value) if value is not None else "").strip()


def compare(
    previous: List[Dict[str, Any]],
    current: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """이전/현재 레코드 리스트를 비교해 변경 내역 dict를 반환."""

    prev_map = {record_key(r): r for r in previous}
    curr_map = {record_key(r): r for r in current}

    prev_keys = set(prev_map)
    curr_keys = set(curr_map)

    added = [curr_map[k] for k in sorted(curr_keys - prev_keys)]
    removed = [prev_map[k] for k in sorted(prev_keys - curr_keys)]

    modified: List[Dict[str, Any]] = []
    for key in sorted(prev_keys & curr_keys):
        before, after = prev_map[key], curr_map[key]
        changes: List[Dict[str, str]] = []
        for field in WATCHED_FIELDS:
            old_value, new_value = _field_value(before, field), _field_value(after, field)
            if old_value != new_value:
                changes.append({"field": field, "before": old_value, "after": new_value})
        if changes:
            modified.append({
                "source": after.get("source"),
                "name": after.get("name"),
                "type": after.get("type"),
                "uid": after.get("uid"),
                "changes": changes,
            })

    result = {
        "added": added,
        "removed": removed,
        "modified": modified,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "total_before": len(previous),
            "total_after": len(current),
        },
        "by_source": _summarize_by_source(added, removed, modified),
    }

    LOGGER.info(
        "변경 탐지: 신규 %d / 삭제 %d / 변경 %d (총 %d → %d)",
        len(added), len(removed), len(modified), len(previous), len(current),
    )
    return result


def _summarize_by_source(
    added: List[Dict[str, Any]],
    removed: List[Dict[str, Any]],
    modified: List[Dict[str, Any]],
) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}

    def bump(items: List[Dict[str, Any]], bucket: str) -> None:
        for item in items:
            source = item.get("source") or "UNKNOWN"
            summary.setdefault(source, {"added": 0, "removed": 0, "modified": 0})
            summary[source][bucket] += 1

    bump(added, "added")
    bump(removed, "removed")
    bump(modified, "modified")
    return summary


def is_significant(diff: Dict[str, Any], threshold_ratio: float = 0.30) -> Tuple[bool, str]:
    """비정상 대량 변동 감지 (파싱 실패로 인한 대량 삭제 오탐 방지용 서킷브레이커).

    전체 대비 삭제 비율이 threshold를 넘으면 '수집 오류 의심'으로 판정합니다.
    실무상 하루에 명단의 30%가 사라지는 일은 없습니다 — 대개 파서가 깨진 것입니다.
    """
    total_before = diff["summary"]["total_before"]
    if total_before == 0:
        return True, "최초 수집 (비교 대상 없음)"

    removed_ratio = diff["summary"]["removed"] / total_before
    if removed_ratio > threshold_ratio:
        return False, (
            f"삭제 비율 {removed_ratio:.1%} > 임계치 {threshold_ratio:.0%} — "
            f"수집 오류 의심. 반영 보류 권고."
        )
    return True, "정상 범위"
