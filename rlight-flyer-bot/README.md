# アルライト ライブスケジュール フライヤー生成Bot

アルライト公式サイトから毎月のライブ情報を取得し、フライヤー画像を自動生成してDiscordに通知する仕組みです。

## 仕組み

1. `https://rlightband.wixsite.com/site` をスクレイピング
2. 今月（または指定月）のライブ情報を抽出
3. `backgrounds/` の背景画像にPillowで情報を合成
4. Discord Webhook に画像投稿

## セットアップ手順

### 1. リポジトリ作成
このフォルダをまるごと GitHub の新しいリポジトリにアップロードします。

### 2. Discord Webhook URL を Secrets に登録
- リポジトリ → Settings → Secrets and variables → Actions → New repository secret
- Name: `DISCORD_WEBHOOK`
- Value: Discord で取得した Webhook URL を貼る

### 3. 背景画像を `backgrounds/` に追加
- ファイル名は `01.jpg`〜`12.jpg`（月番号）
- 推奨サイズ: 1080×1920 (縦長)

### 4. 実行
- リポジトリの Actions タブ → "Generate Monthly Flyer" → Run workflow
- 対象月は空欄なら今月、数字を入れればその月で生成

## ローカル実行

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
python generate_flyer.py
```

## トラブルシュート

- **背景画像エラー**: `backgrounds/` に該当月の画像があるか確認
- **ライブ情報が0件**: サイトのHTML構造が変わった可能性。`parse_lives()` の調整が必要
- **Discord に届かない**: Webhook URL が正しいか、Secretsに登録されているか確認
