# Backend/ — AWS バックエンド（SAM）

> 📌 **このリポジトリは [TouringProject](https://github.com/h-akira/TouringProject) の submodule。**
> 設計・経緯（`docs/` `adr/` `pre-research/`）と、**分離（2026-09-25）より前の git 履歴**は親リポジトリにある。

ツーリングAI会話アプリのサーバー側。**AWS SAM** で管理する。
設計の全体像は [docs/01_architecture.md](https://github.com/h-akira/TouringProject/blob/main/docs/01_architecture.md) を参照。

## 現在のステージ：非同期での中継（US-1.01・1.03・2.03）

`POST /ask` は**質問と現在地を受け取り、キューに積んで即座に返す**。
回答の生成・会話の記憶・Web検索は**エージェント側の仕事**で、このLambdaは
**入口の門番**（検証して渡す）に徹する（[pre-research/agentcore/AUTH.md](https://github.com/h-akira/TouringProject/blob/main/pre-research/agentcore/AUTH.md)）。

⚠️ **回答は `POST /ask` では返らない。** API Gateway の29秒上限に対し
エージェントが最悪25.5秒かかるため、**待たない形に変えた**（[docs/01a](https://github.com/h-akira/TouringProject/blob/main/docs/01a_async_ask.md)）。
アプリは `GET /ask/{requestId}` を叩いて回答を取りに来る。

> ⚠️ **アプリで 403 が出たら、原因は2つある。**
> ① **APIキー**（未設定・誤り。アプリの設定画面で確認する。下記「APIキー」）
> ② **デプロイ漏れ**（API Gateway は**未定義のパスに 404 ではなく 403 を返す**ため、
> エンドポイントを追加してデプロイしていないと認証エラーのように見える）
>
> **切り分けは `/health`**（キー不要）。ここが 200 なら API は生きているので、
> 403 の原因はキーかデプロイ漏れのどちらか。

```
Backend/
  template.yaml            SAM定義（API Gateway + APIキー/Usage Plan + Lambda + SQS + DynamoDB + IAM）
  samconfig.toml           デプロイ設定（スタック名・リージョン・パラメータ）
  src/
    handlers/
      ask.py               POST /ask（住所・方位を確定してキューに積む）
      worker.py            SQS経由で起動し、AgentCoreを呼んで結果を保存
      result.py            GET /ask/{requestId}（アプリがポーリングする先）
      geocode.py           座標→住所（Amazon Location Service）
    lib/
      agent.py             AgentCore の呼び出しとSSEの組み立て
      store.py             DynamoDB アクセス（⚠️ 二重処理を防ぐ条件付き書き込み）
      geo.py               2点間の方位・距離
  tests/                   ハンドラ・ライブラリのテスト（AWS呼び出しはスタブ）
  events/
    ask-post.json          sam local invoke 用（新規会話）
    ask-post-session.json  同（sessionId 付き＝会話の継続）
```

> ⚠️ **SQSは「少なくとも1回」配信。** 同じ質問が2回届くと**AgentCoreを2回呼んで二重課金**になる。
> `store.claim()` の条件付き書き込みで2つ目を弾いている（[docs/03](https://github.com/h-akira/TouringProject/blob/main/docs/03_dynamodb_table.md) §4）。
> **worker の Timeout(120秒) < 可視性タイムアウト(180秒)** の関係も崩さないこと。

> ⚠️ **リージョンが分かれている。** このスタックは `ap-northeast-1`（東京）だが、
> 呼び出す AgentCore Runtime は **`us-east-1`**（Web検索コネクタがそこ限定のため）。
> Lambdaは `AGENT_REGION` で明示的に us-east-1 を指す。

### 音声（`POST /ask-audio`）

**STT/TTS はこのスタックが担う。** アプリは録音した音声を送るだけで、
**Lambdaが Transcribe（バッチ）と Polly を呼ぶ**（[docs/01](https://github.com/h-akira/TouringProject/blob/main/docs/01_architecture.md) §7）。
既存の `POST /ask`（テキスト）は変わらない。

音声の方式は**「手前でSTT」**（[pre-research/voice/](https://github.com/h-akira/TouringProject/blob/main/pre-research/voice/)）。
Nova 2 Sonic（音声→音声）は日本語非対応のため採用しなかった。

#### ⚠️ 疎通確認（アプリ無しで試す）

**デプロイが通っても動く保証はない。** この経路の失敗は
**デプロイ時ではなく実行時に出る**ので、一度は通しで確かめる。

```sh
# 録音（M4A）を用意する。pre-research/voice/recordings/ はWAVなので変換が要る
ffmpeg -i input.wav -c:a aac -ar 16000 -ac 1 question.m4a

curl -X POST "https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/ask-audio" \
  -H "x-api-key: <APIキー>" \
  -F "audio=@question.m4a;type=audio/mp4" \
  -F 'location={"start":{"latitude":35.681,"longitude":139.767}}'
# → 202 と requestId が返る

curl "https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/ask/<requestId>" \
  -H "x-api-key: <APIキー>"
# → pending がしばらく続き、done になると answer と audioUrl が入る
```

⚠️ **`audioUrl` はAPIキー不要**（S3の署名付きURL・数分で失効）。そのまま `curl -o` で落とせる。

**うまくいかないときに見る順**:

| 症状 | 見るところ |
|---|---|
| 202が返らない | `lambda-trg-dev-ask-audio` のログ。⚠️ **413なら録音が長すぎる** |
| `pending` のまま | ⚠️ **`transcripts/` が出ているか**をS3で確認 → 出ていなければ Transcribe のロール権限 |
| `error` になる | `lambda-trg-dev-transcribe-done` のログ（無音・認識失敗もここ） |
| `audioUrl` が無い | `lambda-trg-dev-worker` のログ（Pollyの失敗。⚠️ **回答自体は返る**） |

### テスト

```sh
cd Backend
pip install -r requirements-dev.txt   # 初回だけ
python3 -m pytest tests/ -q
```

⚠️ **`requirements-dev.txt` はテスト専用**で、Lambdaには入らない。
ハンドラは標準ライブラリと `boto3` しか使っておらず、**`boto3` はLambdaランタイムが持っている**ため
デプロイ時にインストールするものは無い。
⚠️ **ただしテストを動かす側には必要**（`tests/test_store.py` が `botocore` を直接importする）。

> API仕様（OpenAPI）は**フロント↔バックの契約**なので `docs/02_api_openapi.yaml` に置いている。
> 現時点では契約・ドキュメント・型生成の源として持ち、API Gateway の `DefinitionBody` には
> 組み込んでいない（仕様が固まったらリクエスト検証用に昇格可能）。

## 前提ツール

- AWS SAM CLI / AWS CLI（導入済み）
- Python 3.13（Lambdaランタイムと合わせる）
- Docker（`sam local` でのローカル実行に使う）

## ローカルで動かす（デプロイ不要で試す）

⚠️ **今のステージでは AgentCore を実際に呼ぶので、AWSの認証情報が必要**
（モックだった頃と違い、権限なしでは動かない）。

```sh
cd Backend
sam build

# AgentCore Runtime の ARN を取得（実値はコミットしないこと）
export AGENT_ARN=$(AWS_PROFILE=touring aws bedrock-agentcore-control \
  list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[0].agentRuntimeArn' --output text)

# 1件だけ実行してみる
AWS_PROFILE=touring sam local invoke AskFunction \
  --event events/ask-post.json \
  --parameter-overrides "AgentRuntimeArn=$AGENT_ARN"
```

`requestId` と `sessionId` を含むJSONが**すぐに**返れば成功（`statusCode` は **202**）。
**同じ `sessionId` を送れば会話が続く**（`events/ask-post-session.json` を参照）。

⚠️ **回答はここでは返らない。** `AskFunction` はキューに積むだけなので、
ローカルで回答まで確かめるには SQS と DynamoDB が要る。**実際の確認はデプロイ後に行う**。

> 📌 **初回は10秒前後かかる。** AgentCore のコンテナ起動（コールドスタート）のため。
> 2回目以降は2〜3秒（実測値は [pre-research/voice/](https://github.com/h-akira/TouringProject/blob/main/pre-research/voice/) §6）。
> この待ち時間は `WorkerFunction`（`Timeout` 120秒）が引き受ける。
> **アプリから見た待ち時間は変わらない**（タイムアウトしなくなるだけ）。

> 実機スマホから Mac のローカルAPIに繋ぐ場合は、GPSのときと同様にネットワーク到達性
> （同一Wi-Fi・ファイアウォール）に注意。必要なら一旦AWSにデプロイして試す。

## AWS にデプロイする（実機から試すとき）

> 📌 **通常は手で叩かなくてよい。** このリポジトリの `main` にpushすれば CodeBuild が
> テスト → デプロイする（`buildspec.yml`。CodeBuild 自体は TouringProject_CICD）。
> ⚠️ **Agent の Runtime ARN が変わったときは、Agent のビルドがこのビルドを起動する。**
> 以下は**手元から直接デプロイしたいとき**の手順。

デプロイ設定は `samconfig.toml` に記述済み（スタック名・リージョン・パラメータ）。
そのため**引数なしでデプロイできる**:

```sh
cd Backend
sam build
sam deploy
```

📌 **変更内容（changeset）が表示され、`y` の入力を求められる。**
手で流すときは**何が変わるか見てから進む**方が安全なので、そのままにしてある
（`samconfig.toml` の `confirm_changeset`）。⚠️ **CI では `--no-confirm-changeset` で自動化している**。

⚠️ **`AgentRuntimeArn` に渡しているのはARNではなく、SSMパラメータの「名前」**
（`/trg/dev/agent-runtime-arn`）。CloudFormationがそれを解決して値を取るので、
**名前にアカウントIDは含まれず** `samconfig.toml` にコミットできる。

⚠️ **Agentを先にデプロイしておく必要がある**（そのパラメータを書くのはAgent側）。
無いと `Parameter /trg/... not found` で失敗する。手元でやるなら:

```sh
cd Agent && AWS_REGION=us-east-1 agentcore deploy -y

# ⚠️ 書き込むのは「東京」。読む側（SAM）がそこを見るため
AGENT_ARN=$(AWS_PROFILE=touring aws bedrock-agentcore-control \
  list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[0].agentRuntimeArn' --output text)
AWS_PROFILE=touring aws ssm put-parameter \
  --name /trg/dev/agent-runtime-arn --value "$AGENT_ARN" \
  --type String --overwrite --region ap-northeast-1
```

デプロイ後、出力される `ApiBaseUrl` に `/ask` を付けたURLがエンドポイント。

### 🔑 APIキー

**このAPIはAPIキーが無いと叩けない**（`/health` を除く）。
キーはスタックが**自動生成する**ので、デプロイ後に値を取り出してアプリに入れる。
名前は `apikey-trg-<env>-main`。

> 📌 **キーとUsage Planはテンプレートに明示的に書いている。** SAMに任せると
> `stack--RestA-<ランダム>` のような名前になり、**SAMには名前を指定する術がない**ため。
>
> ⚠️ **キーの値が変わったら、アプリの設定画面で入れ直す。**
> 値はAPI Gatewayが生成するので、キーを作り直せば別の値になる。

⚠️ **キーの値はスタックの Outputs に出していない。**
Outputs は `describe-stacks` の権限があれば誰でも読めるうえ、CIのログにも残るため。
出しているのは**キーのID**だけで、値は次のコマンドで取る:

```sh
# キーのIDを取得 → その値を引く（--include-value が無いと値は返らない）
AWS_PROFILE=touring aws apigateway get-api-key \
  --api-key "$(AWS_PROFILE=touring aws cloudformation describe-stacks \
      --stack-name stack-trg-dev-main --region ap-northeast-1 \
      --query "Stacks[0].Outputs[?OutputKey=='ApiKeyId'].OutputValue" --output text)" \
  --include-value --region ap-northeast-1 \
  --query value --output text
```

⚠️ **出てきた値はコミットしない**（公開リポジトリの鉄則）。
アプリの**設定画面に貼り付ける**と `expo-secure-store` に保管される。

**動作確認:**

```sh
# キー無し → 403
curl -s -o /dev/null -w "%{http_code}\n" https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/ask

# キー付き → 400（bodyが空なので。403でなければ認証は通っている）
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "x-api-key: <キー>" \
  https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/ask

# /health はキー不要 → 200
curl -s -o /dev/null -w "%{http_code}\n" https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod/health
```

> ⚠️ **モバイル回線でも通る。** 以前のIP制限と違い、キーは回線に依存しない。
> **スマホを同じWi-Fiに繋ぐ必要はもう無い。**

### 流量制限（Usage Plan）

キー単位で上限をかけている。**値はすべて仮**で、デプロイ時に変えられる。

| パラメータ | 既定値 | 意味 |
|---|---|---|
| `DailyQuota` | 2000 | **1日あたりの呼び出し回数** |
| `ThrottleRate` | 5 | 毎秒の定常レート |
| `ThrottleBurst` | 10 | 瞬間的な上限 |

⚠️ **クォータは「問い数」ではなく「呼び出し回数」。**
1問 ＝ `POST` 1回 ＋ ポーリングの `GET` 約10回なので、**2000回 ≒ 180問/日**。
「1日200問」のつもりで200にすると**約18問で打ち止め**になる。

⚠️ **`ThrottleRate` を1にしてはいけない。** アプリのポーリングは**1秒間隔**なので、
自分の質問がレート上限に当たる。

```sh
# 上限を変えて再デプロイする例
# ⚠️ --parameter-overrides を付けると samconfig.toml の指定は「併合されず置き換わる」。
#    変えない値も明示的に並べること（省くとテンプレートの既定値に戻る）。
sam deploy --parameter-overrides \
  "Environment=dev" "AgentRuntimeArn=/trg/dev/agent-runtime-arn" "DailyQuota=5000"
```

上限に達すると **429** が返る。アプリは「利用上限に達しました」と表示する
（⚠️ ポーリング中の429は一時的なものとして**そのまま再試行する**）。

### 必要なIAM権限（Lambda実行ロール）

`template.yaml` の `Policies` で以下を付与済み。**無いと `AccessDenied` になる。**

| 権限 | 用途 |
|---|---|
| `bedrock-agentcore:InvokeAgentRuntime` | エージェントの呼び出し |
| `geo-places:ReverseGeocode` | 座標→住所（`handlers/geocode.py`） |

- AgentCore側の対象はRuntimeのARNと、その配下（`/runtime-endpoint/*`）の**両方**。
  実際に呼ばれるのは後者なので、片方だけでは足りない。
- `geo-places` はリソース単位のARNを持たないので `Resource: "*"`（アクション側で絞る）。

### なぜ座標→住所をLambdaでやるのか

**LLMは緯度経度から場所を正しく言い当てられない**（実機で約40km離れた市を答えた）。
そのため住所はLambdaで確定させ、事実としてエージェントに渡している。
詳細と検証結果は [pre-research/geocoding/](https://github.com/h-akira/TouringProject/blob/main/pre-research/geocoding/)。

### 命名規約

リソースは `<リソースタイプ>-trg-<env>-<識別子>` で命名する。
- `trg` = touring、`env` = `dev`/`prod`（`Environment` パラメータ）
- 識別子: 単一/メインは `main`、複数あり得るものは用途名
- 例: スタック `stack-trg-dev-main` / Lambda `lambda-trg-dev-ask` / API Gateway `apigw-trg-dev-main`

### デプロイに必要な権限（初回の注意）

`sam deploy` は、コードを置くS3バケットへの書き込みや CloudFormation 実行の権限を要する。
権限不足だと `AccessDenied ... s3:PutObject` 等が出る。デプロイに使うIAMユーザー/ロールに、
CloudFormation・S3（SAM管理バケット）・Lambda・API Gateway・IAM の必要な権限を付与しておくこと。
（ローカル実行 `sam local` はこれらの権限を必要としない。）

## 今後このLambdaに足すもの

**次にやることは親リポジトリの `.memory/todo.md` を見ること。** ここには「このLambdaの担当範囲」だけ挙げる。

- **コスト暴走対策**: AWS Budgets → 予算超過で自動遮断
  （⚠️ キーとクォータは入ったが、**予算による遮断だけが残っている**）
- **DLQの監視**（`sqs-trg-dev-ask-dlq` に溜まっても気づく手段が無い）

- **音声対応**（`POST /ask-audio` の新設・S3・Transcribe/Polly）。[docs/01](https://github.com/h-akira/TouringProject/blob/main/docs/01_architecture.md) §7

> ⚠️ **LLMの呼び出しはここではない**（AgentCoreの担当）。
