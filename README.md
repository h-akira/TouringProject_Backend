# TouringProject_Backend — ツーリング AI 会話アプリの API（AWS SAM）

App から質問を受け、住所・方位・日時を確定して Agent（AgentCore）に渡し、回答を非同期で返す API。音声の文字起こしと読み上げの音声も作る。
[TouringProject](https://github.com/h-akira/TouringProject) の submodule。リージョンは `ap-northeast-1`（東京）。

| 見たいもの | 場所 |
|---|---|
| 設計 | [docs/](docs/README.md) |
| プロジェクト全体の要件と契約 | [docs-parent/](docs-parent/README.md)（親リポジトリの写し。編集しない） |
| API 仕様 | [docs-parent/04_api_openapi.yaml](docs-parent/04_api_openapi.yaml) |

## 構成

```
template.yaml      SAM の定義（API Gateway・API キー/Usage Plan・Lambda・SQS・DynamoDB・S3・IAM）
samconfig.toml     デプロイ設定（スタック名・リージョン・パラメータ）
buildspec.yml      CodeBuild の手順書
src/handlers/      Lambda の入口（ask・ask_audio・transcribe_done・worker・result・geocode・health）
src/lib/           Agent の呼び出し・プロンプトの組み立て・方位・DynamoDB・音声
tests/             テスト（AWS の呼び出しはスタブ）
events/            sam local invoke 用のイベント
```

## 前提ツール

- AWS SAM CLI / AWS CLI
- Python 3.13（Lambda のランタイムと合わせる）
- Docker（`sam local` に使う）

## テスト

```sh
pip install -r requirements-dev.txt   # 初回だけ
python3 -m pytest tests/ -q
```

`requirements-dev.txt` はテスト専用で、Lambda には入らない（ハンドラは標準ライブラリと、ランタイムにある `boto3` しか使わない）。

## デプロイ

Agent を先にデプロイしておく（Runtime の ARN を SSM から読むため）。Agent の Runtime の ARN が変わったら、こちらも再デプロイする。

```sh
sam build
sam deploy
```

- デプロイ設定は `samconfig.toml` にあるので引数は要らない。変更内容（changeset）を見てから `y` で進める。
- `AgentRuntimeArn` に渡しているのは ARN ではなく SSM パラメータの名前（`/trg/dev/agent-runtime-arn`）。無いと `Parameter /trg/... not found` で失敗する。
- ⚠️ `--parameter-overrides` を付けると `samconfig.toml` の指定は併合されず置き換わる。変えない値も並べる。

```sh
# 例: 日次クォータを変える
sam deploy --parameter-overrides \
  "Environment=dev" "AgentRuntimeArn=/trg/dev/agent-runtime-arn" "DailyQuota=5000"
```

SSM パラメータを手で書くとき（Agent を手元でデプロイした場合）:

```sh
AGENT_ARN=$(AWS_PROFILE=touring aws bedrock-agentcore-control \
  list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[0].agentRuntimeArn' --output text)
# ⚠️ 書き込むのは東京（読む側の SAM がそこを見る）
AWS_PROFILE=touring aws ssm put-parameter \
  --name /trg/dev/agent-runtime-arn --value "$AGENT_ARN" \
  --type String --overwrite --region ap-northeast-1
```

デプロイする IAM ユーザー/ロールには、CloudFormation・S3（SAM が管理するバケット）・Lambda・API Gateway・IAM の権限が要る。

## API キー

`/health` 以外は API キーが要る。キーはスタックが作り、値は Outputs に出していないので次で取り出す（⚠️ 値はコミットしない）。

```sh
AWS_PROFILE=touring aws apigateway get-api-key \
  --api-key "$(AWS_PROFILE=touring aws cloudformation describe-stacks \
      --stack-name stack-trg-dev-main --region ap-northeast-1 \
      --query "Stacks[0].Outputs[?OutputKey=='ApiKeyId'].OutputValue" --output text)" \
  --include-value --region ap-northeast-1 --query value --output text
```

App の設定画面に貼り付ける。キーを作り直すと値が変わるので、App でも入れ直す。
API の URL は Outputs の `ApiBaseUrl`。

## 動作確認

```sh
BASE=https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/Prod

curl -s -o /dev/null -w "%{http_code}\n" $BASE/health                 # 200（キー不要）
curl -s -o /dev/null -w "%{http_code}\n" $BASE/ask                    # 403（キー無し）
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "x-api-key: <キー>" $BASE/ask   # 400（本文が空。403 でなければ認証は通っている）
```

音声の経路を通しで確かめる（失敗はデプロイ時ではなく実行時に出る）:

```sh
ffmpeg -i input.wav -c:a aac -ar 16000 -ac 1 question.m4a
curl -X POST "$BASE/ask-audio" -H "x-api-key: <キー>" \
  -F "audio=@question.m4a;type=audio/mp4" \
  -F 'location={"start":{"latitude":35.681,"longitude":139.767}}'   # 202 と requestId
curl "$BASE/ask/<requestId>" -H "x-api-key: <キー>"                  # pending → done（answer と audioUrl）
```

`audioUrl` は S3 の署名付き URL（キー不要・数分で失効）。

## ローカルで動かす

`AskFunction` は Agent を実際に呼ぶ準備（プロンプトの組み立てとキューへの投入）をするので、AWS の認証情報が要る。

```sh
sam build
export AGENT_ARN=$(AWS_PROFILE=touring aws bedrock-agentcore-control \
  list-agent-runtimes --region us-east-1 \
  --query 'agentRuntimes[0].agentRuntimeArn' --output text)   # 実値はコミットしない
AWS_PROFILE=touring sam local invoke AskFunction \
  --event events/ask-post.json --parameter-overrides "AgentRuntimeArn=$AGENT_ARN"
```

`requestId` と `sessionId` を含む 202 がすぐ返れば成功。回答はここでは返らない（SQS と DynamoDB が要るので、デプロイ後に確かめる）。

## 困ったとき

| 症状 | 見るところ |
|---|---|
| 403 | ① API キーの誤り ② デプロイ漏れ（API Gateway は未定義のパスに 404 ではなく 403 を返す）。`/health` が 200 なら API は生きている |
| 429 | Usage Plan の上限。クォータはポーリングも消費する（1問≒11回） |
| `/ask-audio` が 202 を返さない | `lambda-trg-dev-ask-audio` のログ。413 なら録音が長すぎる（2 MiB） |
| `pending` のまま | S3 の `transcripts/` が出ているか。出ていなければ Transcribe のロールの権限 |
| `error` になる | `lambda-trg-dev-transcribe-done` のログ（無音・認識の失敗もここ） |
| `audioUrl` が無い | `lambda-trg-dev-worker` のログ（Polly の失敗。回答自体は返る） |
| `AccessDenied` | Lambda の実行ロールの権限（`template.yaml` の `Policies`） |
