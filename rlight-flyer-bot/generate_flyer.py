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
from PIL import Image, ImageDraw, ImageFont

SITE_URL = "https://rlightband.wixsite.com/site"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROOT = Path(__file__).parent
BG_DIR = ROOT / "backgrounds"
OUTPUT_PATH = ROOT / "output.jpg"

CANVAS_W, CANVAS_H = 1080, 1920

# 月ごとの背景画像ファイル名 (backgrounds/01.jpg, 02.jpg, ... 12.jpg)
def pick_background(month: int) -> Path:
    candidates = [
        BG_DIR / f"{month:02d}.jpg",
        BG_DIR / f"{month:02d}.png",
        BG_DIR / f"{month}.jpg",
        BG_DIR / f"{month}.png",
    ]
    for p in candidates:
        if p.exists():
            return p
    # フォールバック: 何でもいいから最初に見つかった画像
    for p in sorted(BG_DIR.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return p
    raise FileNotFoundError(
        f"背景画像が backgrounds/ に見つかりません。{month:02d}.jpg を置いてください。"
    )


def fetch_schedule_html() -> str:
    res = requests.get(SITE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    res.raise_for_status()
    return res.text


def parse_lives(html: str, target_month: int) -> list[dict]:
    """
    Wixサイトから今月のライブを抽出する。
    サイトのHTML構造に依存するため、構造が変わったらここを直す。
    現状は「日付っぽい文字列を含むテキストブロック」を全部拾い、
    target_month に該当するものを返すゆるい実装。
    """
    soup = BeautifulSoup(html, "html.parser")
    text_blocks = []
    for el in soup.find_all(["p", "div", "span", "h1", "h2", "h3", "li"]):
        t = el.get_text(separator="\n", strip=True)
        if not t:
            continue
        text_blocks.append(t)

    # 日付パターン: 2026/5/24 or 5/24 or 5月24日 など
    date_re = re.compile(
        r"(?:(\d{4})[/年\-\.])?(\d{1,2})[/月\-\.](\d{1,2})日?"
    )

    seen = set()
    lives = []
    for block in text_blocks:
        for line_idx, line in enumerate(block.split("\n")):
            m = date_re.search(line)
            if not m:
                continue
            month = int(m.group(2))
            day = int(m.group(3))
            if month != target_month:
                continue
            # 直近数行をライブ情報の本文とみなす
            context_lines = block.split("\n")[line_idx:line_idx + 6]
            key = "\n".join(context_lines)
            if key in seen:
                continue
            seen.add(key)
            lives.append({
                "date": f"{month}/{day}",
                "lines": [l for l in context_lines if l.strip()],
            })
    return lives


def render_flyer(bg_path: Path, lives: list[dict], target_month: int) -> Path:
    bg = Image.open(bg_path).convert("RGB").resize((CANVAS_W, CANVAS_H))
    # 半透明の暗いオーバーレイ
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 130))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(canvas)

    font_path = find_japanese_font()
    title_font = ImageFont.truetype(font_path, 90)
    month_font = ImageFont.truetype(font_path, 140)
    date_font = ImageFont.truetype(font_path, 70)
    body_font = ImageFont.truetype(font_path, 42)

    # タイトル
    draw.text((CANVAS_W // 2, 120), "アルライト LIVE", fill="white",
              font=title_font, anchor="mm")
    draw.text((CANVAS_W // 2, 260), f"{target_month}月", fill="#ffcc33",
              font=month_font, anchor="mm")

    # ライブ情報
    y = 420
    if not lives:
        draw.text((CANVAS_W // 2, CANVAS_H // 2),
                  "今月のライブ情報はありません",
                  fill="white", font=date_font, anchor="mm")
    else:
        for live in lives:
            draw.text((80, y), live["date"], fill="#ffcc33",
                      font=date_font, anchor="lm")
            y += 90
            for line in live["lines"]:
                if line == live["date"] or re.search(r"\d{1,2}[/月]\d{1,2}", line):
                    continue
                draw.text((100, y), line[:40], fill="white",
                          font=body_font, anchor="lm")
                y += 60
            y += 40
            if y > CANVAS_H - 120:
                break

    canvas.convert("RGB").save(OUTPUT_PATH, "JPEG", quality=92)
    return OUTPUT_PATH


def find_japanese_font() -> str:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("日本語フォントが見つかりません")


def post_to_discord(image_path: Path, lives: list[dict], target_month: int):
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print("DISCORD_WEBHOOK が未設定です。送信をスキップします。", file=sys.stderr)
        return
    summary_lines = [f"**{target_month}月のライブスケジュール**"]
    if lives:
        for live in lives:
            summary_lines.append(f"・{live['date']}")
    else:
        summary_lines.append("（今月の予定はサイト上で確認できませんでした）")
    payload = {"content": "\n".join(summary_lines)}

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

    bg = pick_background(target_month)
    print(f"背景画像: {bg.name}")

    out = render_flyer(bg, lives, target_month)
    print(f"生成完了: {out}")

    post_to_discord(out, lives, target_month)


if __name__ == "__main__":
    main()
