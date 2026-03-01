---
name: short
description: Hyperliquid でショートポジションを開く。チャート分析でショートシグナルが出た場合に使用する。
allowed-tools: Bash
---

Hyperliquid で SHORT ポジションを開く。

Bash ツールを使って以下のコマンドを実行すること:
```bash
cd /app && python script/short.py
```

実行後、標準出力の内容（entry_price, trade_id など）を確認して報告すること。
エラーが発生した場合はエラー内容を報告すること。
