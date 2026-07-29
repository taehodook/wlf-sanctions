"""
make_og_image.py
================
링크 공유용 OG 이미지(1200x630, og-image.png)를 생성합니다.
사이트 다크테마(#0B1526)에 맞춘 카드로, data/meta.json의 실제 수치를 반영합니다.

수동 실행:  python src/make_og_image.py
"""
import json
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (11, 21, 38)          # #0B1526
PANEL = (20, 32, 54)       # 카드 배경
PANEL_BORDER = (40, 56, 84)
ACCENT = (56, 189, 248)    # #38BDF8 시안
WHITE = (241, 245, 249)
MUTED = (148, 163, 184)

FONT_DIR = r"C:\Windows\Fonts"
BOLD = os.path.join(FONT_DIR, "malgunbd.ttf")
REG = os.path.join(FONT_DIR, "malgun.ttf")


def f(path, size):
    return ImageFont.truetype(path, size)


def load_meta():
    path = os.path.join("data", "meta.json")
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {"total": 0, "by_source": {}}


def draw_text_center(d, cx, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text((cx - w / 2, y), text, font=font, fill=fill)
    return w


def rounded_panel(d, box, radius, fill, outline=None, width=1):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def main():
    meta = load_meta()
    total = meta.get("total", 0)
    by = meta.get("by_source", {})
    # 화면 표기용 (코드, 라벨, 건수)
    sources = [
        ("OFAC", "미국 재무부", by.get("OFAC", 0)),
        ("EU", "EU 통합", by.get("EU", 0)),
        ("UNSC", "UN 안보리", by.get("UNSC", 0)),
        ("KoFIU", "금융위", by.get("KoFIU", 0)),
    ]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 상단 액센트 바
    d.rectangle([0, 0, W, 8], fill=ACCENT)

    pad = 72

    # 눈금 라벨
    d.text((pad, 70), "SANCTIONS  WATCHLIST", font=f(BOLD, 24), fill=ACCENT)

    # 타이틀
    d.text((pad, 116), "WLF 제재명단 통합 조회", font=f(BOLD, 76), fill=WHITE)

    # 서브타이틀
    d.text(
        (pad, 214),
        "OFAC · EU · UN 안보리 · KoFIU  —  매일 자동 수집 · 통계 · 변경이력",
        font=f(REG, 30),
        fill=MUTED,
    )

    # 큰 총계 숫자
    d.text((pad, 292), f"{total:,}", font=f(BOLD, 108), fill=WHITE)
    num_w = d.textlength(f"{total:,}", font=f(BOLD, 108))
    d.text((pad + num_w + 20, 372), "건 등록", font=f(BOLD, 40), fill=ACCENT)

    # 출처 카드 4개 (하단)
    card_y = 452
    card_h = 106
    gap = 20
    card_w = (W - pad * 2 - gap * 3) / 4
    for i, (code, label, cnt) in enumerate(sources):
        x = pad + i * (card_w + gap)
        rounded_panel(
            d,
            [x, card_y, x + card_w, card_y + card_h],
            radius=16,
            fill=PANEL,
            outline=PANEL_BORDER,
            width=1,
        )
        cx = x + card_w / 2
        draw_text_center(d, cx, card_y + 18, code, f(BOLD, 30), WHITE)
        draw_text_center(d, cx, card_y + 58, f"{cnt:,}", f(REG, 26), ACCENT)

    # 푸터
    d.text(
        (pad, H - 44),
        "wlf-sanctions.netlify.app  ·  매일 09:00 KST 자동 갱신  ·  내부 참고용",
        font=f(REG, 22),
        fill=MUTED,
    )

    out = "og-image.png"
    img.save(out, "PNG")
    print(f"생성 완료: {out}  ({W}x{H}, 총 {total:,}건 반영)")


if __name__ == "__main__":
    main()
