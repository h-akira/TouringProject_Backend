# AGENTS.md — Backend

ツーリング AI 会話アプリの API（AWS SAM・Python）。質問を受けて事実（住所・方位・日時）を確定し、Agent（AgentCore）に渡して回答を非同期で返す。音声の文字起こし（Transcribe）と読み上げ（Polly）も担う。
親リポジトリ `TouringProject` の submodule。共通の規約は親の `AGENTS.md`（親の中で作業しているときは既に読まれている）。

## どこに何があるか

| 場所 | 中身 |
|---|---|
| `docs/` | Backend の設計（現在の姿だけ） |
| `docs-parent/` | 親の `docs/` の写し（要件・技術方針・契約・OpenAPI）。⚠️ 編集しない。更新は親の `docs/sync.sh` |
| `README.md` | 使い方（テスト・デプロイ・API キー・動作確認・困ったとき） |

Backend に固有の ADR は無い（必要になったら `adr/` を作る。書き方は親の `AGENTS.md`）。
コードのコメントから参照してよいのは、`docs/`・`docs-parent/` と research の絶対 URL だけ（learning・親の adr は参照しない）。

## 前提

- ⚠️ リージョンは `ap-northeast-1`（東京）。呼び出す Agent は `us-east-1` にあるので、クライアントのリージョンを明示する（`AGENT_REGION`）。混同すると動かない。
- リソースの命名は契約（`docs-parent/03_units_contracts.md` UC-1）に従う。
- Agent・App との約束（Runtime ARN の受け渡し・API・呼び出しの形）は契約の UC-3〜UC-5。片方だけ変えない。
- LLM は Backend から直接呼ばない（Agent の担当）。LLM に推測させず確定できる事実は Backend で確定させる。

## 作業の規則

- ⚠️ デプロイと実機での確認はユーザーが行う。AI は `sam deploy` を実行しない。
- ⚠️ AWS の認証情報・アカウント ID・実際の API の URL を書かない（`samconfig.toml` にも）。`samconfig.toml` に置くのは SSM パラメータの名前だけ。
- テストは `python3 -m pytest tests/ -q`。コードを変えたら実行し、結果を報告する。
- 契約（UC）に触れる変更は、親の `docs/` を先に直してから `docs/sync.sh` で `docs-parent/` を更新する。

## 落とし穴

- ⚠️ API Gateway は未定義のパスに 404 ではなく 403 を返す。403 は API キーの誤りとデプロイ漏れの両方を疑い、`GET /health`（キー不要）で切り分ける。
- クォータはポーリングも消費する（1問≒11回）。`ThrottleRate` を1にするとポーリングで自分の質問が弾かれる。
- ⚠️ SQS は少なくとも1回配信する。二重処理は Agent の二重課金になるので、条件付き書き込み（`store.claim()`）と「worker の Timeout（120秒）＜ 可視性タイムアウト（180秒）」を崩さない。
- ⚠️ Runtime の ARN はデプロイ時に焼き込まれる。Agent の ARN が変わったら Backend も再デプロイする。
- `--parameter-overrides` は `samconfig.toml` の指定と併合されず置き換わる。
- 座標と住所は CloudWatch のログに残る。ログに出す内容を増やすときは気をつける。
