# CC Visual Trade

Hyperliquid の15分足チャートを **Claude Code CLI** に画像分析させ、`LONG / SHORT / HOLD` を自動判断して執行する自動売買 bot。

## アーキテクチャ

```
毎15分 (APScheduler)
  ↓
1. Hyperliquid から15分足 100本取得 → mplfinance で PNG生成
   (ローソク足 + SMA20/50 + RSI + 出来高)
  ↓
2. claude -p "チャートを分析して..." --allowedTools Read,Bash
   ├─ Claude が Read ツールで PNG を分析
   ├─ LONG  → Bash: python script/long.py  (指値→タイムアウト→成り行き)
   ├─ SHORT → Bash: python script/short.py (指値→タイムアウト→成り行き)
   └─ HOLD  → 何もしない
  ↓
3. 判断・実行結果を SQLite に記録

毎30秒
  → オープンポジションが1時間経過 → 指値決済 → タイムアウト → 成り行き決済
```

## 機能

- **チャート生成**: SMA20/50・RSI・出来高付きのダークテーマ PNG
- **AI判断**: Claude Code CLI がチャート画像を見て LONG/SHORT/HOLD を決定
- **注文執行**: 指値優先 (手数料メリット) → 30秒未約定で成り行きフォールバック
- **強制決済**: 1時間後に必ず決済 (指値60秒 → 成り行き)
- **ダッシュボード**: FastAPI + Jinja2 (ポート 8080)
  - 現在ポジション・含み損益・決済カウントダウン
  - 最新チャート画像
  - Claude の判断とその理由
  - 取引履歴・勝率・累計P&L

## 技術スタック

| 役割 | ライブラリ |
|------|-----------|
| AI判断 | Claude Code CLI (`claude -p`) |
| チャート生成 | mplfinance + matplotlib |
| 取引 | hyperliquid-python-sdk |
| スケジューラ | APScheduler |
| Web | FastAPI + Jinja2 |
| DB | SQLite + SQLAlchemy |
| コンテナ | Docker Compose |

## セットアップ

### 1. 環境変数を設定

```bash
cp .env.example .env
```

`.env` を編集:

```env
HYPERLIQUID_PRIVATE_KEY=0x your_private_key
HYPERLIQUID_ACCOUNT_ADDRESS=0x your_wallet_address
TRADING_COIN=BTC
POSITION_SIZE_USD=100
LEVERAGE=3
DRY_RUN=true   # 最初は true で動作確認
```

### 2. Claude Code CLI の認証

```bash
claude auth login   # または gh auth login 後に claude setup
```

認証情報は `~/.claude/` に保存され、Docker コンテナにマウントされます。

### 3. 起動

```bash
docker compose up --build
```

ダッシュボード: http://localhost:8080

### 4. 本番稼働

動作確認後、`.env` の `DRY_RUN=false` に変更して再起動。

```bash
docker compose restart
```

## ディレクトリ構成

```
CC_Visual_Trade/
├── main.py                  # エントリポイント (FastAPI + APScheduler)
├── .claude/commands/
│   ├── long.md              # /long スキル
│   └── short.md             # /short スキル
├── script/
│   ├── long.py              # Long 注文ロジック
│   └── short.py             # Short 注文ロジック
├── src/
│   ├── chart.py             # チャート生成
│   ├── orchestrator.py      # Claude CLI 呼び出し
│   ├── trader.py            # 1時間決済ロジック
│   ├── database.py          # SQLite モデル
│   ├── dashboard.py         # FastAPI ルーター
│   └── config.py            # 設定
├── templates/               # Jinja2 テンプレート
├── static/                  # CSS
├── charts/                  # 生成チャート PNG (gitignore)
└── data/                    # SQLite DB (gitignore)
```

## 注意事項

- 本ソフトウェアは教育・研究目的です
- 実際の資金を使う場合は自己責任で
- `DRY_RUN=true` で十分テストしてから本番稼働させてください
