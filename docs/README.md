# docs/ — Backend の設計

| ファイル | 中身 |
|---|---|
| [01_architecture.md](01_architecture.md) | 構成・事実の確定・音声の経路・悪用対策・Agent の呼び出し |
| [02_async_ask.md](02_async_ask.md) | 回答を非同期で返す仕組みと二重処理の防止 |
| [03_heading.md](03_heading.md) | 進行方位の算出とプロンプトへの渡し方 |
| [04_dynamodb_table.md](04_dynamodb_table.md) | DynamoDB のテーブル定義 |

プロジェクト全体の要件・方針・契約は [docs-parent/](../docs-parent/README.md)（親リポジトリの写し。編集しない）。
Backend に固有の ADR は無い。プロジェクト全体の決定は親リポジトリの [adr/](https://github.com/h-akira/TouringProject/blob/main/adr/README.md)。
