# AGENTS.md — トレードボット学習ログ

ルールは `/app/prompt/rule.html` に記載されています。
振り返り全文は `/app/data/reflections/trade_{id}.md` に保存されています。

## トレード評価時に読むファイル（これ以外は読まない）

- `/app/prompt/rule.html` — トレードルール（基本ルール・学習済みルール・エントリー推奨条件）
- `/app/prompt/context.md` — ポジションサイズ・フィー・判断フォーマット等の設定
- `/app/charts/` — 今サイクルのチャート画像（プロンプトで明示されたパスのみ）
- `/app/data/reflection_digest.md` — 振り返りダイジェスト（存在する場合）
- `/app/data/reflections/trade_{id}.md` — 振り返り個別ファイル（参照が必要な場合のみ）

**読まないもの**: `/app/src/`, `/app/script/`, `/app/tests/`, `/app/templates/` などのソースコード一切。
