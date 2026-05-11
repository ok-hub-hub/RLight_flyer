"""
アルライト ライブスケジュール フライヤー生成スクリプト

1. https://rlightband.wixsite.com/site からライブ情報を取得
2. backgrounds/ から今月の背景画像を選択
3. Pillow で情報を合成して画像生成
4. Discord Webhook で送信
"""

import os
import re
import sys
import json
import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pilmoji import Pilmoji

SITE_URL = "https://rlightband.wixsite.com/site"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROOT = Path(__file__).parent
BG_DIR = ROOT / "backgrounds"
OUTPUT_PATH = ROOT / "output.jpg"

CANVAS_W, CANVAS_H = 1080, 1920

STOP_KEYWORDS = {"NEXT LIVE", "PAST LIVE", "LIVE INFO", "SCHEDULE", "LIVE"}


def pick_background(month: int) -> Path:
    candidates = [
        BG_DIR / f"{month:02d}.jpg",
        BG_DIR / f"{month:02d}.png",
    ]
    for p in candidates:
        if p.exists():
            return p
    for p in sorted(BG_DIR.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return p
    raise FileNotFoundError("背景画像が backgrounds/ に見つかりません")


def fetch_schedule_html() -> str:
    res = requests.get(SITE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    res.raise_for_status()
    return res.text


def clean_line(s: str) -> str:
    return s.replace("​", "").strip()


def parse_lives(html: str, target_month: int):
    soup = BeautifulSoup(html, "html.parser")

    all_lines = []
    for el in soup.find_all(["p", "div", "span", "h1", "h2", "h3", "li"]):
        t = el.get_text(separator="\n", strip=True)
        if not t:
            continue
        for line in t.split("\n"):
            line = clean_line(line)
            if line and (not all_lines or all_lines[-1] != line):
                all_lines.append(line)

    date_re = re.compile(r"(?:(\d{4})[/年\-\.])?(\d{1,2})[/月\-\.](\d{1,2})日?")

    by_date = {}
    current_key = None
    for line in all_lines:
        if line.upper() in STOP_KEYWORDS:
            current_key = None
            continue
        m = date_re.search(line)
        if m:
            month = int(m.group(2))
            day = int(m.group(3))
            if month == target_month:
                current_key = (month, day)
                by_date.setdefault(current_key, [])
                by_date[current_key].append(line)
                continue
            else:
                current_key = None
                continue
        if current_key is not None:
            by_date[current_key].append(line)

    lives = []
    for key in sorted(by_date.keys()):
        seen = set()
        unique = []
        for line in by_date[key]:
            if line in seen:
                continue
            seen.add(line)
            unique.append(line)
        lives.append({
            "date": f"{key[0]}/{key[1]}",
            "lines": unique,
        })
    return lives


def extract_fields(lines):
    """各ライブのlinesから venue/event_name/open/drink/ticket を抽出"""
    fields = {"venue_line": "", "event_name": "", "open": "", "drink": "", "ticket": ""}
    leftover = []

    for line in lines:
        if not fields["venue_line"] and re.search(r"\d{4}\.\d{1,2}\.\d{1,2}|\d{1,2}/\d{1,2}", line):
            fields["venue_line"] = line
            continue
        if re.search(r"OPEN|START|開場|開演", line, re.IGNORECASE):
            fields["open"] = line
            continue
        if re.search(r"DRINK|ドリンク", line, re.IGNORECASE):
            fields["drink"] = line
            continue
        if re.search(r"ADV|DOOR|TICKET|チケット|前売|当日|U-?\d+|[¥￥]\d", line, re.IGNORECASE):
            fields["ticket"] = line
            continue
        leftover.append(line)

    # event_name候補: leftoverから "w/" やメンバー名っぽい行を除く
    for line in leftover:
        if line.startswith("w/") or line.lower() == "w":
            continue
        if not fields["event_name"]:
            fields["event_name"] = line
            break

    return fields


def detect_issues(lives):
    """重複や異常を検知して警告リストを返す"""
    warnings = []
    # 同じ日付が複数ある？
    dates = [l["date"] for l in lives]
    seen_dates = set()
    for d in dates:
        if d in seen_dates:
            warnings.append(f"⚠️ 同じ日付({d})が複数取得されています")
        seen_dates.add(d)
    # 各ライブ内に同じ行が複数？(本来dedup済みだが念のため)
    for live in lives:
        line_counts = {}
        for line in live["lines"]:
            line_counts[line] = line_counts.get(line, 0) + 1
        for line, cnt in line_counts.items():
            if cnt > 1:
                warnings.append(f"⚠️ {live['date']} 内で重複行: '{line[:30]}...' x{cnt}")
    # 情報不足の警告
    for live in lives:
        f = extract_fields(live["lines"])
        missing = []
        if not f["venue_line"]: missing.append("会場")
        if not f["open"]: missing.append("OPEN時刻")
        if not f["ticket"]: missing.append("チケット代")
        if missing:
            warnings.append(f"ℹ️ {live['date']} 情報不足: {', '.join(missing)} (HPに記載なし)")
    return warnings


def _draw_centered(pilmoji, text, font, y, fill="white", stroke=3):
    w, _ = pilmoji.getsize(text, font=font)
    x = (CANVAS_W - w) // 2
    pilmoji.text((x, y), text, fill, font=font,
                 stroke_width=stroke, stroke_fill="black")


def _event_height(f):
    h = 0
    if f["venue_line"]: h += 80
    if f["event_name"]: h += 90
    if f["open"]: h += 70
    if f["drink"]: h += 70
    if f["ticket"]: h += 70
    return h


def render_flyer(bg_path, lives, target_month):
    # 背景: アスペクト比保ったままクロップして埋める (引き伸ばしなし)
    bg_raw = Image.open(bg_path).convert("RGB")
    bg = ImageOps.fit(bg_raw, (CANVAS_W, CANVAS_H), method=Image.LANCZOS, centering=(0.5, 0.5))

    # 半透明オーバーレイで暗くする (読みやすさ重視で濃いめ)
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 175))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    font_path = find_japanese_font()
    title_font = ImageFont.truetype(font_path, 84)
    venue_font = ImageFont.truetype(font_path, 50)
    event_font = ImageFont.truetype(font_path, 46)
    info_font = ImageFont.truetype(font_path, 42)
    footer_font = ImageFont.truetype(font_path, 44)

    # レイアウトゾーン
    TITLE_Y = 120
    EVENTS_TOP = 280
    EVENTS_BOTTOM = CANVAS_H - 200
    FOOTER_Y = CANVAS_H - 110

    with Pilmoji(canvas) as pilmoji:
        # タイトル
        title = f"🎸 {target_month}月 Live Schedule 🎸"
        _draw_centered(pilmoji, title, title_font, TITLE_Y)

        # 各イベントを均等スロットに配置
        n = max(1, len(lives))
        slot_h = (EVENTS_BOTTOM - EVENTS_TOP) / n

        for i, live in enumerate(lives):
            f = extract_fields(live["lines"])
            slot_top = EVENTS_TOP + i * slot_h
            slot_center = slot_top + slot_h / 2

            # 区切り線(2件目以降)
            if i > 0:
                draw.line((140, int(slot_top), CANVAS_W - 140, int(slot_top)),
                          fill="white", width=2)

            # スロット中央に内容を配置
            content_h = _event_height(f)
            y = int(slot_center - content_h / 2)

            if f["venue_line"]:
                _draw_centered(pilmoji, f["venue_line"], venue_font, y)
                y += 80
            if f["event_name"]:
                _draw_centered(pilmoji, f"「{f['event_name']}」", event_font, y)
                y += 90
            if f["open"]:
                _draw_centered(pilmoji, f"⏳ {f['open']}", info_font, y)
                y += 70
            if f["drink"]:
                _draw_centered(pilmoji, f"🍺 {f['drink']}", info_font, y)
                y += 70
            if f["ticket"]:
                _draw_centered(pilmoji, f"🎫 {f['ticket']}", info_font, y)
                y += 70

        # フッター
        _draw_centered(pilmoji, "ご予約はDMまたはHPまで 🔥", footer_font, FOOTER_Y)

    canvas.save(OUTPUT_PATH, "JPEG", quality=92)
    return OUTPUT_PATH


def find_japanese_font():
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("日本語フォントが見つかりません")


def post_to_discord(image_path, lives, target_month, warnings):
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print("DISCORD_WEBHOOK が未設定です。送信をスキップします。", file=sys.stderr)
        return
    summary = [f"**{target_month}月のライブスケジュール**"]
    for live in lives:
        summary.append(f"・{live['date']}")
    if warnings:
        summary.append("")
        summary.append("**チェック結果**")
        summary.extend(warnings)
    payload = {"content": "\n".join(summary)}
    with open(image_path, "rb") as f:
        res = requests.post(
            webhook,
            data={"payload_json": json.dumps(payload)},
            files={"file": (image_path.name, f, "image/jpeg")},
            timeout=30,
        )
    res.raise_for_status()
    print("Discordに送信しました。")


def main():
    target_month = int(os.environ.get("TARGET_MONTH") or datetime.date.today().month)
    print(f"対象月: {target_month}月")
    html = fetch_schedule_html()
    lives = parse_lives(html, target_month)
    print(f"取得したライブ件数: {len(lives)}")
    warnings = detect_issues(lives)
    for w in warnings:
        print(w)
    bg = pick_background(target_month)
    print(f"背景画像: {bg.name}")
    out = render_flyer(bg, lives, target_month)
    print(f"生成完了: {out}")
    post_to_discord(out, lives, target_month, warnings)


if __name__ == "__main__":
    main()
