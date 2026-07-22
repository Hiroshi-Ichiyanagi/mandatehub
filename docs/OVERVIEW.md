# mandatehub — システム概要と商品カタログ

*更新: 2026-07-23（実測値は全て本番 `/metrics`・`/healthz` から取得）*

> **一言でいうと**：自律 AI エージェントが「予算枠（マンデート）の範囲内でのみ・二重支払い不能・証明付き」でお金を払える x402 決済レイヤー。その上で、機械が読める価値あるデータ／検証を **1 コール ≈ $0.01 の実 USDC** で売る、稼働中のサービス。
>
> **ビジネススタイル**：大きなお金を一気に扱わず、少額決済を確実にコツコツ積み上げる。爆発半径が小さいから監査前でも正直に本番運用でき、全決済がオンチェーン証明付きで蓄積されるため、**運用実績そのものが信用資産になる**。

- 📦 ライブラリ: `pip install mandatehub` — <https://pypi.org/project/mandatehub/>
- 🌐 紹介サイト: <https://mandatehub.ichiyanagi1111.workers.dev>
- 🟢 稼働サービス: <https://mandatehub.obolpay.xyz> （Base メインネット・実 USDC・24/365）
- 📚 ソース: <https://github.com/Hiroshi-Ichiyanagi/mandatehub>
- 🧪 無料 Colab（ウォレット不要）: [quickstart ノートブック](https://colab.research.google.com/github/Hiroshi-Ichiyanagi/mandatehub/blob/main/examples/mandatehub_quickstart.ipynb)

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
- **オンチェーン実行なし・HTTP なし・鍵なし（コアは）** — 証明するのは*会計*。
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
| **運用** | 毎時バックアップ（監査チェーン検証つき）／5分毎監視／Bazaar・x402-list 掲載チェック（cron 自律） |
| **堅牢性** | 再起動生存・改ざん検出・レート制限・全経路 fail-closed・SSH 公開鍵限定・脅威モデル文書 |

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
  **課金前に 503**（古い・欠損データに課金しない）／各商品は CDP に個別 resource URL で記録。

| # | エンドポイント | 商品名 | 説明 | 由来資産 |
|---|---|---|---|---|
| 1 | `GET /quote` | **ECB FX 参照レート** | 欧州中央銀行の公式日次為替レート（EUR 基準・約30通貨）を canonical ハッシュ付きで。古い時は課金しない。 | x402-gateway |
| 2 | `GET /product/fx?from=USD&to=JPY&amount=<最小単位>` | **ゼロスプレッド FX 変換＋開示** | 任意の 2 通貨間を ECB レートで変換し、**スプレッド 0bps** を明示開示。整数最小単位・Decimal 演算。 | genesis_finance |
| 3 | `GET /product/qswap?matrix=fidelity\|swap\|both` | **LLM バックエンド選択マトリクス** | Apple Silicon 実測：`llama.cpp`／`mlx`／`candle` の忠実度＋スワップ遅延・メモリ。機械がバックエンドを選ぶためのデータ。 | qswap |
| 4 | `GET /product/audit-verify?data=<base64 json>` | **監査ログ検証** | 提出されたハッシュ連鎖監査ログを署名アンカー（Ed25519／HMAC）＋連鎖整合で検証。「改ざんされていない」を第三者に有料で証明。 | genesis-keystone |
| 5 | `GET /product/verify-tx?tx=0x<64hex>` | **オンチェーン決済検証** | Base 上の USDC 送金 tx を独立検証（receipt＋Transfer ログ解読）。**「検証」そのものを商品化**。 | mandatehub |
| 6 | `GET /product/govern-verify?bundle=genuine\|tampered` or `?data=<base64 zip>` | **govern バンドル検証** | AI 実行の証拠バンドルをオフライン検証（ハッシュ連鎖・Ed25519 receipt・witness・STH）。純 Python。zip 爆弾/zip-slip 対策済み。 | govern-open-verify |
| 7 | `GET /product/openunit` | **openunit 評価** | 人口加重の勘定単位（UN-WPP＋WB-PPP）の決定的評価。**販売のたびにライブ再検証**。1 openunit ≈ 2.848 USD。 | openunit |
| 8 | `GET /product/kairos?top=1..300` | **Kairos 収束スコア（日本株）** | 約2000 銘柄の追い風「収束」度（KCS）。明示的 `as_of` 付き静的スナップショット。**ライブデータではない・投資助言ではない**と明記。 | kairos |

### 運用・観測エンドポイント（無料）

| エンドポイント | 内容 |
|---|---|
| `GET /` | サービス案内（人間にはダッシュボード HTML、機械には JSON） |
| `GET /healthz` | 稼働状態・残予算・監査ルート |
| `GET /metrics` | 決済件数・収益・ユニーク支払者・日別内訳 |
| `GET /quote-v2` | x402 **v2** ディスカバリ用チャレンジ（Bazaar 掲載対応） |

---

## 4. エージェント統合面（Go-to-Market 実装済み）🆕

AI エージェントが**自力で発見して・理解して・支払える**ための面を全て実装済み：

| 経路 | 実体 | 状態 |
|---|---|---|
| **機械ディスカバリ** | `/.well-known/agents.json`（商品カタログ）・`/.well-known/ai-plugin.json`（マニフェスト＋ロゴ）・`/openapi.json`（OpenAPI 3.1、パス毎に `x-402-payment`） | 🟢 本番稼働。カタログから自動生成されるため**実売と乖離しない** |
| **MCP サーバー** | `examples/mcp_server.py` — Claude Desktop / Cursor 等にネイティブツールとして追加（`discover`/`preview` は無料、`purchase` は実 USDC） | ✅ 出荷済み・ライブ検証済み |
| **LangChain / CrewAI** | `examples/agent_tools.py` — 両フレームワークのツールアダプタ（依存は任意） | ✅ 出荷済み（LangChain は実ライブラリで実行検証） |
| **Colab チュートリアル** | `examples/mandatehub_quickstart.ipynb` — 無料・ウォレット不要で発見→402 プレビュー | ✅ 出荷済み・全セル実行検証 |
| **システムプロンプト雛形** | README「Agent integration」節 — エージェントに貼るだけの支払い手順 | ✅ 出荷済み |
| **CLI クライアント** | `examples/x402_pay.py`（`--quote-only` で無料プレビュー） | ✅ 出荷済み |

### 掲載・ディスカバリの現況

| チャネル | 状態 |
|---|---|
| **x402-list.com** | 📨 **提出済み（2026-07-22、審査待ち）**。姉妹サービス Obolpay x402 Gateway は同日**グレード A（11/11）**で掲載済み。掲載されたら cron が ntfy 通知 |
| **CDP x402 Bazaar** | `validate` は accepted 済み、クローラのインデックス待ち（cron 監視中） |
| **告知素材** | `docs/LAUNCH.md` に X/Farcaster・Coinbase 提出文・ハッカソンピッチを用意（正直な数字ガードレール付き、投稿はオーナー実行） |

---

## 5. 何が「本物」で何が「まだ」か（正直な状態・2026-07-23 実測）

**本番であるもの**：本物の USDC が Base メインネットで動く（**14 決済・0.14 USDC・残予算 $98.6**）／
Coinbase CDP 本番 facilitator 経由／誰でも支払える公開 URL／VPS で 24/365 稼働・自動バックアップ・
監視・レート制限／**255 テスト**・改ざん検出・敵対レビュー済み。

**まだこれからのもの**：
- **外部の購入者はまだゼロ**（unique_payees=1 ＝ 自分のエージェントのみ）。ディレクトリ掲載と
  告知が進行中の「1 人目の外部支払者」待ち。
- x402-list.com 審査待ち／Bazaar クローラ待ち。
- 公式には**監査前（H1）の自己資金パイロット**。ただし少額積み上げ型の方針により、
  H1-H3 は「スケール時のゲート」であって現運用のブロッカーではない。

---

## 6. 使い方（60 秒）

**払う側（エージェント）**：
```bash
pip install 'mandatehub[evm]'
python examples/x402_pay.py --quote-only https://mandatehub.obolpay.xyz/quote  # 無料で条件確認
export MANDATEHUB_AGENT_PRIVATE_KEY=0x...            # Base に USDC を持つ鍵
python examples/x402_pay.py https://mandatehub.obolpay.xyz/quote
# → 200 + データ + オンチェーン決済 tx + chainVerification + ProofOfMandate
```

**エージェントに発見させる**：
```bash
curl https://mandatehub.obolpay.xyz/.well-known/agents.json   # カタログ・価格・支払い方法
```

**売る側（自分の API を x402 で課金）**：
```bash
export MANDATEHUB_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
export MANDATEHUB_NETWORK=base MANDATEHUB_PAY_TO=0xあなたのウォレット
export MANDATEHUB_CDP_KEY_FILE=~/.mandatehub-cdp.json
python deploy/local/operator.py                     # マンデートゲート付き x402 エンドポイント
```

---

## 7. 関連ドキュメント

- [ROADMAP](../ROADMAP.md) — 公開・プロトコル・ハードゲート
- [LAUNCH](LAUNCH.md) — 告知素材（正直な数字ガードレール付き）
- [OPERATIONS](OPERATIONS.md) — 運用規律
- [ASSET_SURVEY](ASSET_SURVEY.md) — 全資産の商品化判定
- [THREAT_MODEL](THREAT_MODEL.md) — H1 準備
- [MULTIWORKER](MULTIWORKER.md) — H2 マルチワーカー
