---
name: test
description: Docker内でテストネットのテストスイートを実行する
allowed-tools: Bash
---

1. コード変更があれば先にリビルド: `docker compose up --build -d`
2. テスト実行: `docker compose exec -e TESTNET=true app python -m pytest tests/ -v -s`
3. 失敗があれば原因を診断し修正を提案する
