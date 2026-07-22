# mandatehub — システム概要と商品カタログ

> **一言でいうと**：自律 AI エージェントが「予算枠（マンデート）の範囲内でのみ・二重支払い不能・証明付き」でお金を払える x402 決済レイヤー。その上で、機械が読める価値あるデータ／検証を **1 コール ≈ $0.01 の実 USDC** で売る、稼働中のサービス。

- 📦 ライブラリ: `pip install mandatehub` — <https://pypi.org/project/mandatehub/>
- 🌐 紹介サイト: <https://mandatehub.ichiyanagi1111.workers.dev>
- 🟢 稼働サービス: <https://mandatehub.obolpay.xyz> （Base メインネット・実 USDC・24/365）
- 📚 ソース: <https://github.com/Hiroshi-Ichiyanagi/mandatehub>

---

## 1. これは何か

**mandatehub は「x402 の中に入るマンデート＋証明レイヤー」**です。[x402](https://github.com/coinbase/x402) は
Coinbase の HTTP `402 Payment Required` 決済プロトコル（Base 上の USDC）。mandatehub はその
facilitator の内側で、次の 2 方向を扱います。

- **意図／アカウント抽象化（④）** — **マンデート**は事前入金された予算上限つきの認可
  （「予算を預けて、その枠内でエージェントに使わせる」ERC-4337／セッションキー型）。
  エージェントは枠内でインテントを決済し、`ProofOfMandate` によって
  **「予算を一度も超えていない」ことを誰でもオフラインで検証**できる。
- **最良執行／サープラス回収（③）** — ソルバーオークションで最良の開示コストで約定し、
  価格改善分（サープラス）を整数厳密に分配。`ProofOfBestExecution` /
  `ProofOfSurplusRecapture` を発行。「利用者手数料 0%、なのにシステムは稼ぐ」。

### 設計上の芯（すべての部分が守る規律）

- **決定的＆オフライン検証可能** — 証明・決済の生成はすべて**明示的な時刻**を取り、壁時計
  （`datetime.now()`）を読まない。同じ入力＋同じ時刻 → バイト単位で同一のハッシュ。
- **追記のみの複式簿記** — 金額は整数の最小単位（浮動小数点なし）。全取引が通貨ごとに
  ゼロへ均衡。残高は必ず成立済みエントリから導出。
- **オンチェーン実行なし・HTTP なし・鍵なし（コアは）** — 証明するのは*会計*
  （誰が執行してもマンデート内に収まり、サープラスが公正分配されたこと）。
- **標準ライブラリのみ** — サードパーティ実行時依存ゼロ。

---

## 2. 何がどう動いているか（本番構成）

| 層 | 実体 |
| --- | --- |
| **ライブラリ** | PyPI `mandatehub` 0.1.0（stdlib のみ、EVM 署名は `[evm]` extra）／CI は Python 3.11–3.13 |
| **稼働サービス** | VPS（x402 ゲートウェイと同居）上で `systemd` 常駐、`Restart=always` |
| **公開** | Cloudflare Named Tunnel → `mandatehub.obolpay.xyz`（ポートは直接非公開） |
| **決済** | Coinbase **CDP facilitator**（本番）経由で **Base メインネットの実 USDC** を決済 |
| **台帳** | 追記のみ複式簿記（SQLite、Postgres 対応でマルチワーカー可） |
| **運用** | 毎時バックアップ（監査チェーン検証つき）／5分毎監視／Bazaar 掲載チェック（cron） |
| **堅牢性** | 再起動生存・改ざん検出・ネイティブレート制限・全経路 fail-closed・脅威モデル文書 |

### リクエストのライフサイクル（`402 → pay → settle → proof`）

1. 客（エージェント）が公開 URL を GET → **402** ＋ 支払い条件（`accepts`）が返る。
2. 客が x402 の `exact` スキームで署名した支払いを `X-PAYMENT` で再送。
3. **マンデートゲートが先に判定**（予算・用途・リプレイ・レート）。違反は
   **facilitator に到達する前に無料で拒否**（fail-closed）。
4. 正当なら CDP facilitator が **オンチェーンで決済**。operator は tx を**自分でも独立検証**。
5. 応答に商品データ＋決済 tx＋`chainVerification`＋`ProofOfMandate` が同梱される。

**差別化点**：拒否された支払い（リプレイ・予算超過・レート超過）は
**ネットワーク呼び出しゼロ・オンチェーン動作ゼロ**でコストがかからない。

---

## 3. 商品カタログ（機械が買える 8 系統）

- **価格**：1 コール **0.01 USDC**（Base メインネット）
- **共通仕様**：応答は canonical な `artifact_sha256` 等でバイト再現可能／提供できない時は
  **課金前に 503**（SLA fail-closed、古い・欠損データに課金しない）／各商品は CDP に個別の
  resource URL で記録され、**それぞれ x402 Bazaar に掲載可能**。

| # | エンドポイント | 商品名 | 説明 | 由来資産 |
|---|---|---|---|---|
| 1 | `GET /quote` | **ECB FX 参照レート** | 欧州中央銀行の公式日次為替レート（EUR 基準・約30通貨）を canonical ハッシュ付きで。古い時は課金しない。 | x402-gateway |
| 2 | `GET /product/fx?from=USD&to=JPY&amount=<最小単位>` | **ゼロスプレッド FX 変換＋開示** | 任意の 2 通貨間を ECB レートで変換し、**スプレッド 0bps** を明示開示。整数最小単位・Decimal 演算・ハッシュ付き。 | genesis_finance |
| 3 | `GET /product/qswap?matrix=fidelity\|swap\|both` | **LLM バックエンド選択マトリクス** | Apple Silicon 実測：`llama.cpp`／`mlx`／`candle` 間の忠実度（fidelity 16行）とスワップ遅延・メモリ（9行）。「どのバックエンドを選ぶべきか」を機械が判断するためのデータ。 | qswap |
| 4 | `GET /product/audit-verify?data=<base64 json>` | **監査ログ検証** | 提出されたハッシュ連鎖監査ログを、その署名アンカー（Ed25519／HMAC）＋連鎖整合で検証。「私のログは改ざんされていない」を第三者に有料で証明。 | genesis-keystone |
| 5 | `GET /product/verify-tx?tx=0x<64hex>` | **オンチェーン決済検証** | Base 上の USDC 送金 tx を独立検証（receipt ステータス＋ERC-20 Transfer ログの from/to/金額を解読）。**「検証」そのものを商品化**。 | mandatehub |
| 6 | `GET /product/govern-verify?bundle=genuine\|tampered` または `?data=<base64 zip>` | **govern バンドル検証** | AI 実行の証拠バンドルをオフライン検証（ハッシュ連鎖・Ed25519 receipt・witness binding・STH 整合）。純 Python 検証器。デモ 2 種、または自分のバンドルを base64 zip で提出（≤256KB・zip爆弾/zip-slip 対策済み）。 | govern-open-verify |
| 7 | `GET /product/openunit` | **openunit 評価** | 人口加重の勘定単位（UN-WPP＋WB-PPP ヴィンテージ）の決定的評価。**販売のたびにアーティファクトをライブ再検証**（`reverified_now`）。1 openunit ≈ 2.848 USD。 | openunit |
| 8 | `GET /product/kairos?top=1..300` | **Kairos 収束スコア（日本株）** | 約2000 銘柄の多面的な追い風の「収束」度（KCS）。**静的リサーチ・スナップショット**として明示的な `as_of` 付き。**「ライブ市場データではない・投資助言ではない」**と明記。 | kairos |

### 運用・観測エンドポイント（無料）

| エンドポイント | 内容 |
|---|---|
| `GET /` | サービス案内（人間にはダッシュボード HTML、機械には JSON） |
| `GET /healthz` | 稼働状態・残予算・監査ルート |
| `GET /metrics` | 決済件数・収益・ユニーク支払者・日別内訳 |
| `GET /quote-v2` | x402 **v2** ディスカバリ用チャレンジ（Bazaar 掲載対応・`PAYMENT-REQUIRED` ヘッダ＋`extensions.bazaar`） |

---

## 4. 何が「本物」で何が「まだ」か（正直な状態）

**本番であるもの**：本物の USDC が Base メインネットで動く／Coinbase CDP 本番 facilitator 経由／
誰でも支払える公開 URL／VPS で 24/365 稼働・自動バックアップ・監視・レート制限／
251 テスト・改ざん検出・敵対レビュー済み・公開 money-path を堅牢化済み。

**まだデモ／パイロットであるもの**：
- 現在の購入者は自分自身のエージェントのみ（第三者の支払いはまだゼロ）。
- **x402 Bazaar への掲載は CDP のクローラ待ち**（我々側の要件＝`validate` は accepted 済み、
  自律通知が監視中）。
- 公式には**監査前（H1）の自己資金パイロット**。大規模に他者資金を扱う「事業」段階ではない。

**本物のお金を大規模に動かす前のハードゲート**（[ROADMAP](../ROADMAP.md)）：
H1 独立セキュリティ監査 ／ H2 本番堅牢化（Postgres マルチワーカーは実装・実証済み、
共有監査ストア・KMS が残り）／ H3 法務レビュー。

---

## 5. 使い方（60 秒）

**払う側（エージェント）**：
```bash
pip install 'mandatehub[evm]'
export MANDATEHUB_AGENT_PRIVATE_KEY=0x...            # Base に USDC を持つ鍵
python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote
# → 200 + データ + オンチェーン決済 tx + chainVerification + ProofOfMandate
```

**売る側（自分の API を x402 で課金）**：
```bash
export MANDATEHUB_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
export MANDATEHUB_NETWORK=base MANDATEHUB_PAY_TO=0xあなたのウォレット
export MANDATEHUB_CDP_KEY_FILE=~/.mandatehub-cdp.json
python deploy/local/operator.py                     # マンデートゲート付き x402 エンドポイント
```

---

## 6. 関連ドキュメント

- [ROADMAP](../ROADMAP.md) — 公開・プロトコル・ハードゲート
- [OPERATIONS](OPERATIONS.md) — 運用規律（チャーター・money-path 不変条件）
- [OPERATING_AT_SCALE](OPERATING_AT_SCALE.md) — 価格・収益・採用・スケール
- [ASSET_SURVEY](ASSET_SURVEY.md) — 過去資産の商品化判定（出荷・キュー・除外）
- [LAUNCH](LAUNCH.md) — 告知・マーケティング素材（正直な数字ガードレール付き）
- [THREAT_MODEL](THREAT_MODEL.md) — H1 準備（防御主張とコード＋テストの対応）
- [MULTIWORKER](MULTIWORKER.md) — H2 マルチワーカー（Postgres 共有台帳）
- [TESTNET](TESTNET.md) / [BAZAAR](BAZAAR.md) — testnet 検証・Bazaar 掲載
- 仕様: [`specs/mandate.md`](../specs/mandate.md) ・ [`specs/best-exec.md`](../specs/best-exec.md)
- 運用手順: [`deploy/local/RUNBOOK.md`](../deploy/local/RUNBOOK.md)
