"""
Discord #flyer-edit チャンネルのメッセージを Gemini で解析し、
config.json を更新する。安全のため、許可されたフィールドのみ変更を受け付ける。
"""

import os
import sys
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_FILE = ROOT / ".state" / "last_edit_msg_id.txt"

DISCORD_API = "https://discord.com/api/v10"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

ALLOWED_KEYS = {"title", "footer", "emojis", "overlay_opacity",
                "stroke_width", "font_sizes", "extra_info"}
ALLOWED_EMOJI_KEYS = {"open", "drink", "ticket"}
ALLOWED_FONT_KEYS = {"title", "venue", "event", "info", "footer"}
ALLOWED_EXTRA_KEYS = {"venue_line", "event_name", "open", "drink", "ticket"}


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def validate_config(cfg):
    """許可されたフィールドのみ残し、不正な変更は除去"""
    out = {}
    for k, v in cfg.items():
        if k not in ALLOWED_KEYS:
            continue
        if k == "emojis" and isinstance(v, dict):
            out[k] = {ek: ev for ek, ev in v.items() if ek in ALLOWED_EMOJI_KEYS}
        elif k == "font_sizes" and isinstance(v, dict):
            out[k] = {fk: int(fv) for fk, fv in v.items()
                      if fk in ALLOWED_FONT_KEYS and isinstance(fv, (int, float)) and 20 <= fv <= 200}
        elif k == "extra_info" and isinstance(v, dict):
            extra = {}
            for date, info in v.items():
                if isinstance(info, dict):
                    extra[date] = {ik: str(iv) for ik, iv in info.items()
                                   if ik in ALLOWED_EXTRA_KEYS and iv}
            out[k] = extra
        elif k == "overlay_opacity" and isinstance(v, (int, float)):
            out[k] = max(0, min(255, int(v)))
        elif k == "stroke_width" and isinstance(v, (int, float)):
            out[k] = max(0, min(10, int(v)))
        elif k in ("title", "footer") and isinstance(v, str):
            out[k] = v[:200]
    return out


def fetch_edit_messages():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        return []
    res = requests.get(
        f"{DISCORD_API}/channels/{channel_id}/messages?limit=50",
        headers={"Authorization": f"Bot {token}"}, timeout=30)
    res.raise_for_status()
    return res.json()


def post_to_edit_channel(text):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        return
    try:
        res = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json"},
            json={"content": text}, timeout=30)
        if res.status_code >= 400:
            print(f"⚠️ Discord post失敗 ({res.status_code}): {res.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Discord post例外: {e}", file=sys.stderr)


def call_gemini(current_config, user_request):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 未設定")

    system_prompt = f"""あなたはバンドの月次ライブフライヤー画像のJSON設定を編集するアシスタントです。

現在の設定:
{json.dumps(current_config, ensure_ascii=False, indent=2)}

ユーザーの依頼:
{user_request}

依頼に応じて設定を更新し、**更新後の完全なJSON設定のみ**を返してください。

ルール:
- 依頼で言及されたフィールド以外は現在の値を維持してください
- 編集可能なフィールド: title (string, {{month}} プレースホルダ可), footer (string), emojis (open/drink/ticket), overlay_opacity (0-255), stroke_width (0-10), font_sizes (title/venue/event/info/footer), extra_info (日付ごとの追加情報 "M/D"形式キー、値はvenue_line/event_name/open/drink/ticket)
- これら以外のフィールドの追加・変更は無視する
- 不明確な依頼や危険な依頼の場合は、 {{"error": "理由"}} を返す
- マークダウンや説明文は不要。**JSONのみ**を返す。"""

    res = requests.post(
        f"{GEMINI_API}?key={api_key}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": system_prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
            },
        }, timeout=60)
    res.raise_for_status()
    data = res.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def get_last_processed_id():
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip()
    return ""


def save_last_processed_id(msg_id):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(msg_id)


def main():
    messages = fetch_edit_messages()
    if not messages:
        print("flyer-editチャンネルにメッセージなし")
        return
    last_id = get_last_processed_id()
    # 古い順に並び替え、未処理メッセージのみ抽出 (Botの自分自身のメッセージは除外)
    new_messages = []
    for msg in reversed(messages):
        if int(msg["id"]) <= int(last_id) if last_id else False:
            continue
        if msg.get("author", {}).get("bot"):
            continue
        new_messages.append(msg)

    if not new_messages:
        print("新規メッセージなし")
        return

    print(f"処理対象: {len(new_messages)}件")
    config = load_config()
    config_changed = False

    for msg in new_messages:
        user_text = msg.get("content", "").strip()
        has_attachment = bool(msg.get("attachments"))
        if not user_text:
            # 画像のみのメッセージは画像生成側で処理されるので、ここでは何もしない
            save_last_processed_id(msg["id"])
            continue
        # 短すぎる雑談 (5文字未満) はスキップ
        if len(user_text) < 5 and not has_attachment:
            save_last_processed_id(msg["id"])
            continue
        print(f"→ 処理中: {user_text[:60]}")
        try:
            result = call_gemini(config, user_text)
            if "error" in result:
                # エラー時は静かにスキップ (雑談を誤検知してた可能性が高いので)
                print(f"  スキップ: {result['error']}")
            else:
                new_config = validate_config(result)
                # 変更があったかチェック (空のレスポンスでconfigが消えないように)
                if new_config != {k: config.get(k) for k in new_config}:
                    for k, v in new_config.items():
                        if isinstance(v, dict) and isinstance(config.get(k), dict):
                            config[k] = {**config[k], **v}
                        else:
                            config[k] = v
                    save_config(config)
                    config_changed = True
                    post_to_edit_channel(
                        f"✅ <@{msg['author']['id']}> の依頼を反映：「{user_text[:80]}」\n"
                        f"5分以内に新しいフライヤーが届きます。")
        except Exception as e:
            print(f"  エラー: {e}", file=sys.stderr)
        finally:
            save_last_processed_id(msg["id"])
            time.sleep(1)  # Gemini APIのレート制限を避ける

    if config_changed:
        print("config.json 更新あり")
    else:
        print("config.json 変更なし")


if __name__ == "__main__":
    main()
