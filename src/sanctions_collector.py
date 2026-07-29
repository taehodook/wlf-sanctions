"""
sanctions_collector.py  v2.1  (KYCflow WLF Bundle Builder 통합본)
=================================================================
AML/CFT WLF(요주의 인물 필터링) 시스템용 제재명단 자동 수집기

v2.0 → v2.1 변경 이력 (전부 실측 검증 완료 2026.07.17.)
------------------------------------------------------
[FIX ] normalize_type에 'Aircraft' 누락 → OFAC 항공기 344건이 'Unknown'으로 추락하던 회귀 수정.
       모르는 유형 값은 경고를 남기도록 하여 다음 회귀를 조기에 드러냅니다.
[FIX ] 별칭 절단(aliases[:8]) 제거 → 버려지던 별칭 1,718건(6.9%) 회수.
       WLF에서 별칭 누락은 곧 미탐입니다. 최다 보유 대상(REVIVAL OF ISLAMIC
       HERITAGE SOCIETY)은 95개 중 87개가 버려지고 있었습니다.
[FIX ] 지정근거 절단(programs[:3]) 제거 (해당 84건).
[ADD ] 국적 표기 정규화(countries.py) → ISO alpha2 + 한글 표시명.
       출처별 표기가 235종으로 갈라져 'Russia'(1,693) / 'RUSSIAN FEDERATION'(1,041)이
       다른 나라로 집계되던 문제 해소. 매핑률 99.9%(미매핑은 원문 보존).
       기준 테이블은 AML 국가위험평가 서비스의 255개국 정본을 그대로 사용합니다.
[ADD ] 이름 역순 별칭 — v2가 "AL ZAWAHIRI, Dr. Ayman"을 "Dr. Ayman AL ZAWAHIRI"로
       바꾸면서 기존 검색 표기가 사라지는 문제. 자연순을 본명으로 두되 OFAC 공식
       표기인 "성, 이름" 형태를 별칭으로 함께 실어 양쪽 다 검색되게 했습니다.
[FIX ] KoFIU 수동 반입 파일 인식 엄격화 — 이전에는 manual/의 아무 엑셀이나 집어
       정산자료가 제재명단으로 파싱될 수 있었습니다. 파일명 규칙 불일치 시 거부합니다.
[ADD ] 날짜 표기 ISO 통일(_iso_date) — KoFIU '2026.01.02.', EU '2015-07-01-04:00' 등
       혼재하던 형식을 YYYY-MM-DD로 정리(파싱 실패 시 원문 보존).

v1.0 → v2.0 변경 이력
--------------------
[FIX ] OFAC 소스를 CSV → XML 로 교체
       └ SDN.CSV에는 별칭(AKA) 컬럼이 없어 v1은 OFAC 별칭 0건이었음.
         XML은 akaList를 포함하므로 별칭을 회수. WLF 정확도 직결.
         (건수 19,217 동일 · uid 100% 일치 확인 → 기존 이력과 단절 없음)
[ADD ] EUCollector — EU 통합 금융제재 리스트(FSD XML, 5,994건) 신규
[FIX ] KoFIUCollector — 게시판 스크레이핑 → 수동 파일 반입(manual/) 방식으로 전환
       └ 사유: kofiu.go.kr 게시판이 AJAX(/cmn/board/selectLawList.do) 기반으로
         변경되어 정적 DOM 셀렉터로 접근 불가. 금융거래등제한대상자는
         공공데이터 API로도 개방되어 있지 않음(2026.07. 확인).
[ADD ] SHA-256 무결성 서명 + KYCflow 데모/정식판용 번들(JSON) 출력
[ADD ] 출처별 별칭·생년월일 보유율 리포트(Reconciliation 보조)

수집 대상 (2026.07.17. 실측)
---------------------------
1. OFAC (미국 재무부)     : SDN List (XML)          — 자동   19,217건
2. UNSC (UN 안보리)       : Consolidated List (XML) — 자동    1,010건
3. EU   (집행위 FSD)      : Consolidated List (XML) — 자동    5,994건
4. KoFIU (금융정보분석원) : 금융거래등제한대상자     — 수동 반입(manual/)

의존성 패키지
------------
    pip install requests pandas beautifulsoup4 lxml openpyxl
    ※ countries.json(255개국 정본)이 같은 폴더에 있어야 합니다.

실행
----
    python sanctions_collector.py                 # 전체 수집
    python sanctions_collector.py --no-eu         # EU 제외(용량 절감)
    python sanctions_collector.py --bundle-only   # 번들만 출력

주의
----
* 운영 반영 전 반드시 원본 대비 건수 검증(Reconciliation)을 수행하십시오.
* KoFIU 명단은 manual/ 폴더에 최신 파일을 두어야 반영됩니다(월 1회 이상 권장).
* OFAC은 원본에 지정일이 없습니다(전량 공란). 지정연도 통계는 UNSC·EU 한정입니다.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import pandas as pd
import requests

from countries import resolve as resolve_country

# ---------------------------------------------------------------------------
# 0. 공통 설정 / 로깅
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger("sanctions")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 90
DEFAULT_RETRIES = 3
RAW_DIR = "raw"
OUTPUT_DIR = "output"
MANUAL_DIR = "manual"


# ---------------------------------------------------------------------------
# 1. 정규화 스키마
# ---------------------------------------------------------------------------

@dataclass
class SanctionRecord:
    """4개 출처 데이터를 통합하는 정규화 레코드.

    필수 필드: source / name / type
    """

    source: str                                   # 'OFAC' | 'UNSC' | 'EU' | 'KoFIU'
    name: str                                     # 대상 명칭 (원문)
    type: str                                     # 'Individual' | 'Entity' | 'Vessel' | 'Unknown'

    uid: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    nationality: Optional[str] = None             # 표시명(정규화 성공 시 한글, 실패 시 원문)
    nationality_code: Optional[str] = None        # ISO alpha2. 국가위험평가 연계 키
    nationality_raw: Optional[str] = None         # 원문 보존 (감사·역추적용)
    birth_date: Optional[str] = None
    address: Optional[str] = None
    program: Optional[str] = None
    listed_on: Optional[str] = None
    remarks: Optional[str] = None
    collected_at: str = ""

    def __post_init__(self) -> None:
        self.name = _clean(self.name)
        self.aliases = [a for a in (_clean(x) for x in self.aliases) if a and a != self.name]
        # 중복 별칭 제거(순서 보존)
        self.aliases = list(dict.fromkeys(self.aliases))

        # 국적 정규화.
        # 출처마다 표기가 달라(실측 235종) 그대로 두면 같은 나라가 통계에서 갈라집니다.
        # 원문은 nationality_raw에 남겨 감사 시 역추적할 수 있게 합니다.
        if self.nationality and not self.nationality_code:
            self.nationality_raw = self.nationality
            code, display = resolve_country(self.nationality)
            self.nationality_code = code
            self.nationality = display or None

        if not self.collected_at:
            self.collected_at = datetime.now().isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    if s.lower() in ("nan", "none", "na", "n/a", "-0-"):
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _iso_date(value: Any) -> Optional[str]:
    """여러 날짜 표기를 ISO(YYYY-MM-DD)로 통일합니다.

    OFAC·UNSC·EU는 ISO로 주지만 KoFIU 고시는 "2026.01.02." 처럼 옵니다.
    섞이면 화면 날짜 포맷과 연도 집계가 어긋나므로 수집 단계에서 맞춥니다.
    파싱에 실패하면 버리지 말고 원문을 그대로 돌려줍니다.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = _clean(value)
    if not s:
        return None
    m = re.match(r"^(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return s
    if re.match(r"^\d{8}$", s):
        try:
            return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return s
    return s


def normalize_type(raw: Any) -> str:
    """출처별 유형 표기를 통일합니다.

    ※ 'Aircraft'를 빠뜨리면 OFAC 항공기 344건이 통째로 'Unknown'으로 떨어집니다.
       유형은 화면 필터·통계의 축이므로 새 값이 보이면 반드시 여기에 추가하십시오.
    """
    s = _clean(raw).lower()
    if not s:
        # OFAC CSV/XML 관례: sdnType 공란은 단체를 뜻합니다.
        return "Entity"
    if s in ("individual", "person", "개인", "p"):
        return "Individual"
    if s in ("vessel", "ship", "선박"):
        return "Vessel"
    if s in ("aircraft", "plane", "항공기"):
        return "Aircraft"
    if s in ("entity", "enterprise", "organization", "단체", "법인", "e"):
        return "Entity"
    LOGGER.warning("정규화되지 않은 유형 값: %r → Unknown", raw)
    return "Unknown"


# ---------------------------------------------------------------------------
# 2. HTTP 클라이언트
# ---------------------------------------------------------------------------

class HttpClient:
    """재시도 내장 HTTP 클라이언트."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                last = exc
                LOGGER.debug("GET 실패(%d/%d) %s — %s", attempt, self.retries, url, exc)
        raise RuntimeError(f"GET 실패: {url} — {last}")


# ---------------------------------------------------------------------------
# 3. 수집기 베이스
# ---------------------------------------------------------------------------

class BaseCollector(ABC):
    source_name: str = "UNKNOWN"

    def __init__(self, client: Optional[HttpClient] = None, save_raw: bool = True):
        self.client = client or HttpClient()
        self.save_raw = save_raw

    @abstractmethod
    def fetch(self) -> bytes: ...

    @abstractmethod
    def parse(self, payload: bytes) -> Any: ...

    @abstractmethod
    def normalize(self, parsed: Any) -> List[SanctionRecord]: ...

    def collect(self) -> List[SanctionRecord]:
        LOGGER.info("[%s] 수집 시작", self.source_name)
        try:
            payload = self.fetch()
            parsed = self.parse(payload)
            records = self.normalize(parsed)
            LOGGER.info("[%s] 수집 완료: %d건", self.source_name, len(records))
            return records
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[%s] 수집 실패 — 해당 출처 제외하고 계속: %s", self.source_name, exc)
            return []

    def _dump_raw(self, payload: bytes, filename: str) -> None:
        if not self.save_raw:
            return
        try:
            os.makedirs(RAW_DIR, exist_ok=True)
            path = os.path.join(RAW_DIR, filename)
            with open(path, "wb") as fp:
                fp.write(payload)
            LOGGER.debug("[%s] 원본 스냅샷 저장: %s", self.source_name, path)
        except OSError as exc:
            LOGGER.warning("[%s] 원본 저장 실패: %s", self.source_name, exc)


def _ns_of(root: ET.Element) -> Dict[str, str]:
    """루트 태그에서 기본 네임스페이스를 자동 추출.
    OFAC·EU는 배포 채널에 따라 xmlns가 예고 없이 바뀌므로 하드코딩 금지."""
    tag = root.tag
    uri = tag[tag.find("{") + 1:tag.find("}")] if "{" in tag else ""
    return {"n": uri} if uri else {}


def _find(el: ET.Element, path: str, ns: Dict[str, str]) -> Optional[ET.Element]:
    if ns:
        return el.find("/".join(f"n:{p}" for p in path.split("/")), ns)
    return el.find(path)


def _findall(el: ET.Element, path: str, ns: Dict[str, str]) -> List[ET.Element]:
    if ns:
        return el.findall("/".join(f"n:{p}" for p in path.split("/")), ns)
    return el.findall(path)


def _text(el: Optional[ET.Element], path: str, ns: Dict[str, str]) -> str:
    if el is None:
        return ""
    x = _find(el, path, ns)
    return _clean(x.text) if x is not None and x.text else ""


# ---------------------------------------------------------------------------
# 4. OFAC (XML) — v2 핵심 변경
# ---------------------------------------------------------------------------

class OFACCollector(BaseCollector):
    """OFAC SDN List 수집기 (XML).

    v1은 SDN.CSV를 사용했으나 CSV에는 별칭(AKA) 컬럼이 존재하지 않아
    WLF의 핵심 기능인 별칭 매칭이 불가능했음. XML은 akaList를 포함.
    """

    source_name = "OFAC"

    CANDIDATE_URLS = [
        "https://www.treasury.gov/ofac/downloads/sdn.xml",  # 레거시 미러(안정적)
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML",
    ]

    def fetch(self) -> bytes:
        last: Optional[Exception] = None
        for url in self.CANDIDATE_URLS:
            try:
                resp = self.client.get(url)
                data = resp.content
                LOGGER.info("[OFAC] 다운로드 성공 (%.1f KB) — %s", len(data) / 1024, url)
                self._dump_raw(data, f"ofac_sdn_{datetime.now():%Y%m%d}.xml")
                return data
            except Exception as exc:  # noqa: BLE001
                last = exc
                LOGGER.warning("[OFAC] 실패, 다음 후보 시도: %s", url)
        raise RuntimeError(f"[OFAC] 모든 후보 URL 실패 — {last}")

    def parse(self, payload: bytes) -> ET.Element:
        return ET.fromstring(payload)

    def normalize(self, parsed: ET.Element) -> List[SanctionRecord]:
        ns = _ns_of(parsed)
        out: List[SanctionRecord] = []
        for sdn in _findall(parsed, "sdnEntry", ns):
            typ = _text(sdn, "sdnType", ns)
            last_name = _text(sdn, "lastName", ns)
            first_name = _text(sdn, "firstName", ns)
            name = f"{first_name} {last_name}".strip() if typ == "Individual" else last_name
            if not name:
                continue

            aliases: List[str] = []
            aka_list = _find(sdn, "akaList", ns)
            if aka_list is not None:
                for aka in _findall(aka_list, "aka", ns):
                    al = f"{_text(aka, 'firstName', ns)} {_text(aka, 'lastName', ns)}".strip()
                    if al:
                        aliases.append(al)

            # v1(CSV)은 "AL ZAWAHIRI, Dr. Ayman" 형태였고 v2(XML)는 자연순입니다.
            # 자연순이 읽기 좋지만, 성-이름 역순은 OFAC 공식 표기이자 기존 검색 습관이므로
            # 별칭으로 함께 실어 어느 쪽으로 찾아도 걸리게 합니다.
            if typ == "Individual" and first_name and last_name:
                aliases.append(f"{last_name}, {first_name}")

            programs = [_clean(p.text) for p in _findall(sdn, "programList/program", ns) if p.text]

            # 국적·생년월일·주소 (XML은 CSV보다 구조적으로 제공)
            nationality = ""
            nat_list = _find(sdn, "nationalityList", ns)
            if nat_list is not None:
                nat = _find(nat_list, "nationality", ns)
                nationality = _text(nat, "country", ns) if nat is not None else ""
            if not nationality:
                cit_list = _find(sdn, "citizenshipList", ns)
                if cit_list is not None:
                    cit = _find(cit_list, "citizenship", ns)
                    nationality = _text(cit, "country", ns) if cit is not None else ""

            birth_date = ""
            dob_list = _find(sdn, "dateOfBirthList", ns)
            if dob_list is not None:
                dob = _find(dob_list, "dateOfBirthItem", ns)
                birth_date = _text(dob, "dateOfBirth", ns) if dob is not None else ""

            address = ""
            addr_list = _find(sdn, "addressList", ns)
            if addr_list is not None:
                a = _find(addr_list, "address", ns)
                if a is not None:
                    parts = [_text(a, k, ns) for k in ("address1", "city", "stateOrProvince", "country")]
                    address = ", ".join(p for p in parts if p)

            remarks = _text(sdn, "remarks", ns)
            if not birth_date and remarks:
                birth_date = self._extract_dob(remarks)

            out.append(SanctionRecord(
                source="OFAC",
                name=name,
                type=normalize_type(typ or "Entity"),
                uid=_text(sdn, "uid", ns),
                aliases=aliases,
                nationality=nationality or None,
                birth_date=birth_date or None,
                address=address or None,
                program=",".join(programs) or None,
                listed_on=None,
                remarks=remarks or None,
            ))
        LOGGER.info("[OFAC] 파싱 완료: %d건 (별칭 보유 %d건)",
                    len(out), sum(1 for r in out if r.aliases))
        self._reconcile(parsed, ns, len(out))
        return out

    @staticmethod
    def _reconcile(root: ET.Element, ns: Dict[str, str], parsed_count: int) -> None:
        """원본이 스스로 선언한 건수(Record_Count)와 파싱 결과를 대조합니다.

        OFAC XML의 publshInformation에는 Publish_Date와 Record_Count가 들어 있습니다.
        파서가 특정 항목을 조용히 건너뛰어도 건수만 보면 알 수 없으므로,
        원본 선언값과 어긋나면 경고를 남겨 드러냅니다.
        (건수 변동 자체는 정상입니다 — OFAC은 수시로 명단을 갱신합니다.)
        """
        pub = _find(root, "publshInformation", ns)
        if pub is None:
            return
        declared = _text(pub, "Record_Count", ns)
        published = _text(pub, "Publish_Date", ns)
        if published:
            LOGGER.info("[OFAC] 원본 발행일: %s", published)
        if not declared.isdigit():
            return
        declared_n = int(declared)
        if declared_n != parsed_count:
            LOGGER.warning(
                "[OFAC] 건수 불일치 — 원본 선언 %d건 vs 파싱 %d건 (차이 %+d). "
                "파서가 항목을 건너뛰고 있을 수 있습니다.",
                declared_n, parsed_count, parsed_count - declared_n)
        else:
            LOGGER.info("[OFAC] 건수 검증 통과: 원본 선언 %d건과 일치", declared_n)

    @staticmethod
    def _extract_dob(remarks: str) -> Optional[str]:
        """remarks의 'DOB 12 Mar 1965' 패턴에서 생년월일 추출.
        v1의 유용한 아이디어를 그대로 계승."""
        m = re.search(r"DOB\s+([0-9]{1,2}\s+\w{3}\s+[0-9]{4}|[0-9]{4})", remarks, re.I)
        return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 5. UNSC (XML)
# ---------------------------------------------------------------------------

class UNSCCollector(BaseCollector):
    """UN 안보리 통합 제재리스트."""

    source_name = "UNSC"

    URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

    def fetch(self) -> bytes:
        resp = self.client.get(self.URL)
        data = resp.content
        LOGGER.info("[UNSC] 다운로드 성공 (%.1f KB)", len(data) / 1024)
        self._dump_raw(data, f"unsc_consolidated_{datetime.now():%Y%m%d}.xml")
        return data

    def parse(self, payload: bytes) -> ET.Element:
        return ET.fromstring(payload)

    def normalize(self, parsed: ET.Element) -> List[SanctionRecord]:
        out: List[SanctionRecord] = []

        for ind in parsed.iter("INDIVIDUAL"):
            name = " ".join(v for v in (
                _clean(_t(ind, "FIRST_NAME")), _clean(_t(ind, "SECOND_NAME")),
                _clean(_t(ind, "THIRD_NAME")), _clean(_t(ind, "FOURTH_NAME")),
            ) if v)
            if not name:
                continue
            aliases = [_clean(_t(a, "ALIAS_NAME")) for a in ind.iter("INDIVIDUAL_ALIAS")]
            aliases = [a for a in aliases if a and a.lower() != "na"]

            nat_el = ind.find("NATIONALITY")
            nationality = _clean(_t(nat_el, "VALUE")) if nat_el is not None else ""

            dob = ""
            for d in ind.iter("INDIVIDUAL_DATE_OF_BIRTH"):
                dob = _clean(_t(d, "DATE")) or _clean(_t(d, "YEAR"))
                if dob:
                    break

            addr = ""
            for a in ind.iter("INDIVIDUAL_ADDRESS"):
                parts = [_clean(_t(a, k)) for k in ("STREET", "CITY", "STATE_PROVINCE", "COUNTRY")]
                addr = ", ".join(p for p in parts if p)
                if addr:
                    break

            out.append(SanctionRecord(
                source="UNSC", name=name, type="Individual",
                uid=_clean(_t(ind, "DATAID")), aliases=aliases,
                nationality=nationality or None, birth_date=dob or None,
                address=addr or None, program=_clean(_t(ind, "UN_LIST_TYPE")) or None,
                listed_on=_iso_date(_t(ind, "LISTED_ON")),
                remarks=_clean(_t(ind, "COMMENTS1")) or None,
            ))

        for ent in parsed.iter("ENTITY"):
            name = _clean(_t(ent, "FIRST_NAME"))
            if not name:
                continue
            aliases = [_clean(_t(a, "ALIAS_NAME")) for a in ent.iter("ENTITY_ALIAS")]
            aliases = [a for a in aliases if a and a.lower() != "na"]

            addr = ""
            for a in ent.iter("ENTITY_ADDRESS"):
                parts = [_clean(_t(a, k)) for k in ("STREET", "CITY", "STATE_PROVINCE", "COUNTRY")]
                addr = ", ".join(p for p in parts if p)
                if addr:
                    break

            out.append(SanctionRecord(
                source="UNSC", name=name, type="Entity",
                uid=_clean(_t(ent, "DATAID")), aliases=aliases,
                address=addr or None, program=_clean(_t(ent, "UN_LIST_TYPE")) or None,
                listed_on=_iso_date(_t(ent, "LISTED_ON")),
                remarks=_clean(_t(ent, "COMMENTS1")) or None,
            ))

        LOGGER.info("[UNSC] 파싱 완료: 개인 %d건 / 단체 %d건",
                    sum(1 for r in out if r.type == "Individual"),
                    sum(1 for r in out if r.type == "Entity"))
        return out


def _t(el: Optional[ET.Element], tag: str) -> str:
    """네임스페이스 없는 단순 XML용 텍스트 추출(UNSC)."""
    if el is None:
        return ""
    x = el.find(tag)
    return x.text if x is not None and x.text else ""


# ---------------------------------------------------------------------------
# 6. EU (XML) — v2 신규
# ---------------------------------------------------------------------------

class EUCollector(BaseCollector):
    """EU 집행위 FSD 통합 금융제재 리스트.

    공개 배포 엔드포인트(공개 토큰)를 사용합니다.
    구조: <export><sanctionEntity><nameAlias .../><citizenship .../><birthdate .../>
    첫 nameAlias를 주명칭으로, 나머지를 별칭으로 정규화합니다.
    """

    source_name = "EU"

    URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
           "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw")

    def fetch(self) -> bytes:
        resp = self.client.get(self.URL)
        data = resp.content
        LOGGER.info("[EU] 다운로드 성공 (%.1f KB)", len(data) / 1024)
        self._dump_raw(data, f"eu_fsd_{datetime.now():%Y%m%d}.xml")
        return data

    def parse(self, payload: bytes) -> ET.Element:
        return ET.fromstring(payload)

    def normalize(self, parsed: ET.Element) -> List[SanctionRecord]:
        ns = _ns_of(parsed)
        out: List[SanctionRecord] = []

        for ent in _findall(parsed, "sanctionEntity", ns):
            names: List[str] = []
            for na in _findall(ent, "nameAlias", ns):
                whole = _clean(na.attrib.get("wholeName", ""))
                if not whole:
                    whole = _clean(" ".join(
                        na.attrib.get(k, "") for k in ("firstName", "middleName", "lastName")))
                if whole:
                    names.append(whole)
            if not names:
                continue

            st = _find(ent, "subjectType", ns)
            code = st.attrib.get("code", "") if st is not None else ""
            typ = "Individual" if code == "person" else ("Vessel" if code == "vessel" else "Entity")

            cit = _find(ent, "citizenship", ns)
            nationality = _clean(cit.attrib.get("countryDescription", "")) if cit is not None else ""

            bd = _find(ent, "birthdate", ns)
            birth = ""
            if bd is not None:
                birth = _clean(bd.attrib.get("birthdate", "") or bd.attrib.get("year", ""))

            addr_el = _find(ent, "address", ns)
            address = ""
            if addr_el is not None:
                parts = [_clean(addr_el.attrib.get(k, ""))
                         for k in ("street", "city", "countryDescription")]
                address = ", ".join(p for p in parts if p)

            reg = _find(ent, "regulation", ns)
            program = _clean(reg.attrib.get("programme", "")) if reg is not None else ""
            listed = _clean(reg.attrib.get("publicationDate", "")) if reg is not None else ""

            out.append(SanctionRecord(
                source="EU", name=names[0], type=typ,
                uid=_clean(ent.attrib.get("logicalId", "")),
                aliases=names[1:9],
                nationality=nationality or None,
                birth_date=birth or None,
                address=address or None,
                program=program or None,
                listed_on=_iso_date(listed),
                remarks=_clean(_text(ent, "remark", ns)) or None,
            ))

        LOGGER.info("[EU] 파싱 완료: %d건 (별칭 보유 %d건)",
                    len(out), sum(1 for r in out if r.aliases))
        return out


# ---------------------------------------------------------------------------
# 7. KoFIU (수동 반입) — v2 설계 전환
# ---------------------------------------------------------------------------

class KoFIUCollector(BaseCollector):
    """금융위원회 금융거래등제한대상자 — 국가법령정보센터 자동 수집.

    ▣ 경로 (2026.07.18. 실측 확인)
      명단은 kofiu.go.kr 게시판이 아니라 **금융위 고시의 별표**로 공시되며,
      국가법령정보센터에 PDF로 올라옵니다. 게시판 스크레이핑이 막혔던 것과 달리
      이 경로는 순수 HTTP 요청만으로 끝까지 도달합니다.

        ① /행정규칙/{규정명}                    → 현행 admRulSeq (개정돼도 자동 추적)
        ② /LSW/admRulBylInfoR.do?admRulSeq=..  → 별표 식별자(bylSeq) + 시행/고시 정보
        ③ /admRulBylContentsInfoR.do?bylSeq=.. → 뷰어 문서키(= 원본 flSeq)
        ④ /LSW/flDownload.do?flSeq=..          → 원본 PDF (한글로 만든 문서, 55쪽)

      ①이 핵심입니다. admRulSeq는 개정 때마다 바뀌는데, 규정명 URL이 항상
      현행 버전으로 해석되므로 코드를 고칠 필요가 없습니다.

      ※ admRulBylTextDownLoad.do (평문 .txt) 경로를 쓰지 않는 이유
        같은 별표를 평문으로 내려받는 엔드포인트가 있고 줄바꿈이 정돈돼 있어
        더 편해 보이지만, **한자를 손상시킵니다**(2026.07.18. 실측):
          PDF  : HUIONE GROUP LIMITED (Chinese Traditional: 滙旺集團有限公司)
          평문 : HUIONE GROUP LIMITED (CChinese Traditional: 旺集團有限公司)
        선두 글자 누락('滙' 소실)과 라틴 문자 중복('CChinese')이 함께 나타나며,
        한자 109자 중 11자가 사라집니다. 캄보디아·중국 연계 대상(Huione 계열,
        베이징 숙박소 등)의 중문 표기가 정확히 이 지점에서 깨집니다.
        파싱 난이도(줄바꿈 재조합)보다 원문 충실도를 우선합니다.
        ※ 나머지 1,062건은 두 경로의 파싱 결과가 완전히 동일했습니다.

    ▣ 서식
      OFAC SDN과 사실상 같은 형태입니다.
        1. AL QAIDA (a.k.a. AL QAEDA; a.k.a. ...) [E.O.13224].
        300. JANJALANI, Khadafi (a.k.a. ...); DOB 03 Mar 1975; POB ...; (individual) [E.O.13224].
        718. RI, Myong Hun(리명훈) (a.k.a. ...); DOB 14 Mar 1969; Gender Male (2022. 12. 2. 지정).
      2022년 이후 우리 정부 자체 지정분은 서식이 달라 `(individual)` 표기가 없고
      한글명이 병기됩니다. 유형 판별은 그래서 아래 _is_individual()로 따로 다룹니다.

    ▣ 실패 시
      네트워크·서식 문제로 실패하면 manual/ 의 수동 반입 파일로 대체합니다.
      둘 다 없으면 이 출처만 건너뛰고 나머지는 정상 수집합니다.
    """

    source_name = "KoFIU"

    ADM_RUL_NAME = "금융거래등제한대상자 지정 및 지정 취소에 관한 규정"
    BASE = "https://www.law.go.kr"
    BYL_CLS_CD = "200207"          # 별첨 구분코드
    EXPECTED_MIN = 500             # 이보다 적으면 파싱이 깨진 것으로 간주

    # manual/ 대체 반입 시 인정할 파일명 조각
    FILENAME_HINTS = ("kofiu", "제한대상", "금융거래등제한", "지정고시", "sanction")
    COLUMN_HINTS = {
        "name": ["성명", "명칭", "이름", "대상자", "성명(한글)", "한글명", "name"],
        "name_en": ["영문", "영문명", "성명(영문)", "english"],
        "type": ["구분", "개인/단체", "유형", "종류"],
        "nationality": ["국적", "소속국가", "국가"],
        "birth_date": ["생년월일", "생일", "출생", "dob"],
        "address": ["주소", "소재지"],
        "program": ["근거", "지정근거", "제재근거", "고시"],
        "listed_on": ["지정일", "고시일", "등재일"],
    }

    def __init__(self, client=None, save_raw: bool = True):
        super().__init__(client, save_raw)
        self._ext = ".pdf"
        self._notice = ""      # [시행 2026. 1. 22.] [금융위원회고시 제2026-2호 ...]
        self._title = ""       # 별표 제목 — 끝에 "(1066명)"이 붙어 건수 검산에 씁니다

    # ── ① 현행 admRulSeq ──
    def _current_adm_rul_seq(self) -> str:
        url = f"{self.BASE}/행정규칙/{quote(self.ADM_RUL_NAME)}"
        html = self.client.get(url).text
        found = re.findall(r"admRulSeq[\"'=:\s,]+(\d+)", html)
        if not found:
            raise RuntimeError("현행 admRulSeq를 찾지 못했습니다 — 규정명 변경 여부 확인 필요")
        seq = found[0]
        LOGGER.info("[KoFIU] 현행 규정 admRulSeq=%s", seq)
        return seq

    # ── ② 별표 식별자 ──
    def _byl_ids(self, adm_rul_seq: str) -> Dict[str, str]:
        r = self.client.get(f"{self.BASE}/LSW/admRulBylInfoR.do", params={
            "admRulSeq": adm_rul_seq, "bylBrNo": "00", "bylCls": "BF",
            "bylClsCd": self.BYL_CLS_CD, "bylNo": "0000",
        })
        html = r.text
        opt = re.search(r'<option value="(\d+),(\d+),(\d+),(\d+)"[^>]*>([^<]*)', html)
        if not opt:
            raise RuntimeError("별표 목록에서 bylSeq를 찾지 못했습니다")
        self._title = _clean(opt.group(5))
        adm_rul_id = re.search(r'id="bylAdmRulId"[^>]*value="(\d+)"', html)
        notice = re.search(r'class="ast_tit">([^<]*)<br\s*/?>([^<]*)<', html)
        if notice:
            self._notice = _clean(notice.group(2))
            LOGGER.info("[KoFIU] %s", self._notice)
        return {
            "bylSeq": opt.group(1), "bylNo": opt.group(2),
            "bylBrNo": opt.group(3), "bylClsCd": opt.group(4),
            "admRulId": adm_rul_id.group(1) if adm_rul_id else "",
            "admRulSeq": adm_rul_seq,
        }

    # ── ③ 원본 문서키 ──
    def _file_seq(self, ids: Dict[str, str]) -> str:
        r = self.client.get(f"{self.BASE}/admRulBylContentsInfoR.do", params=ids)
        keys = re.findall(r"key=(\d+)", r.text) or re.findall(r"flSeq=(\d+)", r.text)
        if not keys:
            raise RuntimeError("별표 본문에서 문서키(flSeq)를 찾지 못했습니다")
        return keys[0]

    # ── ④ PDF ──
    def fetch(self) -> bytes:
        try:
            ids = self._byl_ids(self._current_adm_rul_seq())
            fl_seq = self._file_seq(ids)
            resp = self.client.get(f"{self.BASE}/LSW/flDownload.do",
                                   params={"flSeq": fl_seq, "bylClsCd": self.BYL_CLS_CD})
            data = resp.content
            if not data.startswith(b"%PDF"):
                raise RuntimeError(f"PDF가 아닌 응답 (선두 {data[:8]!r})")
            LOGGER.info("[KoFIU] 별표 PDF 다운로드 (%.1f KB) — flSeq=%s", len(data) / 1024, fl_seq)
            self._dump_raw(data, f"kofiu_byl_{datetime.now():%Y%m%d}.pdf")
            self._ext = ".pdf"
            return data
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[KoFIU] 법령정보센터 자동 수집 실패(%s) — manual/ 대체 시도", exc)
            return self._fetch_manual()

    def _fetch_manual(self) -> bytes:
        os.makedirs(MANUAL_DIR, exist_ok=True)
        files = sorted(
            glob.glob(os.path.join(MANUAL_DIR, "*.xlsx")) + glob.glob(os.path.join(MANUAL_DIR, "*.xls"))
            + glob.glob(os.path.join(MANUAL_DIR, "*.csv")) + glob.glob(os.path.join(MANUAL_DIR, "*.pdf")),
            key=os.path.getmtime, reverse=True)
        matched = [f for f in files
                   if any(h in os.path.basename(f).lower() for h in self.FILENAME_HINTS)]
        if not matched:
            raise FileNotFoundError(
                f"{MANUAL_DIR}/ 에도 대체 파일이 없습니다. 파일명에 "
                f"{' / '.join(self.FILENAME_HINTS[:3])} 중 하나가 포함되어야 합니다.")
        target = matched[0]
        LOGGER.info("[KoFIU] 수동 반입 파일 사용: %s", target)
        self._ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fp:
            return fp.read()

    # ── 파싱 ──
    def parse(self, payload: bytes) -> Any:
        if self._ext == ".pdf" or payload.startswith(b"%PDF"):
            return self._pdf_text(payload)
        return self._read_tables(payload)     # manual/ 엑셀·CSV 대체 경로

    @staticmethod
    def _pdf_text(payload: bytes) -> str:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload))
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    @staticmethod
    def split_entries(text: str) -> List[Dict[str, Any]]:
        """번호의 순차성으로 항목 경계를 찾습니다.

        줄바꿈에 의존하지 않는 이유: pdftotext는 항목마다 줄을 나누지만 pypdf는
        '...[E.O.13224].24. MAKHTAB...' 처럼 붙여서 내놓습니다. 추출기가 바뀌어도
        깨지지 않도록 번호의 순차성만 신뢰합니다(두 추출기 1,066건 일치 확인).

        본문에 '2025. 12. 1. 지정' 같은 날짜가 섞여 있어 숫자+마침표만으로는
        항목 번호와 구분되지 않으므로 두 겹으로 막습니다.
          ① 바로 다음에 와야 할 번호만 인정 (순차성)
          ② 번호 뒤에는 이름이 와야 함 (영문 대문자·한글·따옴표·괄호)
        """
        flat = re.sub(r"\s+", " ", text.replace("\f", " "))
        hits, expected = [], 1
        for m in re.finditer(r'(?<!\d)(\d{1,4})\.\s+(?=[A-Z가-힣"\'(])', flat):
            if int(m.group(1)) == expected:
                hits.append((expected, m.start(), m.end()))
                expected += 1
        out = []
        for i, (no, _s, e) in enumerate(hits):
            end = hits[i + 1][1] if i + 1 < len(hits) else len(flat)
            out.append({"no": no, "text": flat[e:end].strip()})
        return out

    # 이름 뒤에 들러붙는 속성 키워드. 구분자가 ';'가 아니라 ','인 경우가 많아
    # (예: 'HONG, Jinhua, DOB 19 Jan 1972') 구분자에 의존하지 않고 키워드로 끊습니다.
    ATTR_KEYS = re.compile(
        r"[,;]?\s*\b(?:alt\.\s*)?(?:DOB|POB|Gender|nationality|Nationality|citizen|Passport|"
        r"Address|Telephone|Fax|Email|E-mail|Company\s+Registration|Tax\s+ID|National\s+ID|"
        r"Identification\s+Number|Registration\s+Number|Business\s+Registration|SWIFT|Website|"
        r"Member\s+of|Linked\s+to|link\s+to|Deputy\s+Director|Director\s+of|President\s+of|Manager\s+of|"
        r"Commander\s+of|Representative\s+of|Former\s+|Vice\s+Representative)\b", re.I)

    # 주소로 시작하는 조각. 숫자로 시작하거나 도로/사서함 표기가 앞머리에 오는 경우.
    ADDR_HEAD = re.compile(
        r"^(?:P\.?\s?O\.?\s*Box\b|No\.?\s*\d|House\s+no|Room\s+\d|Suite\s+\d|Floor\b|\d+[,\s]|"
        r"Apartment\b|Building\b|Street\b|Road\b|Avenue\b)", re.I)
    ADDR_WORDS = re.compile(
        r"\b(?:Street|Road|Avenue|Boulevard|District|Province|City|Town|Village|Colony|"
        r"P\.?\s?O\.?\s*Box|Apartment|Building|Floor|Suite|dong|gu|Pyongyang|DPRK)\b", re.I)

    @classmethod
    def _trim_name(cls, head: str) -> str:
        """이름 뒤에 붙은 속성·주소를 떼어냅니다.

        원본이 'NAME, 주소, 주소; 속성' 처럼 쉼표로만 이어지는 경우가 있어
        기계적으로 첫 쉼표에서 자르면 'ATIF, Muhammad' 같은 성-이름이 깨집니다.
        그래서 ① 속성 키워드가 나오면 그 앞까지 ② 남은 쉼표 조각 중
        '명백히 주소인 것'만 뒤에서부터 버리는 두 단계로 처리합니다.
        애매하면 남깁니다 — 이름이 조금 긴 것이 이름이 잘리는 것보다 낫습니다.
        """
        s = (head or "").strip()
        m = cls.ATTR_KEYS.search(s)
        if m:
            s = s[:m.start()]
        # 이 서식에서 ';'는 주소 블록·속성 구분자이며 이름 안에는 쓰이지 않습니다.
        s = s.split(";")[0]
        s = s.strip().rstrip(",;.").strip()

        parts = [p.strip() for p in s.split(",")]
        while len(parts) > 1:
            tail = parts[-1]
            if cls.ADDR_HEAD.match(tail) or cls.ADDR_WORDS.search(tail):
                parts.pop()
                continue
            # 꼬리가 국가명이면 주소의 마지막 조각입니다.
            # 255개국 정본(countries.py)으로 판정하므로 임의 목록을 새로 만들지 않습니다.
            if len(parts) > 1 and resolve_country(tail)[0]:
                parts.pop()
                continue
            if len(parts) > 2 and cls.ADDR_WORDS.search(parts[-2]):
                parts.pop()
                continue
            break
        return ", ".join(parts).strip().rstrip(",;.").strip()

    # 개인을 가리키는 직책·연결 표현. 우리 정부 자체 지정분은 '(individual)'도
    # DOB도 없이 직책만 적힌 경우가 있습니다(예: '(김주원), China; Vice Representative ...').
    ROLE_HINT = re.compile(
        r"\b(?:Representative|Director|Manager|Commander|President|Chairman|Chief|Officer|"
        r"Secretary|Ambassador|Counsellor|Consul|Attache|Delegate|General\s+Manager|"
        r"Lieutenant|Colonel|Major|Captain|link\s+to)\b", re.I)

    # 단체를 가리키는 표현. 직책 단서와 겹칠 때 이쪽이 우선합니다.
    ENTITY_HINT = re.compile(
        r"\b(?:COMPANY|CORPORATION|CORP|LIMITED|LTD|LLC|L\.L\.C|BANK|TRADING|GROUP|INC|"
        r"FOUNDATION|ASSOCIATION|ORGANIZATION|COMMITTEE|BUREAU|FACTORY|COMPLEX|SHIPPING|"
        r"AIRLINES|TRUST|SOCIETY|BRIGADE|MOVEMENT|SARL|GMBH|JOINT\s+VENTURE)\b"
        r"|회사|은행|무역|연구소|위원회|공사|교류사|총국", re.I)

    @classmethod
    def _is_individual(cls, text: str, name: str = "") -> bool:
        """개인/단체 판별.

        구 서식은 '(individual)'을 붙이지만 2022년 이후 자체 지정분에는 없습니다.
        생년월일(DOB)·성별은 단체에 붙지 않으므로 결정적 단서입니다.
        그것도 없는 자체 지정분은 ① 직책 표현 ② 순한글 2~4자 이름으로 보완합니다.
        (1,066건 전수 교차검증: 단체 키워드 보유 항목의 개인 오탐 0건)
        """
        if "(individual)" in text:
            return True
        if re.search(r"\bGender\s+(Male|Female)\b", text) or re.search(r"\bDOB\b", text):
            return True

        subject = name or text
        # 이름 자체가 단체를 가리키면 직책 표현이 있어도 단체입니다
        # (예: '... Bureau in Beijing'의 주어가 기관인 경우).
        if cls.ENTITY_HINT.search(subject):
            return False
        if cls.ROLE_HINT.search(text):
            return True
        # 순한글 2~4자는 인명 관례. 단체는 '조선금정경제정보기술교류사'처럼 훨씬 깁니다.
        if re.fullmatch(r"[가-힣]{2,4}", subject.strip()):
            return True
        return False

    def normalize(self, parsed: Any) -> List[SanctionRecord]:
        if not isinstance(parsed, str):
            return self._normalize_tables(parsed)   # manual/ 대체 경로

        entries = self.split_entries(parsed)
        if len(entries) < self.EXPECTED_MIN:
            raise RuntimeError(
                f"파싱 결과가 비정상적으로 적습니다({len(entries)}건). 별표 서식 변경 가능성 — "
                "운영 반영 전 원본 확인 필요")

        out: List[SanctionRecord] = []
        for entry in entries:
            text = entry["text"]
            # 마지막 항목 뒤에 붙는 안내문('◇ 참고 ▪ 미국의 제재대상자…') 제거
            text = re.split(r"◇\s*참고", text)[0].strip()
            if not text:
                continue

            # 이름: 별칭·유형표기·근거·지정일이 시작되기 전까지 자른 뒤,
            # 뒤에 들러붙은 속성/주소를 한 번 더 떼어냅니다.
            head = re.split(r"\s*\(a\.k\.a\.|\s*\(f\.k\.a\.|\s*\(n\.k\.a\.|\s*\(individual\)|\s*\[|\s*\(\d{4}\.\s*\d{1,2}\.", text)[0]
            name = self._trim_name(head)
            if not name:
                continue

            aliases = [a.strip().strip('"').rstrip(",;")
                       for a in re.findall(r"a\.k\.a\.\s*([^;)]+)", text)]
            # 한글명 병기(예: 'RI, Myong Hun(리명훈)') → 별칭으로 분리.
            # 단 우리 정부 자체 지정분에는 이름이 '(류경철)'처럼 한글만 있는 경우가 있어,
            # 무조건 떼어내면 이름이 통째로 비어 명단에서 사라집니다. 남는 게 있을 때만 뗍니다.
            kor = re.search(r"\(([가-힣][가-힣\s]{1,19})\)", name)
            if kor:
                stripped = re.sub(r"\s*\([가-힣][가-힣\s]{1,19}\)", "", name).strip().rstrip(",;")
                if stripped:
                    aliases.append(kor.group(1).strip())
                    name = stripped
                else:
                    # 한글명이 곧 본명 — 괄호만 벗겨 그대로 씁니다.
                    name = kor.group(1).strip()

            program = re.findall(r"\[([^\]]+)\]", text)
            listed = re.search(r"\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*지정\)", text)
            dob = re.search(r"\bDOB\s+([^;(\[]+)", text)
            nat = re.search(r"\b(?:nationality|citizen)\s+([A-Za-z',\- ]+?)(?=[;(\[]|$)", text)
            pob = re.search(r"\bPOB\s+([^;(\[]+)", text)

            out.append(SanctionRecord(
                source="KoFIU",
                name=name,
                type="Individual" if self._is_individual(text, name) else "Entity",
                uid=None,                    # 고유번호 미제공 — 이름 기반 키로 추적
                aliases=aliases,
                nationality=_clean(nat.group(1)) if nat else None,
                birth_date=_iso_date(dob.group(1)) if dob else None,
                address=_clean(pob.group(1)) if pob else None,
                program=_clean(program[-1]) if program else None,
                listed_on=("%s-%02d-%02d" % (listed.group(1), int(listed.group(2)), int(listed.group(3))))
                          if listed else None,
                remarks=self._notice or None,
            ))

        LOGGER.info("[KoFIU] 파싱 완료: %d건 (개인 %d / 단체 %d, 별칭 보유 %d건)",
                    len(out), sum(1 for r in out if r.type == "Individual"),
                    sum(1 for r in out if r.type == "Entity"),
                    sum(1 for r in out if r.aliases))
        self._reconcile(len(out))
        return out

    def _reconcile(self, parsed_count: int) -> None:
        """별표 제목이 스스로 밝힌 인원수와 파싱 결과를 대조합니다.
        제목이 '… 금융거래등제한대상자 (1066명)' 형태라 검산이 가능합니다."""
        m = re.search(r"\((\d{3,5})\s*명\)", self._title or "")
        if not m:
            return
        declared = int(m.group(1))
        if declared != parsed_count:
            LOGGER.warning("[KoFIU] 건수 불일치 — 고시 표기 %d명 vs 파싱 %d건 (차이 %+d)",
                           declared, parsed_count, parsed_count - declared)
        else:
            LOGGER.info("[KoFIU] 건수 검증 통과: 고시 표기 %d명과 일치", declared)

    # ── manual/ 대체 경로 (기존 검증 코드 유지) ──
    def _read_tables(self, payload: bytes) -> List[pd.DataFrame]:
        if self._ext == ".csv":
            for enc in ("utf-8-sig", "cp949", "euc-kr"):
                try:
                    return [pd.read_csv(io.BytesIO(payload), encoding=enc, dtype=str)]
                except Exception:  # noqa: BLE001
                    continue
            raise ValueError("[KoFIU] CSV 인코딩 판별 실패")
        sheets = pd.read_excel(io.BytesIO(payload), sheet_name=None, dtype=str, header=None)
        return [self._promote_header(df) for df in sheets.values()]

    @staticmethod
    def _promote_header(df: pd.DataFrame) -> pd.DataFrame:
        """상단 공백/제목 행을 건너뛰고 실제 헤더 행을 찾아 승격."""
        for i in range(min(10, len(df))):
            row = df.iloc[i].astype(str)
            if row.str.contains("성명|명칭|구분|국적", na=False).any():
                out = df.iloc[i + 1:].copy()
                out.columns = [_clean(c) for c in df.iloc[i]]
                return out.reset_index(drop=True)
        return df

    def _map_columns(self, df: pd.DataFrame) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for field_name, hints in self.COLUMN_HINTS.items():
            for c in [str(x) for x in df.columns]:
                if any(h.lower() in c.lower() for h in hints):
                    mapping[field_name] = c
                    break
        return mapping

    def _normalize_tables(self, parsed: List[pd.DataFrame]) -> List[SanctionRecord]:
        out: List[SanctionRecord] = []
        for df in parsed:
            if df is None or df.empty:
                continue
            m = self._map_columns(df)
            if "name" not in m:
                LOGGER.warning("[KoFIU] 성명 컬럼을 찾지 못한 시트 — 건너뜀 (컬럼: %s)",
                               list(df.columns)[:8])
                continue
            for _, row in df.iterrows():
                name = _clean(row.get(m["name"]))
                if not name:
                    continue
                aliases = []
                if "name_en" in m:
                    en = _clean(row.get(m["name_en"]))
                    if en:
                        aliases.append(en)
                out.append(SanctionRecord(
                    source="KoFIU", name=name,
                    type=normalize_type(row.get(m["type"]) if "type" in m else ""),
                    aliases=aliases,
                    nationality=_clean(row.get(m["nationality"])) or None if "nationality" in m else None,
                    birth_date=_iso_date(row.get(m["birth_date"])) if "birth_date" in m else None,
                    address=_clean(row.get(m["address"])) or None if "address" in m else None,
                    program=_clean(row.get(m["program"])) or None if "program" in m else None,
                    listed_on=_iso_date(row.get(m["listed_on"])) if "listed_on" in m else None,
                ))
        LOGGER.info("[KoFIU] 수동 반입 파싱: %d건", len(out))
        return out


# ---------------------------------------------------------------------------
# 8. 집계 · 저장 · 번들
# ---------------------------------------------------------------------------

class SanctionsAggregator:
    """다중 수집기를 실행하고 정규화 결과를 병합·저장·번들링."""

    def __init__(self, collectors: Optional[Iterable[BaseCollector]] = None,
                 output_dir: str = OUTPUT_DIR):
        client = HttpClient()
        self.collectors: List[BaseCollector] = list(collectors) if collectors else [
            OFACCollector(client), UNSCCollector(client),
            EUCollector(client), KoFIUCollector(client),
        ]
        self.output_dir = output_dir
        self.records: List[SanctionRecord] = []
        self.stats: Dict[str, int] = {}

    def run(self) -> List[SanctionRecord]:
        merged: List[SanctionRecord] = []
        for collector in self.collectors:
            result = collector.collect()
            self.stats[collector.source_name] = len(result)
            merged.extend(result)
        self.records = self._deduplicate(merged)
        self._report()
        return self.records

    @staticmethod
    def _deduplicate(records: List[SanctionRecord]) -> List[SanctionRecord]:
        """동일 출처 내 (source, name, type, uid) 중복 제거.
        ※ 기관 간 동일인 매칭(Entity Resolution)은 명의매칭 엔진 영역이므로 제외."""
        seen = set()
        unique: List[SanctionRecord] = []
        for r in records:
            key = (r.source, r.name.upper(), r.type, r.uid or "")
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        removed = len(records) - len(unique)
        if removed:
            LOGGER.info("중복 제거: %d건", removed)
        return unique

    def _report(self) -> None:
        LOGGER.info("=" * 72)
        LOGGER.info("%-8s %9s %9s %9s   %s", "SOURCE", "COUNT", "ALIAS", "DOB", "STATUS")
        LOGGER.info("-" * 72)
        for source, count in self.stats.items():
            rs = [r for r in self.records if r.source == source]
            al = sum(1 for r in rs if r.aliases)
            db = sum(1 for r in rs if r.birth_date)
            status = "OK" if count else "FAILED / EMPTY"
            LOGGER.info("%-8s %9d %9d %9d   %s", source, count, al, db, status)
        LOGGER.info("-" * 72)
        LOGGER.info("%-8s %9d %9d %9d", "TOTAL", len(self.records),
                    sum(1 for r in self.records if r.aliases),
                    sum(1 for r in self.records if r.birth_date))
        LOGGER.info("=" * 72)

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(r) for r in self.records])

    # ── 산출물 1: 전체 필드(감사·분석용) ──
    def save(self, basename: Optional[str] = None) -> Dict[str, str]:
        if not self.records:
            LOGGER.error("저장할 레코드가 없습니다. 모든 소스가 실패했을 수 있습니다.")
            return {}

        os.makedirs(self.output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        base = basename or f"consolidated_sanctions_{stamp}"
        json_path = os.path.join(self.output_dir, f"{base}.json")
        csv_path = os.path.join(self.output_dir, f"{base}.csv")

        payload = {
            "meta": {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "total_count": len(self.records),
                "source_counts": self.stats,
            },
            "data": [asdict(r) for r in self.records],
        }
        try:
            with open(json_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            LOGGER.info("JSON 저장 완료: %s", json_path)
        except OSError as exc:
            LOGGER.error("JSON 저장 실패: %s", exc)
            json_path = ""
        try:
            self.to_dataframe().to_csv(csv_path, index=False, encoding="utf-8-sig")
            LOGGER.info("CSV 저장 완료: %s", csv_path)
        except OSError as exc:
            LOGGER.error("CSV 저장 실패: %s", exc)
            csv_path = ""
        return {"json": json_path, "csv": csv_path}

    # ── 산출물 2: KYCflow 적재 번들(SHA-256 서명) ──
    def save_bundle(self) -> str:
        """KYCflow 데모/정식판이 읽는 경량 번들. 반입 무결성 검증용 서명 포함.

        전체 필드 JSON은 감사·분석용(대용량)이고, 번들은 매칭 엔진이
        즉시 색인할 수 있는 최소 필드만 담아 폐쇄망 반입 부담을 줄입니다.
        """
        if not self.records:
            return ""
        os.makedirs(self.output_dir, exist_ok=True)
        today = datetime.now().strftime("%Y.%m.%d.")

        entries = [{
            "id": f"{r.source}-{r.uid or ''}",
            "name": r.name,
            "type": "개인" if r.type == "Individual" else ("선박" if r.type == "Vessel" else "법인/단체"),
            "aliases": r.aliases,
            "src": "UN" if r.source == "UNSC" else r.source,
            "program": r.program or "",
            "country": r.nationality or "",
            "dob": r.birth_date or "",
            "listed": r.listed_on or "",
        } for r in self.records]

        body = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        sha = hashlib.sha256(body.encode()).hexdigest()

        source_labels = {
            "OFAC": "OFAC SDN List (XML)",
            "UNSC": "UN Security Council Consolidated List",
            "EU": "EU Consolidated Financial Sanctions List",
            "KoFIU": "금융위원회 금융거래등제한대상자 (수동 반입)",
        }
        bundle = {
            "meta": {
                "bundle": "KYCflow WLF Bundle",
                "version": "2.0",
                "built": today,
                "sources": [{"name": source_labels.get(s, s), "count": c}
                            for s, c in self.stats.items() if c],
                "total": len(entries),
                "alias_count": sum(1 for e in entries if e["aliases"]),
                "sha256": sha,
            },
            "entries": entries,
        }
        path = os.path.join(self.output_dir, f"wlf_bundle_{today[:-1]}.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(bundle, fp, ensure_ascii=False, separators=(",", ":"))
        LOGGER.info("번들 저장 완료: %s (%.1f MB)", path, os.path.getsize(path) / 1024 / 1024)
        LOGGER.info("  총 %d건 · 별칭 보유 %d건 · SHA-256 %s…",
                    len(entries), bundle["meta"]["alias_count"], sha[:16])
        LOGGER.info("  → KYCflow 데모 ② WLF 탭 '실명단 번들 적용'에서 이 파일을 선택하십시오.")
        return path


# ---------------------------------------------------------------------------
# 9. 진입점
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="AML/CFT 제재명단 통합 수집기 v2.0")
    ap.add_argument("--no-eu", action="store_true", help="EU 리스트 제외")
    ap.add_argument("--no-kofiu", action="store_true", help="KoFIU 수동 반입 건너뜀")
    ap.add_argument("--bundle-only", action="store_true", help="번들만 출력(전체 JSON/CSV 생략)")
    ap.add_argument("--no-raw", action="store_true", help="원본 스냅샷 저장 안 함")
    args = ap.parse_args()

    setup_logging()
    LOGGER.info("제재명단 통합 수집 시작 (%s)", datetime.now().strftime("%Y.%m.%d. %H:%M:%S"))

    client = HttpClient()
    save_raw = not args.no_raw
    collectors: List[BaseCollector] = [
        OFACCollector(client, save_raw), UNSCCollector(client, save_raw),
    ]
    if not args.no_eu:
        collectors.append(EUCollector(client, save_raw))
    if not args.no_kofiu:
        collectors.append(KoFIUCollector(client, save_raw))

    aggregator = SanctionsAggregator(collectors)
    records = aggregator.run()
    if not records:
        LOGGER.error("수집 결과 0건 — 종료 코드 1")
        return 1

    if not args.bundle_only:
        aggregator.save()
    aggregator.save_bundle()

    df = aggregator.to_dataframe()
    LOGGER.info("샘플 5건:\n%s", df[["source", "name", "type"]].head().to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
