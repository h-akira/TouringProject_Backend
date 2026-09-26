# ユニットの定義

プロジェクトを構成するユニットと、それぞれの責務・担当するユーザーストーリー（[00_user_stories.md](00_user_stories.md)）。
ユニット間の約束は [03_units_contracts.md](03_units_contracts.md)。

## 1. ユニット一覧

| ユニット | 中身 | リポジトリ | 公開 | IaC / 配布 | リージョン |
|---|---|---|---|---|---|
| App | Android アプリ（React Native / Expo） | `TouringProject_App` | public | Play の内部テスト | — |
| Backend | API と事実の確定・音声の処理 | `TouringProject_Backend` | public | SAM | `ap-northeast-1` |
| Agent | 会話・回答の生成・Web 検索 | `TouringProject_Agent` | public | CDK（`agentcore` CLI） | `us-east-1` |
| CICD | Agent・Backend のデプロイの仕組み | `TouringProject_CICD` | private | CloudFormation | — |

各ユニットは親リポジトリ `TouringProject` の同じ名前のディレクトリに submodule として置かれる。

learning（学習教材）と research（調査の記録）も別リポジトリだが、ユニットではない（アプリの設計の対象外）。

## 2. App

| 責務 |
|---|
| インカムのボタン（`VOICE_COMMAND`）で起動し、もう一度の押下か録音の上限で送信する |
| インカムのマイクへの経路を張り、録音する |
| 位置を監視し、進行方位のための2点目を履歴から選ぶ |
| 質問を送り、回答をポーリングで取りに行き、読み上げの音声を再生する |
| 会話の `sessionId` を保持し、リセットで捨てる |
| 応答後に設定で選んだアプリ（ナビ）へ戻る |
| API キーを端末に安全に保管する（設定画面） |

| US | 担当する範囲 |
|---|---|
| US-1.01 | 現在地を取得して質問に添える |
| US-1.02 | `sessionId` を保持して次の質問で送る |
| US-1.03 | Android 実機で動く |
| US-1.05 | `sessionId` を捨てて会話を区切る |
| US-2.01 / 2.02 | 録音と再生 |
| US-2.03 | 2点目の選択 |
| US-2.04 | ハンズフリーの起動・終話・ナビへの復帰 |

## 3. Backend

| 責務 |
|---|
| API の入口（API キー・流量制限・入力の検証） |
| 座標から住所を確定し、2点から進行方位を算出し、現在日時と合わせてプロンプトを組む |
| 音声を受けて STT（Transcribe）を呼ぶ。回答の読み上げ（Polly）を作る |
| Agent を呼び、回答を非同期で返す（SQS・DynamoDB） |
| 会話の `sessionId` を初回に発行する |

| US | 担当する範囲 |
|---|---|
| US-1.01 | 住所の確定 |
| US-1.02 | 初回の `sessionId` の発行と Agent への受け渡し |
| US-2.01 / 2.02 | STT / TTS |
| US-2.03 | 方位の算出 |

## 4. Agent

| 責務 |
|---|
| `sessionId` ごとに会話を保持する |
| 回答を生成する（読み上げる前提の文体） |
| 必要なら Web 検索する |
| ツールを足す形で拡張できる構造を保つ |

| US | 担当する範囲 |
|---|---|
| US-1.02 | 会話の保持 |
| US-1.04 | Web 検索 |
| US-X.01 | 将来、ツールとして足す |

## 5. CICD

| 責務 |
|---|
| Agent と Backend のデプロイ |
| App の Play 配信の設定（Google Cloud・Play Console・GitHub の設定手順） |

直接担当する US は無い。
