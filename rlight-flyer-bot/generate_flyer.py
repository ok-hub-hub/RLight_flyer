"""
アルライト ライブスケジュール フライヤー生成スクリプト

1. Discord #flyer-bg から最新画像 (背景) と補足テキストを取得
2. https://rlightband.wixsite.com/site からライブ情報をスクレイピング
3. 背景 + HP情報 + Discord補足を合成して画像生成
4. 完成版 + 編集用テンプレート(背景のみ) を Discord Webhook で送信
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
from pilmoji import Pilmoji

SITE_URL = "https://rlightband.wixsite.com/site"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ROOT = Path(__file__).parent
BG_DIR = ROOT / "backgrounds"
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "output.jpg"
TEMPLATE_PATH = ROOT / "output_template.jpg"

CANVAS_W, CANVAS_H = 1080, 1920

STOP_KEYWORDS = {"NEXT LIVE", "PAST LIVE", "LIVE INFO", "SCHEDULE", "LIVE"}

DISCORD_API = "https://discord.com/api/v10"

DEFAULT_CONFIG = {
    "title": "🎸 {month}月 Live Schedule 🎸",
    "footer": "ご予約はDMまたはHPまで 🔥",
    "emojis": {"open": "⏳", "drink": "🍺", "ticket": "🎫"},
    "overlay_opacity": 145,
    "stroke_width": 3,
    "font_sizes": {"title": 84, "venue": 50, "event": 46, "info": 42, "footer": 44},
    "extra_info": {},
    "canva_template_url": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text())
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k] = {**cfg[k], **v}
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"⚠️ config.json読み込みエラー: {e}, デフォルト使用", file=sys.stderr)
    return cfg


CONFIG = load_config()


def clean_line(s: str) -> str:
    return s.replace("​", "").strip()


# ---------- Discord ----------

def fetch_discord_messages(limit: int = 50):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        print("Discord Bot 未設定。Discordからの取得をスキップします。", file=sys.stderr)
        return []
    res = requests.get(
        f"{DISCORD_API}/channels/{channel_id}/messages?limit={limit}",
        headers={"Authorization": f"Bot {token}"},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()


def fetch_discord_background(messages):
    for msg in messages:
        for att in msg.get("attachments", []):
            if att.get("content_type", "").startswith("image/"):
                r = requests.get(att["url"], timeout=30)
                r.raise_for_status()
                path = ROOT / "discord_bg.jpg"
                with open(path, "wb") as f:
                    f.write(r.content)
                return path
    return None


def parse_discord_supplements(messages, target_month):
    """テキストメッセージから日付付きの補足情報を抽出"""
    supplements = {}
    date_re = re.compile(r"(\d{1,2})[/月\-\.](\d{1,2})日?")
    for msg in messages:
        text = msg.get("content", "")
        if not text:
            continue
        lines = [clean_line(l) for l in text.split("\n") if l.strip()]
        current = None
        for line in lines:
            m = date_re.search(line)
            if m:
                month = int(m.group(1))
                day = int(m.group(2))
                if month == target_month:
                    current = (month, day)
                    supplements.setdefault(current, [])
                    if line not in supplements[current]:
                        supplements[current].append(line)
                    continue
                else:
                    current = None
                    continue
            if current is not None and line not in supplements[current]:
                supplements[current].append(line)
    return supplements


def merge_supplements(lives, supplements):
    by_date = {}
    titles = {}
    for live in lives:
        m, d = map(int, live["date"].split("/"))
        by_date[(m, d)] = list(live["lines"])
        titles[(m, d)] = live.get("event_title", "")
    for key, lines in supplements.items():
        existing = by_date.setdefault(key, [])
        for line in lines:
            if line not in existing:
                existing.append(line)
    return [{"date": f"{m}/{d}", "lines": by_date[(m, d)],
             "event_title": titles.get((m, d), "")}
            for (m, d) in sorted(by_date.keys())]


# ---------- HP scraping ----------

def fetch_schedule_html() -> str:
    res = requests.get(SITE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    res.raise_for_status()
    return res.text


def parse_lives(html: str, target_month: int):
    """Wix サイトの内部JSON (RICOS) からイベント情報を抽出。
    各イベントは "sh":"YYYY.MM.DD(...)..." の見出しを持ち、
    その直前のテキスト群がそのイベントの本文。"""
    sh_pattern = re.compile(r'"sh":"((?:\d{4}\.)?\d{1,2}\.\d{1,2}[^"]{0,80})"')
    text_pattern = re.compile(r'"text":"([^"\\]*(?:\\.[^"\\]*)*)"')
    date_in_sh = re.compile(r'(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})')

    publish_re = re.compile(r'_publishDate')
    title_re = re.compile(r'"title":"([^"]*)"')
    by_date = {}
    prev_end = 0
    for m in sh_pattern.finditer(html):
        date_str = m.group(1)
        dm = date_in_sh.search(date_str)
        if not dm:
            prev_end = m.end()
            continue
        month = int(dm.group(2))
        day = int(dm.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            prev_end = m.end()
            continue
        # 該当イベントの JSON ブロック開始位置: 直前の _publishDate
        pub_matches = list(publish_re.finditer(html[prev_end:m.start()]))
        if pub_matches:
            block_start = prev_end + pub_matches[-1].start()
        else:
            block_start = prev_end
        block = html[block_start:m.start()]
        # タイトル(イベント名)を抽出
        title_m = title_re.search(block)
        event_title = ""
        if title_m and title_m.group(1):
            title_text = title_m.group(1).replace('\\/', '/').replace('\\n', '\n').replace('\\"', '"')
            # 改行があれば一番目立つ部分(通常は最後の「...」行)を選ぶ
            for ln in title_text.split('\n'):
                ln = clean_line(ln)
                if ln:
                    if '「' in ln and '」' in ln:
                        event_title = ln
                        break
                    if not event_title:
                        event_title = ln
        lines = [date_str]
        # ブロック内のテキストを抽出
        for tm in text_pattern.finditer(block):
            txt = tm.group(1).replace('\\/', '/').replace('\\n', '\n').replace('\\"', '"')
            txt = clean_line(txt)
            if txt:
                lines.append(txt)
        prev_end = m.end()
        if month != target_month:
            continue
        key = (month, day)
        seen = set()
        unique = []
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            unique.append(line)
        by_date[key] = {"lines": unique, "event_title": event_title}

    if not by_date:
        # フォールバック: 見出し JSON が見つからない場合は古いHTMLパースを試す
        return _parse_lives_html_fallback(html, target_month)

    lives = []
    for key in sorted(by_date.keys()):
        v = by_date[key]
        lives.append({
            "date": f"{key[0]}/{key[1]}",
            "lines": v["lines"],
            "event_title": v.get("event_title", ""),
        })
    return lives


def _parse_lives_html_fallback(html, target_month):
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
        is_valid = m and (1 <= int(m.group(2)) <= 12) and (1 <= int(m.group(3)) <= 31)
        if is_valid:
            month = int(m.group(2))
            day = int(m.group(3))
            if month == target_month:
                current_key = (month, day)
                by_date.setdefault(current_key, []).append(line)
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
        lives.append({"date": f"{key[0]}/{key[1]}", "lines": unique, "event_title": ""})
    return lives


# ---------- Field extraction ----------

def extract_fields(lines):
    fields = {"venue_line": "", "event_name": "", "open": "", "drink": "", "ticket": ""}
    leftover = []
    ticket_lines = []
    for line in lines:
        if not fields["venue_line"] and re.search(r"\d{4}\.\d{1,2}\.\d{1,2}|\d{1,2}/\d{1,2}", line):
            fields["venue_line"] = line
            continue
        # 配信関連は除外 (フライヤーには会場分のみ載せる)
        if re.search(r"配信|ツイキャス|twitcasting|アーカイブ|premier\.", line, re.IGNORECASE):
            continue
        if re.search(r"OPEN|START|開場|開演", line, re.IGNORECASE):
            fields["open"] = line
            continue
        if re.search(r"DRINK|ドリンク", line, re.IGNORECASE):
            fields["drink"] = line
            continue
        if re.search(r"ADV|DOOR|TICKET|チケット|前売|当日|U-?\d+|[¥￥]\d", line, re.IGNORECASE):
            ticket_lines.append(line)
            continue
        leftover.append(line)
    # チケット情報は最大2行までを「 / 」で連結
    if ticket_lines:
        fields["ticket"] = " / ".join(ticket_lines[:2])
    return fields


def detect_issues(lives):
    warnings = []
    seen_dates = set()
    for live in lives:
        if live["date"] in seen_dates:
            warnings.append(f"⚠️ 同じ日付({live['date']})が複数取得されています")
        seen_dates.add(live["date"])
    for live in lives:
        cnt = {}
        for line in live["lines"]:
            cnt[line] = cnt.get(line, 0) + 1
        for line, c in cnt.items():
            if c > 1:
                warnings.append(f"⚠️ {live['date']} 内で重複行: '{line[:30]}...' x{c}")
    for live in lives:
        f = extract_fields(live["lines"])
        missing = []
        if not f["venue_line"]: missing.append("会場")
        if not f["open"]: missing.append("OPEN時刻")
        if not f["ticket"]: missing.append("チケット代")
        if missing:
            warnings.append(f"ℹ️ {live['date']} 情報不足: {', '.join(missing)} (HP・Discord補足に記載なし)")
    return warnings


# ---------- Rendering ----------

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


def make_bg(bg_path):
    """画像のアスペクト比を保ったまま枠内に収める。余白は黒。"""
    img = Image.open(bg_path).convert("RGB")
    iw, ih = img.size
    scale = min(CANVAS_W / iw, CANVAS_H / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    contained = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))
    x = (CANVAS_W - nw) // 2
    y = (CANVAS_H - nh) // 2
    canvas.paste(contained, (x, y))
    return canvas


def render_flyer(bg_path, lives, target_month):
    bg = make_bg(bg_path)
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H),
                       (0, 0, 0, int(CONFIG["overlay_opacity"])))
    canvas = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    font_path = find_japanese_font()
    fs = CONFIG["font_sizes"]
    title_font = ImageFont.truetype(font_path, fs["title"])
    venue_font = ImageFont.truetype(font_path, fs["venue"])
    event_font = ImageFont.truetype(font_path, fs["event"])
    info_font = ImageFont.truetype(font_path, fs["info"])
    footer_font = ImageFont.truetype(font_path, fs["footer"])

    TITLE_Y = 120
    EVENTS_TOP = 280
    EVENTS_BOTTOM = CANVAS_H - 200
    FOOTER_Y = CANVAS_H - 110

    emojis = CONFIG["emojis"]
    stroke = CONFIG["stroke_width"]

    def draw_(p, text, font, y):
        _draw_centered(p, text, font, y, stroke=stroke)

    with Pilmoji(canvas) as pilmoji:
        title = CONFIG["title"].format(month=target_month)
        draw_(pilmoji, title, title_font, TITLE_Y)
        n = max(1, len(lives))
        slot_h = (EVENTS_BOTTOM - EVENTS_TOP) / n
        for i, live in enumerate(lives):
            f = extract_fields(live["lines"])
            # イベント名は parse_lives で抽出した title を優先
            f["event_name"] = live.get("event_title", "")
            # Merge extra_info if available for this date
            extra = CONFIG.get("extra_info", {}).get(live["date"], {})
            for k in ("open", "drink", "ticket", "event_name", "venue_line"):
                if not f[k] and extra.get(k):
                    f[k] = extra[k]
            slot_top = EVENTS_TOP + i * slot_h
            slot_center = slot_top + slot_h / 2
            if i > 0:
                draw.line((140, int(slot_top), CANVAS_W - 140, int(slot_top)),
                          fill="white", width=2)
            content_h = _event_height(f)
            y = int(slot_center - content_h / 2)
            if f["venue_line"]:
                draw_(pilmoji, f["venue_line"], venue_font, y); y += 80
            if f["event_name"]:
                draw_(pilmoji, f"「{f['event_name']}」", event_font, y); y += 90
            if f["open"]:
                draw_(pilmoji, f"{emojis['open']} {f['open']}", info_font, y); y += 70
            if f["drink"]:
                draw_(pilmoji, f"{emojis['drink']} {f['drink']}", info_font, y); y += 70
            if f["ticket"]:
                draw_(pilmoji, f"{emojis['ticket']} {f['ticket']}", info_font, y); y += 70
        draw_(pilmoji, CONFIG["footer"], footer_font, FOOTER_Y)

    canvas.save(OUTPUT_PATH, "JPEG", quality=92)
    return OUTPUT_PATH


def render_template(bg_path):
    """テキストなしの編集用テンプレート (Canva等で文字を載せ替えやすいよう、
    全体が見えるレイアウト+薄い暗幕付き)"""
    bg = make_bg(bg_path)
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 60))
    out = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    out.save(TEMPLATE_PATH, "JPEG", quality=92)
    return TEMPLATE_PATH


def pick_background(month: int) -> Path:
    for p in [BG_DIR / f"{month:02d}.jpg", BG_DIR / f"{month:02d}.png"]:
        if p.exists():
            return p
    for p in sorted(BG_DIR.iterdir()):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return p
    raise FileNotFoundError("背景画像が backgrounds/ に見つかりません")


# ---------- Posting ----------

def build_canva_copy_text(lives, target_month):
    """Canva にコピペするための整形テキスト"""
    lines = [f"{target_month}月 Live Schedule", ""]
    for live in lives:
        f = extract_fields(live["lines"])
        f["event_name"] = live.get("event_title", "")
        extra = CONFIG.get("extra_info", {}).get(live["date"], {})
        for k in ("open", "drink", "ticket", "event_name", "venue_line"):
            if not f[k] and extra.get(k):
                f[k] = extra[k]
        if f["venue_line"]:
            lines.append(f["venue_line"])
        if f["event_name"]:
            lines.append(f"「{f['event_name']}」")
        if f["open"]:
            lines.append(f["open"])
        if f["drink"]:
            lines.append(f["drink"])
        if f["ticket"]:
            lines.append(f["ticket"])
        lines.append("")
    lines.append(CONFIG.get("footer", ""))
    return "\n".join(lines)


def post_to_discord(output_path, template_path, lives, target_month, warnings):
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

    canva_url = CONFIG.get("canva_template_url", "").strip()
    if canva_url:
        summary.append("")
        summary.append(f"🎨 **Canvaで編集** → {canva_url}")
        summary.append("リンクを開いて「このテンプレートを使う」で自分用に複製してください。")
        summary.append("")
        summary.append("📋 **コピペ用テキスト**")
        summary.append("```")
        summary.append(build_canva_copy_text(lives, target_month))
        summary.append("```")
    else:
        summary.append("")
        summary.append("📎 編集用テンプレート (背景のみ) も添付。Canva等で再配置できます。")

    payload = {"content": "\n".join(summary)}
    with open(output_path, "rb") as f1, open(template_path, "rb") as f2:
        res = requests.post(
            webhook,
            data={"payload_json": json.dumps(payload)},
            files={
                "files[0]": ("flyer.jpg", f1, "image/jpeg"),
                "files[1]": ("template.jpg", f2, "image/jpeg"),
            },
            timeout=30,
        )
    res.raise_for_status()
    print("Discordに送信しました。")


# ---------- Main ----------

STATE_FILE = ROOT / ".state" / "last_image_msg_id.txt"
CONFIG_HASH_FILE = ROOT / ".state" / "last_config_hash.txt"


def find_latest_image_message(messages):
    for msg in messages:
        for att in msg.get("attachments", []):
            if att.get("content_type", "").startswith("image/"):
                return msg
    return None


def config_hash():
    import hashlib
    return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()[:16] if CONFIG_PATH.exists() else ""


def main():
    target_month = int(os.environ.get("TARGET_MONTH") or datetime.date.today().month)
    polling_mode = os.environ.get("POLLING_MODE") == "true"
    print(f"対象月: {target_month}月 (polling_mode: {polling_mode})")

    messages = fetch_discord_messages()
    print(f"Discord メッセージ取得: {len(messages)}件")

    latest_image_msg = find_latest_image_message(messages)
    current_cfg_hash = config_hash()

    # ポーリングモード: 画像 OR config の変更を検知、どちらも無ければスキップ
    if polling_mode:
        last_id = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
        last_cfg = CONFIG_HASH_FILE.read_text().strip() if CONFIG_HASH_FILE.exists() else ""
        image_changed = latest_image_msg and last_id != latest_image_msg["id"]
        config_changed = current_cfg_hash and last_cfg != current_cfg_hash
        if not image_changed and not config_changed:
            if not latest_image_msg:
                print("画像なし、config変更なし。スキップ。")
            else:
                print(f"画像・config共に前回と同じ。スキップ。")
            return
        reason = []
        if image_changed: reason.append("新画像")
        if config_changed: reason.append("config変更")
        print(f"検知: {', '.join(reason)}。生成開始。")

    discord_bg = fetch_discord_background(messages) if messages else None
    if discord_bg:
        print(f"背景画像 (Discord): {discord_bg.name}")
        bg_path = discord_bg
    else:
        bg_path = pick_background(target_month)
        print(f"背景画像 (local): {bg_path.name}")

    html = fetch_schedule_html()
    lives = parse_lives(html, target_month)
    print(f"取得したライブ件数 (HP): {len(lives)}")

    supplements = parse_discord_supplements(messages, target_month) if messages else {}
    if supplements:
        print(f"Discord 補足: {len(supplements)}日分")
        for key, lines in supplements.items():
            print(f"  - {key[0]}/{key[1]}: {len(lines)}行")
        lives = merge_supplements(lives, supplements)

    warnings = detect_issues(lives)
    for w in warnings:
        print(w)

    output_path = render_flyer(bg_path, lives, target_month)
    template_path = render_template(bg_path)
    print(f"生成完了: {output_path}, {template_path}")

    post_to_discord(output_path, template_path, lives, target_month, warnings)

    # 状態保存 (手動実行時もポーリング側で同じものを再処理しないように)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if latest_image_msg:
        STATE_FILE.write_text(latest_image_msg["id"])
        print(f"状態保存 image: {latest_image_msg['id']}")
    if current_cfg_hash:
        CONFIG_HASH_FILE.write_text(current_cfg_hash)
        print(f"状態保存 config: {current_cfg_hash}")


if __name__ == "__main__":
    main()
