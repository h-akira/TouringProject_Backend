<!-- AI instruction (pinned): UC numbers are referred to from other documents and code comments. Never renumber them; leave gaps if one is removed. -->

# ユニット間の契約

ユニット（[02_units_definition.md](02_units_definition.md)）どうしの約束。片方だけを変えるともう片方が壊れるものを置く。
ここを変えたら、関係するユニットを両方直す。

## UC-1 管理規約

### リソースの命名

`<リソースタイプ>-trg-<env>-<識別子>`

- `trg` = touring。`env` = `dev` / `prod`。識別子は、単一なら `main`。
- 例: `stack-trg-dev-main`・`apigw-trg-dev-main`・`lambda-trg-dev-ask`・`lambda-trg-dev-worker`・`dynamodb-trg-dev-main`・`sqs-trg-dev-ask`・`sqs-trg-dev-ask-dlq`・`usageplan-trg-dev-main`・`apikey-trg-dev-main`
- S3 のバケット名は全世界で一意なので、末尾にアカウント ID を付ける（`s3-trg-dev-audio-<ACCOUNT_ID>`。ID はテンプレートが埋める）。
- AgentCore のリソース名はハイフンを使えないので、アンダースコアかキャメルケースにする（Runtime `agentcore_trg_dev_ask`・Gateway `gwTrgDevMain`）。
- SSM パラメータ: `/trg/<env>/<名前>`。

### リポジトリ

| ユニット | GitHub | 親での submodule のパス | 公開 |
|---|---|---|---|
| App | `h-akira/TouringProject_App` | `App/` | public |
| Backend | `h-akira/TouringProject_Backend` | `Backend/` | public |
| Agent | `h-akira/TouringProject_Agent` | `Agent/` | public |
| CICD | `h-akira/TouringProject_CICD` | `CICD/` | private |

親の `docs/` は各ユニットに `docs-parent/` として写される（編集しない）。

## UC-2 リージョンとモデル

| | 値 |
|---|---|
| Backend のリージョン | `ap-northeast-1` |
| Agent のリージョン | `us-east-1` |
| モデル ID | `us.anthropic.claude-sonnet-4-6` |

- ⚠️ `jp.` の推論プロファイルは ap-northeast 専用で、us-east-1 から呼ぶと `The provided model identifier is invalid` になる。
- ⚠️ Backend（東京）から Agent（us-east-1）を呼ぶときは、クライアントのリージョンを明示する。既定のままだと東京を見に行き `ResourceNotFoundException` になる。

## UC-3 Agent → Backend: Runtime ARN の受け渡し

| | |
|---|---|
| パラメータ | SSM `/trg/<env>/agent-runtime-arn` |
| 置き場所 | ⚠️ 東京（`ap-northeast-1`）に書く |
| 書く側 | Agent のデプロイ後 |
| 読む側 | Backend の SAM が `AWS::SSM::Parameter::Value<String>` で解決し、Lambda の環境変数と IAM ポリシーに焼き込む |

- デプロイは Agent → Backend の順。
- ⚠️ ARN が変わったら（初回・ランタイム名の変更）、Backend も再デプロイする。しないと古いランタイムを呼び続ける。
- CloudFormation のエクスポートは使えない（`Fn::ImportValue` はリージョンを跨げない）。

## UC-4 App ↔ Backend: API

正本は [04_api_openapi.yaml](04_api_openapi.yaml)。ここではその外側の約束を書く。

### 認証

- API キーをヘッダ `x-api-key` で送る。全エンドポイントで必須で、`GET /health` だけ不要。
- キーはアプリの設定画面から入力し、端末に安全に保管する。アプリのビルド（`.env`）には入れない。

### 非同期の受け取り

- `POST /ask`（テキスト）と `POST /ask-audio`（音声）は 202 と `requestId`・`sessionId` を返す。回答は返さない。
- 回答は `GET /ask/{requestId}` をポーリングして取る。`status` は `pending` / `done` / `error` の3つ。`done` なら回答のテキストと音声の署名付き URL が載る。音声の質問は、文字起こしが終わると状態によらず `transcript`（聞き取った文。何も聞き取れなければ空文字）も載る。

⚠️ ポーリングは必ず止まるように作る。間隔は経過時間で延ばす。

| 経過 | 間隔 | 回数 |
|---|---|---|
| 0〜30秒 | 1秒 | 30回 |
| 30〜60秒 | 2秒 | 15回 |
| 60〜120秒 | 4秒 | 15回 |
| 120秒 | 打ち切り | 計60回 |

- 画面を離れた・会話をリセットしたら、即座に止める。
- 一時的な通信断と 429 は無視して打ち切りまで続ける。403 は即座に諦める。
- 打ち切りは音声を基準にしている（音声は15〜20秒。テキストは10〜13秒）。打ち切った後も Backend の処理は続く。

### 入力の上限

| | 上限 |
|---|---|
| 質問（テキスト） | 500文字 |
| 音声 | 2 MiB（録音の長さの上限を兼ねる） |
| `sessionId` | 33〜128文字（AgentCore の下限が33文字） |

### 音声

- 録音は M4A（`outputFormat: 'mpeg4'` ＋ `audioEncoder: 'aac'`、`audio/mp4`）。Transcribe が受け付け、Android が変換なしで出せる形式。
- 回答の音声は S3 から署名付き URL で直接取る（有効期限は数分）。

### 流量

- API キーの日次クォータとレート制限は、ポーリングも消費する。1問は `POST` 1回＋`GET` 約10回で、約11回の呼び出しになる。
- レートは、ポーリングの最短間隔（1秒）で弾かれない値にする。

## UC-5 Backend → Agent: 呼び出し

### 呼び出し

`bedrock-agentcore` の `invoke_agent_runtime` を、リージョンを明示して呼ぶ（UC-2）。

| 引数 | 値 |
|---|---|
| `agentRuntimeArn` | UC-3 の ARN |
| `runtimeSessionId` | 会話の `sessionId`（33文字以上） |
| `payload` | `{"question": "<プロンプト>", "location": {...}}` の JSON。Agent は `question` の代わりに `prompt` キーも受ける |

### `location`（現在地のデータ）

周辺の場所を探すツールが読む。座標を LLM にプロンプトから書き写させないため、プロンプトとは別に数値で渡す。位置が無い質問では省く。

| キー | 型 | 必須 | 値 |
|---|---|---|---|
| `latitude` | number | ○ | 現在地（App の `start`） |
| `longitude` | number | ○ | 同上 |
| `headingDegrees` | number | | 進行方位（真北から時計回りの度）。プロンプトの「進行方向」と同じ値で、算出できたときだけ |

### プロンプトの形

Backend が組み、Agent はそのまま LLM に渡す。

```
【現在日時: 2026年9月26日（土）14時05分（日本時間）】
【この質問は、会話の最初の質問から約N分後のものです】      ← 1分以上経っているときだけ
現在地: 緯度 <lat>, 経度 <lon>
現在地の住所: <住所>（この住所は正確です。自分で座標から推測しないこと）   ← 解決できたときだけ
進行方向: <方位>（真北から<度>度）                          ← 算出できたときだけ
ライダーから見て右手は<方位>、左手は<方位>の方角

質問: <質問の本文>
```

- 位置が無い質問は、現在日時と `質問: <本文>` だけになる。
- `<住所>` は都道府県・市区町村・政令市の区・町名まで（例: `神奈川県箱根町湯本`・`北海道札幌市北区北6条西`）。番地・建物名は含めない。
- ⚠️ `質問: ` は区切りでもある。Agent は最後の `質問: ` より後ろを質問の本文としてログに出す。
- Agent のシステムプロンプトは、【現在日時】と方角・左右をここに書かれたとおりに使い、自分で推測・計算し直さないよう指示する。住所の扱いはプロンプトの行の中で指示する（上の括弧書き）。
- 進行方向の行が無いときは、方角や左右に触れずに答える（Agent 側の約束）。

### 応答

- Strands のイベントの SSE（`data: {...}` の行）で返る。Backend は `event.contentBlockDelta.delta.text` を順に連結して回答にする。ただし最後のツール呼び出し（`event.contentBlockStart.start.toolUse`）より前のテキストは捨てる（検索の前置きを読み上げないため）。
- Agent がペイロードを受け付けないときは `{"error": "..."}` を返す。Backend はこれを失敗として扱う。

### 会話の継続

- `sessionId` は、最初の質問で Backend が発行し、App に返す。App が保持して次の質問で送り、Backend がそのまま `runtimeSessionId` に使う。
- Agent は `runtimeSessionId` ごとに会話の履歴をプロセス内に持つ（直近の一定の件数まで）。15分以上あけた継続は保証しない。

## UC-6 CI/CD との関係

- Agent・Backend は、CICD リポジトリ（非公開）の CodeBuild がデプロイする。手順書は各リポジトリの `buildspec.yml`。
- App は `v*` タグの push で、GitHub Actions が Play の内部テストへ配信する。
