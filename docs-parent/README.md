# docs/ — プロジェクト全体の設計と契約

> ⚠️ 各ユニット（App・Backend・Agent・CICD）の `docs-parent/` は、親リポジトリの `docs/` の写し。**写しを編集しない。**
> 正本は親リポジトリ `TouringProject` の `docs/` で、更新は親の `docs/sync.sh` で行う。

| ファイル | 中身 |
|---|---|
| [00_user_stories.md](00_user_stories.md) | 要件定義（ユーザーストーリー）。最上位 |
| [01_technical_policies.md](01_technical_policies.md) | 技術方針（レイヤーの方針・技術選定・リージョン） |
| [02_units_definition.md](02_units_definition.md) | ユニットの定義（責務と担当する US） |
| [03_units_contracts.md](03_units_contracts.md) | ユニット間の契約（UC-1〜UC-6） |
| [04_api_openapi.yaml](04_api_openapi.yaml) | App ↔ Backend の API 仕様（OpenAPI） |

ユニットの中の設計は各ユニットの `docs/` にある。

## 変えたとき

1. 親の `docs/` を直す。
2. 親で `docs/sync.sh` を実行し、各ユニットの `docs-parent/` を更新する。
3. 各ユニットで `docs-parent/` をコミットし、親で submodule のポインタを更新する。
4. `04_api_openapi.yaml` を変えたら、App で `npm run gen:api` を実行して型を作り直す。
