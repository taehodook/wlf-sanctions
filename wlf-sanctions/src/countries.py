"""countries.py — 국적 표기 정규화

출처마다 국가 표기가 제각각입니다(실측 235종).
  OFAC : "Korea, North"      UNSC : "Democratic People's Republic of Korea"
  EU   : "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF"
같은 나라가 통계에서 갈라지고 필터도 어긋나므로, ISO alpha2 기준으로 통일합니다.

기준 테이블은 AML 국가위험평가 서비스의 255개국 정본(alpha2/alpha3/영문/한글)을
그대로 씁니다. 새 매핑을 따로 만들면 두 제품의 국가 표기가 갈라집니다.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Dict, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_TABLE_PATH = os.path.join(_HERE, "countries.json")

with open(_TABLE_PATH, encoding="utf-8") as _fp:
    COUNTRIES = json.load(_fp)

BY_A2: Dict[str, dict] = {c["alpha2"]: c for c in COUNTRIES}


def _key(s: str) -> str:
    """비교용 키: 발음기호 분해, 소문자, 괄호/구두점 제거, 공백 정리.

    아포스트로피는 공백이 아니라 '삭제'해야 합니다.
    공백으로 바꾸면 "People's" → "people s" 가 되어 "peoples"와 어긋납니다.
    """
    s = s or ""
    # Türkiye → Turkiye, Åland → Aland (발음기호만 제거)
    # NFKD는 한글도 자모로 분해하므로 반드시 NFC로 되돌려야 합니다.
    # 안 하면 "북한"이 자모로 쪼개져 아래 정규식에서 통째로 사라집니다.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = unicodedata.normalize("NFC", s)
    s = s.lower()
    s = re.sub(r"\(.*?\)", " ", s)          # "(the)", "(was Zaire)" 등 제거
    s = s.replace("&", " and ")
    s = re.sub(r"['’`]", "", s)             # 아포스트로피는 삭제
    s = re.sub(r"[^a-z가-힣0-9 ]", " ", s)   # 나머지 구두점은 공백
    s = re.sub(r"\b(the|of|republic|state|states|island|islands)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# 정본 영문명 → alpha2
_EXACT: Dict[str, str] = {}
for _c in COUNTRIES:
    _EXACT.setdefault(_key(_c["eng"]), _c["alpha2"])
    # KoFIU 명단은 국적을 한글로 적습니다("북한", "이란").
    # 정본 테이블의 kor_name도 같은 색인에 넣어 두 언어를 모두 받습니다.
    _EXACT.setdefault(_key(_c["kor"]), _c["alpha2"])

# 위 규칙으로 안 잡히는 실제 표기들. 실측 235종을 대조해 만든 표입니다.
ALIASES: Dict[str, str] = {
    # 한반도
    "korea north": "KP", "north korea": "KP", "dprk": "KP",
    "democratic peoples korea": "KP", "korea democratic peoples": "KP",
    "korea south": "KR", "south korea": "KR", "korea": "KR",
    # 러시아·구소련
    "russia": "RU", "russian federation": "RU", "russian": "RU",
    "moldova": "MD", "moldova": "MD",
    # 중동
    "iran islamic": "IR", "iran": "IR", "islamic iran": "IR",
    "syria": "SY", "syrian arab": "SY",
    "palestinian": "PS", "palestinian territory occupied": "PS", "palestine": "PS",
    "united arab emirates": "AE", "uae": "AE",
    # 아시아
    "burma": "MM", "myanmar": "MM",
    "vietnam": "VN", "viet nam": "VN",
    "laos": "LA", "lao peoples democratic": "LA",
    "china": "CN", "peoples china": "CN", "hong kong": "HK", "macau": "MO", "macao": "MO",
    "taiwan": "TW", "taiwan province china": "TW",
    "brunei": "BN", "brunei darussalam": "BN",
    # 아프리카
    "congo democratic": "CD", "democratic congo": "CD", "congo kinshasa": "CD", "zaire": "CD",
    "congo peoples": "CG", "congo": "CG", "congo brazzaville": "CG",
    "cote d ivoire": "CI", "cote divoire": "CI", "ivory coast": "CI",
    "tanzania": "TZ", "united tanzania": "TZ",
    "cape verde": "CV", "cabo verde": "CV",
    "swaziland": "SZ", "eswatini": "SZ",
    "libya": "LY", "libyan arab jamahiriya": "LY",
    "gambia": "GM", "guinea bissau": "GW",
    "central african": "CF",
    # 유럽
    "bosnia and herzegowina": "BA", "bosnia and herzegovina": "BA",
    "macedonia": "MK", "north macedonia": "MK", "former yugoslav macedonia": "MK",
    "czech": "CZ", "czechia": "CZ", "slovakia": "SK", "slovak": "SK",
    "kosovo": "XK", "serbia": "RS", "serbia and montenegro": "RS",
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "holy see": "VA", "vatican": "VA",
    # 아메리카
    "united": "US", "united america": "US", "usa": "US", "u s a": "US",
    "venezuela": "VE", "venezuela bolivarian": "VE",
    "bolivia": "BO", "bolivia plurinational": "BO",
    "virgin british": "VG", "virgin u s": "VI",
    # 기타
    "turkiye": "TR", "turkey": "TR",
    "burma myanmar": "MM",
    # 한글 통용 표기 (정본 kor_name과 다르게 적히는 경우)
    "조선민주주의인민공화국": "KP", "북조선": "KP",
    "대한민국": "KR", "한국": "KR", "남한": "KR",
    "미국": "US", "중국": "CN", "일본": "JP",
    "러시아연방": "RU", "시리아": "SY", "미얀마": "MM", "버마": "MM",
    "아랍에미리트연합": "AE", "튀르키예": "TR",
    "micronesia": "FM", "micronesia federated": "FM",
    "netherlands antilles": "AN",
    "unknown": "", "stateless": "", "none": "",
}


def resolve(raw: str) -> Tuple[Optional[str], str]:
    """국적 원문 → (alpha2, 표시명).

    매칭 실패 시 (None, 원문)을 돌려줍니다. 임의로 버리지 않습니다 —
    모르는 표기는 원문 그대로 남기고 리포트로 드러내는 편이 안전합니다.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, ""

    k = _key(raw)
    if not k:
        return None, raw

    a2 = _EXACT.get(k) or ALIASES.get(k)

    # "MOLDOVA, REPUBLIC OF" 처럼 뒤집힌 표기 → 앞부분만으로 재시도
    if not a2 and "," in raw:
        head = _key(raw.split(",")[0])
        a2 = _EXACT.get(head) or ALIASES.get(head)

    if not a2:
        return None, raw
    if a2 == "":            # 무국적·미상: 국적 없음으로 처리
        return None, ""

    c = BY_A2.get(a2)
    return (a2, c["kor"]) if c else (None, raw)
