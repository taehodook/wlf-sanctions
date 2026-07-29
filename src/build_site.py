"""
build_site.py
=============
수집 → 변경탐지 → 대시보드용 JSON 생성 파이프라인.

GitHub Actions가 매일 실행하는 진입점입니다.

산출물 (data/)
------------------
    meta.json               생성시각, 출처별 건수, 최근 변경일
    index.json              슬림 인덱스 (배열의 배열 — 용량 최소화)
    detail/{SOURCE}.json    출처별 상세 레코드 (지연 로드용)
    stats.json              통계 (출처별/유형별/국적 Top20/추이)
    changes/{YYYY-MM-DD}.json   일별 변경 상세
    changes/index.json      변경 이력 목록

설계 메모
--------
* 전체 스냅샷을 매일 커밋하면 저장소가 폭증하므로, 저장소에는 diff만 일별 보관합니다.
* 비교 기준(이전 스냅샷)은 직전 커밋의 index.json + detail/*.json을 복원해 사용합니다.
* 원본 파일은 Actions Artifact로 분리 보관합니다 (raw/ 디렉토리, 커밋하지 않음).
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import diff_engine
from sanctions_collector import SanctionsAggregator, setup_logging

LOGGER = logging.getLogger("sanctions.build")

KST = timezone(timedelta(hours=9))

DATA_DIR = "data"
DETAIL_DIR = os.path.join(DATA_DIR, "detail")
CHANGES_DIR = os.path.join(DATA_DIR, "changes")

# 슬림 인덱스 필드 (짧은 키 = 용량 절감). 대시보드 JS와 반드시 일치시킬 것.
# 슬림 인덱스 필드. cc = ISO alpha2 국가코드(국가위험평가 연계 키).
INDEX_FIELDS = ["s", "n", "t", "u", "c", "d", "p", "l", "cc"]

# 화면에 표시할 출처 순서. 수집기에 새 출처를 추가하면 여기에도 넣어야
# "수집 불가"로라도 화면에 잡힙니다(빠뜨리면 조용히 사라짐).
SOURCE_ORDER = ("OFAC", "EU", "UNSC", "KoFIU")

SOURCE_INFO = {
    "OFAC":  {"name": "미국 재무부 OFAC SDN",
              "url": "https://sanctionslistservice.ofac.treas.gov/"},
    "EU":    {"name": "EU 통합 금융제재 명단",
              "url": "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions"},
    "UNSC":  {"name": "UN 안보리 통합제재명단",
              "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml"},
    "KoFIU": {"name": "금융위원회 금융거래등제한대상자 (고시 별표)",
              # kofiu.go.kr는 제도 설명 페이지일 뿐 명단이 없습니다.
              # 명단 원본은 국가법령정보센터의 고시 별표입니다.
              "url": "https://www.law.go.kr/행정규칙/금융거래등제한대상자 지정 및 지정 취소에 관한 규정"},
}
#                출처 이름 유형 uid 국적 생년월일

TREND_MAX_POINTS = 90   # 추이 그래프 보관 일수

# 상세 파일 샤딩 수.
# OFAC만 1.9만 건 → 단일 파일 8.5MB이라 폰에서 상세 1건 보려고 전체를 받게 됩니다.
# 키 해시로 24분할하면 1회 요청이 ~360KB로 줄어듭니다.
# ※ 이 함수는 docs/index.html의 shardOf()와 반드시 동일한 결과를 내야 합니다.
DETAIL_SHARDS = 24


def shard_of(key: str) -> str:
    return f"{sum(ord(c) for c in key) % DETAIL_SHARDS:02d}"


# ---------------------------------------------------------------------------
# 입출력 헬퍼
# ---------------------------------------------------------------------------

def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.debug("읽기 실패 (기본값 사용): %s | %s", path, exc)
        return default


def _write_json(path: str, payload: Any, compact: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        if compact:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
    size_kb = os.path.getsize(path) / 1024
    LOGGER.info("저장: %-40s %8.1f KB", path, size_kb)


# ---------------------------------------------------------------------------
# 이전 스냅샷 복원
# ---------------------------------------------------------------------------

def load_previous_records() -> List[Dict[str, Any]]:
    """직전 실행이 남긴 detail/*.json을 읽어 이전 레코드를 복원."""
    records: List[Dict[str, Any]] = []
    if not os.path.isdir(DETAIL_DIR):
        LOGGER.info("이전 스냅샷 없음 — 최초 실행으로 간주")
        return records

    for root, _dirs, files in os.walk(DETAIL_DIR):
        for filename in sorted(files):
            if not filename.endswith(".json"):
                continue
            payload = _read_json(os.path.join(root, filename), default={})
            if isinstance(payload, dict):
                records.extend(payload.values())

    LOGGER.info("이전 스냅샷 복원: %d건", len(records))
    return records


# ---------------------------------------------------------------------------
# 산출물 생성
# ---------------------------------------------------------------------------

def build_index(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [
        [
            r.get("source", ""),
            r.get("name", ""),
            r.get("type", ""),
            r.get("uid") or "",
            r.get("nationality") or "",
            r.get("birth_date") or "",
            r.get("program") or "",
            r.get("listed_on") or "",
            r.get("nationality_code") or "",
        ]
        for r in records
    ]
    return {"fields": INDEX_FIELDS, "rows": rows}


def build_details(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """출처 × 샤드로 분할. 키는 diff_engine의 record_key와 동일하게 맞춥니다.

    반환: {source: {shard: {key: record}}}
    """
    buckets: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for record in records:
        source = record.get("source", "UNKNOWN")
        key = diff_engine.record_key(record)
        buckets.setdefault(source, {}).setdefault(shard_of(key), {})[key] = record
    return buckets


def build_stats(records: List[Dict[str, Any]], trend: List[List[Any]]) -> Dict[str, Any]:
    source_counter = Counter(r.get("source", "UNKNOWN") for r in records)
    type_counter = Counter(r.get("type", "Unknown") for r in records)

    nationality_counter: Counter = Counter()
    for record in records:
        raw = (record.get("nationality") or "").strip()
        if not raw:
            continue
        for token in raw.split(","):
            token = token.strip()
            if token:
                nationality_counter[token] += 1

    program_counter: Counter = Counter()
    for record in records:
        program = (record.get("program") or "").strip()
        if program:
            program_counter[program] += 1

    # 지정연도 분포.
    # 주의: OFAC SDN.CSV에는 지정일 컬럼이 없습니다(전량 공란). 따라서 이 집계는
    # 사실상 UNSC 등 listed_on 보유 소스 한정입니다. 대시보드에서 모집단을 명시해야
    # 전체 명단의 연도별 추이로 오독되지 않습니다.
    year_counter: Counter = Counter()
    year_sources: set = set()
    for record in records:
        matched = re.search(r"(19|20)\d{2}", str(record.get("listed_on") or ""))
        if matched:
            year_counter[matched.group(0)] += 1
            year_sources.add(record.get("source", "UNKNOWN"))

    # 출처 × 유형 교차표 (대시보드 매트릭스용)
    cross: Dict[str, Counter] = {}
    for record in records:
        cross.setdefault(record.get("source", "UNKNOWN"), Counter())[record.get("type", "Unknown")] += 1

    return {
        "total": len(records),
        "by_source": source_counter.most_common(),
        "by_type": type_counter.most_common(),
        "top_nationalities": nationality_counter.most_common(20),
        "top_programs": program_counter.most_common(12),
        "cross": {src: dict(counter) for src, counter in cross.items()},
        "by_year": sorted(year_counter.items()),
        "by_year_scope": {
            "sources": sorted(year_sources),
            "covered": sum(year_counter.values()),
            "total": len(records),
        },
        "dob_known_ratio": round(
            sum(1 for r in records if (r.get("birth_date") or "").strip())
            / max(1, sum(1 for r in records if r.get("type") == "Individual")), 4
        ),
        "trend": trend,
    }


def update_trend(existing: List[List[Any]], today: str, total: int,
                 by_source: Dict[str, int]) -> List[List[Any]]:
    """[date, total, {source: count}] 형태의 추이 데이터 갱신 (같은 날 재실행 시 덮어씀)."""
    trend = [point for point in existing if point and point[0] != today]
    trend.append([today, total, by_source])
    trend.sort(key=lambda p: p[0])
    return trend[-TREND_MAX_POINTS:]


def update_changes_index(date: str, diff: Dict[str, Any]) -> List[Dict[str, Any]]:
    path = os.path.join(CHANGES_DIR, "index.json")
    entries = _read_json(path, default=[]) or []
    entries = [e for e in entries if e.get("date") != date]
    entries.append({
        "date": date,
        "added": diff["summary"]["added"],
        "removed": diff["summary"]["removed"],
        "modified": diff["summary"]["modified"],
        "total": diff["summary"]["total_after"],
        "by_source": diff["by_source"],
    })
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:365]


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------

def main() -> int:
    setup_logging()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    LOGGER.info("=" * 60)
    LOGGER.info("사이트 데이터 빌드 시작 — %s", now.strftime("%Y.%m.%d. %H:%M:%S KST"))
    LOGGER.info("=" * 60)

    # 1) 이전 스냅샷 복원 (변경 비교용)
    previous = load_previous_records()

    # 2) 수집
    aggregator = SanctionsAggregator()
    records = [asdict(r) for r in aggregator.run()]

    if not records:
        LOGGER.error("수집 결과 0건 — 기존 데이터를 보존하고 종료합니다.")
        return 1

    # 3) 소스 단위 안전장치: 특정 소스가 통째로 실패하면 이전 데이터로 대체
    #    (예: KoFIU 게시판 개편으로 파싱 실패 → 명단이 통째로 사라지는 사고 방지)
    collected_sources = {r["source"] for r in records}
    for source in {r["source"] for r in previous} - collected_sources:
        fallback = [r for r in previous if r["source"] == source]
        LOGGER.warning(
            "[%s] 이번 수집 실패 → 이전 데이터 %d건 유지 (STALE 표시)", source, len(fallback)
        )
        for record in fallback:
            record["stale"] = True
        records.extend(fallback)

    # 4) 변경 탐지
    diff = diff_engine.compare(previous, records)
    ok, reason = diff_engine.is_significant(diff)
    if not ok:
        LOGGER.error("서킷브레이커 작동: %s", reason)
        LOGGER.error("데이터를 갱신하지 않고 종료합니다. 파서 점검이 필요합니다.")
        return 2

    # 5) 산출물 생성
    _write_json(os.path.join(DATA_DIR, "index.json"), build_index(records), compact=True)

    # 삭제된 대상이 옛 샤드 파일에 남지 않도록 상세 디렉토리를 통째로 재생성합니다.
    if os.path.isdir(DETAIL_DIR):
        shutil.rmtree(DETAIL_DIR)
    for source, shards in build_details(records).items():
        for shard, bucket in shards.items():
            _write_json(os.path.join(DETAIL_DIR, source, f"{shard}.json"), bucket, compact=True)

    # 출처별 건수는 수집기 자체 집계(aggregator.stats)가 아니라 **실제 레코드**로 셉니다.
    # 원본에 중복 항목이 있으면(KoFIU 고시에 동일 대상이 두 번 실린 사례 확인)
    # 수집 건수와 저장 건수가 어긋나 화면 합계가 총계와 맞지 않게 됩니다.
    by_source = collections.Counter(r["source"] for r in records)
    declared = dict(aggregator.stats)
    for code, n in declared.items():
        if by_source.get(code, 0) != n:
            LOGGER.warning("[%s] 수집 %d건 중 %d건만 저장 — 원본 중복 항목으로 추정",
                           code, n, by_source.get(code, 0))
    by_source = dict(by_source)
    previous_stats = _read_json(os.path.join(DATA_DIR, "stats.json"), default={}) or {}
    trend = update_trend(previous_stats.get("trend", []), today, len(records), by_source)
    _write_json(os.path.join(DATA_DIR, "stats.json"), build_stats(records, trend))

    # 변경 상세는 실제 변동이 있을 때만 파일 생성
    if any(diff["summary"][k] for k in ("added", "removed", "modified")):
        change_payload = {
            "date": today,
            "summary": diff["summary"],
            "by_source": diff["by_source"],
            # 대량 변동 시 파일 폭증 방지를 위해 상세는 상한을 둡니다.
            "added": [_slim(r) for r in diff["added"][:2000]],
            "removed": [_slim(r) for r in diff["removed"][:2000]],
            "modified": diff["modified"][:2000],
        }
        _write_json(os.path.join(CHANGES_DIR, f"{today}.json"), change_payload, compact=True)
    else:
        LOGGER.info("변동 없음 — 변경 상세 파일 생략")

    _write_json(os.path.join(CHANGES_DIR, "index.json"), update_changes_index(today, diff))

    # 출처별 수집 상태.
    # 대시보드의 "갱신 지연" 판정 근거입니다. 이번 회차에 실제로 수집에 성공한
    # 소스만 collected_at을 오늘로 올리고, 실패해 이전 데이터로 버틴 소스는
    # 직전 성공일을 그대로 물려받아 D+경과가 계속 쌓이게 합니다.
    previous_meta = _read_json(os.path.join(DATA_DIR, "meta.json"), default={}) or {}
    previous_status = {s["code"]: s for s in previous_meta.get("source_status", [])}
    source_status = []
    for code in SOURCE_ORDER:
        fresh = code in collected_sources
        source_status.append({
            "code": code,
            "count": by_source.get(code, 0),
            "collected_at": today if fresh else previous_status.get(code, {}).get("collected_at"),
            "stale": not fresh,
        })

    _write_json(os.path.join(DATA_DIR, "meta.json"), {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_kst": now.strftime("%Y.%m.%d. %H:%M"),
        "total": len(records),
        "by_source": by_source,
        "stale_sources": sorted({r["source"] for r in records if r.get("stale")}),
        "last_change": diff["summary"],
        "source_status": source_status,
        "sources": [
            {"code": code, "name": SOURCE_INFO[code]["name"], "url": SOURCE_INFO[code]["url"]}
            for code in SOURCE_ORDER
        ],
    })

    LOGGER.info("빌드 완료 — 총 %d건 / 신규 %d / 삭제 %d / 변경 %d",
                len(records), diff["summary"]["added"], diff["summary"]["removed"],
                diff["summary"]["modified"])
    return 0


def _slim(record: Dict[str, Any]) -> Dict[str, Any]:
    """변경 파일에는 핵심 필드만 담아 용량을 억제."""
    return {
        k: record.get(k)
        for k in ("source", "name", "type", "uid", "nationality", "birth_date", "program")
        if record.get(k)
    }


if __name__ == "__main__":
    sys.exit(main())
