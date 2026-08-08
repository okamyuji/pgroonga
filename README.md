# PGroonga + MeCab 日本語全文検索環境

PostgreSQL 17とPGroongaとMeCabによる日本語全文検索環境をDockerで構築します。検索結果のキャッシュ層としてValkeyを同梱し、キャッシュの有無による応答時間の差を計測できます。

## 特徴

- MeCabの形態素解析により、名詞・動詞・形容詞を単語単位で索引します
- 1000万件のサンプルデータで動作を確認できます
- シーク法のページネーションにより、深いページでも応答時間が一定に保たれます
- 日本語・英語・中国語をトークナイザーの使い分けで検索できます
- 検索結果をValkeyにキャッシュする構成と、その効果を測る計測スクリプトを同梱しています

## クイックスタート

```bash
git clone https://github.com/okamyuji/pgroonga.git
cd pgroonga

# 初回はMeCabとGroongaをソースからビルドするため時間がかかります
docker compose up -d

# PostgreSQLに接続
docker exec -it pgroonga-postgres psql -U postgres
```

```sql
SELECT title FROM blog_posts WHERE title &@~ '開発' LIMIT 10;
```

`docker compose up`はblog_postsに1000万件を投入します。ディスクと時間を消費するため、件数を減らしたい場合は`init/02-sample-data.sql`の`generate_series`の上限を変更してください。

## 構成

```shell
pgroonga/
├── Dockerfile              # マルチステージビルド（Alpineベース）
├── compose.yaml            # PostgreSQLとValkeyとベンチマークの定義
├── alpine/
│   └── build.sh            # MeCab, Groonga, PGroongaのビルドスクリプト
├── init/
│   ├── 01-pgroonga-setup.sql  # テーブルとインデックスの作成
│   └── 02-sample-data.sql     # 1000万件のサンプルデータ生成
├── bench/
│   ├── bench.py            # キャッシュ有無の応答時間を比較する計測スクリプト
│   ├── cjk_check.sh        # Valkey/Redisの日本語検索の挙動を確認する
│   └── Dockerfile          # 計測スクリプトの実行環境
├── search_api.py           # FastAPI実装例
└── search_api.js           # Express実装例
```

## バージョン

- PostgreSQL: 17（Alpineベースイメージ）
- PGroonga: 4.0.8
- Groonga: 16.0.9
- MeCab: 0.996.12（IPA辞書 2.7.0-20070801）
- Valkey: 9

PGroongaは4.0.6以降Mesonでビルドします。`alpine/build.sh`は`meson setup`に`-Dtest=false`を渡しており、これを外すとテスト用のRuby gemを要求されて設定段階で停止します。

clangとllvmのメジャー版は`postgresql17-dev`の依存に合わせる必要があります。ベースイメージのAlpineを上げるときは`apk info -R postgresql17-dev`で確認し、Dockerfileの`ARG LLVM_VERSION`を合わせてください。

## データ量

初期セットアップで次のデータが投入されます。

- blog_posts: 10,000,000件
- 全文検索インデックス: PGroonga（TokenMecab、NormalizerNFKC150）
- ページネーション用インデックス: B-tree複合インデックス

## キャッシュ層

`compose.yaml`のvalkeyサービスは永続化を切り、上限に達したら参照の古いキーから捨てる設定にしています。キャッシュとしてのみ使い、失われても検索結果はPostgreSQLから作り直せるためです。

```yaml
--save ""
--appendonly no
--maxmemory 256mb
--maxmemory-policy allkeys-lru
```

### 計測の実行

```bash
docker compose --profile bench run --rm bench
```

同一の検索語の列を3つの方式で流し、応答時間の分布とPostgreSQLへの問い合わせ回数を出力します。

- nocache: キャッシュを使わず毎回PostgreSQLへ問い合わせる基準線
- naive: TTLキャッシュのみ。失効直後は全リクエストがDBへ流れます
- local-lock: プロセス内の排他のみ。アプリのインスタンス数だけDBクエリが並びます
- dist-lock: Valkey上の分散ロックで再構築を1本に絞ります
- swr: 論理期限が切れている間は古い結果を返し、裏で1本だけ再構築します

事前にキャッシュを入れてから期限を切らせ、その直後に同一の検索語へ一斉アクセスさせます。キャッシュが空の状態から始めるのではなく、あったものが消えた瞬間を再現するためです。基準線のnocacheも他と同じ並列度で測ります。直列のnocacheと並列のnaiveを比べると、キャッシュを入れると遅くなるという誤った読み方になります。

Valkeyに到達できない場合は、リクエストを失敗させずPostgreSQLへ直行します。速くするために足したキャッシュが単一障害点になっては本末転倒だからです。動作を確かめるにはValkeyを停止した状態で実行します。

```bash
docker stop pgroonga-valkey
docker compose --profile bench run --rm bench
```

環境変数で条件を変えられます。

| 変数 | 既定値 | 意味 |
|---|---|---|
| `BENCH_PROCESSES` | 4 | アプリのインスタンス数に相当するプロセス数 |
| `BENCH_THREADS` | 4 | 1インスタンスあたりの同時リクエスト数 |
| `BENCH_REQUESTS_PER_THREAD` | 3 | 1スレッドあたりの実行回数 |
| `BENCH_MODES` | 全方式 | 実行する方式をカンマ区切りで指定します |
| `BENCH_HOT_KEYWORD` | 全文検索 | 一斉アクセスを集中させる検索語 |
| `BENCH_CACHE_TTL` | 60 | キャッシュの有効期間（秒） |
| `BENCH_LOCK_TTL_MS` | 30000 | 分散ロックの保持期限（ミリ秒） |
| `BENCH_LOCK_WAIT_MS` | 35000 | 再構築を待つ上限（ミリ秒、保持期限より長くする） |
| `BENCH_STALE_TTL` | 600 | 古い結果を保持する物理TTL（秒） |
| `BENCH_BURSTS` | 3 | 失効を起こす回数 |
| `BENCH_SLOW_THRESHOLD_MS` | 1000 | 遅いと見なす境目（ミリ秒） |

### 実測値

Apple M3 Pro、Dockerに8 CPUと12GBを割り当てた環境での結果です。検索語「全文検索」は約50万件にヒットし、単発の検索は約0.35秒かかります。遅いと見なす境目は`BENCH_SLOW_THRESHOLD_MS=100`で、キャッシュに当たったときの1ミリ秒前後と比べて体感できる水準に置いています。

| 方式 | DB検索 | DBの同時実行 | p50 | 100ms超 | 最遅 |
|---|---|---|---|---|---|
| nocache | 144 | 23 | 980ms | 144 / 144 | 1684ms |
| naive | 48 | 23 | 0.7ms | 48 / 144 | 968ms |
| local-lock | 12 | 11 | 0.6ms | 48 / 144 | 637ms |
| dist-lock | 3 | 3 | 0.9ms | 48 / 144 | 523ms |
| swr | 3 | 3 | 1.4ms | 0 / 144 | 96ms |

DBへの負荷は48本から3本まで下がる一方、遅いリクエストの件数はnaiveとlocal-lockとdist-lockで同じです。待たされる相手がDBの混雑からロックの解放へ変わるだけで、待つ人数は変わりません。実行ごとに1割から2割は振れます。

「DB検索」は検索を作り直した回数です。1回につき全文検索と総件数の2文が実行されるので、SQLの実行回数そのものではありません。「DBの同時実行」は`pg_stat_activity`で数えたバックエンドの本数です。`max_parallel_workers_per_gather`が2なので、1本の検索が最大3バックエンドを使います。

パーセンタイルは出力しません。1バーストは48件しかなく、そこでの99パーセンタイルは最も遅い1件と同義になるためです。失効時の尾は、遅い側が何件出たかと各バーストの最遅値で見ます。

### ページネーションの実測値

同じ環境で`title &@~ '開発'`を1ページ10件で取得した結果です。

| ページ | OFFSET法 | シーク法 |
|---|---|---|
| 1ページ目 | 72ms | 63ms |
| 11ページ目 | 126ms | 56ms |
| 101ページ目 | 4810ms | 48ms |
| 1001ページ目 | 1884ms | 48ms |

OFFSET法は101ページ目で実行計画が変わり、捨てる行を数えるために全体を並べ直します。シーク法は深さによらず一定です。

### Valkey/Redisの日本語検索の確認

```bash
./bench/cjk_check.sh
```

ValkeyとRedisのコンテナを起動し、同じ日本語文書に対して同じ検索語を投げた結果を並べて出します。どちらも日本語の形態素解析器を持たないため、全文検索そのものを移す用途には使えないことを確認できます。

## API実装例

### Python (FastAPI)

```bash
pip install fastapi uvicorn asyncpg pydantic
uvicorn search_api:app --reload
```

エンドポイントは次のとおりです。

- `GET /search` 基本検索
- `GET /search/paginated` シーク法ページネーション
- `GET /search/by-tags` タグ検索
- `GET /suggest` オートコンプリート

### Node.js (Express)

```bash
npm install express pg express-rate-limit
node search_api.js
```

同じエンドポイントを提供します。

DB接続情報は環境変数`PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE`で上書きできます。未指定のときはcompose.yamlの検証用の値を使います。

```bash
PGPASSWORD=your-password node search_api.js
```

## 主要なSQLクエリ

### 基本検索

```sql
SELECT title FROM blog_posts
WHERE title &@~ '開発'
ORDER BY published_at DESC
LIMIT 10;
```

### シーク法ページネーション

```sql
-- 1ページ目
SELECT title, published_at, id
FROM blog_posts
WHERE title &@~ '開発'
ORDER BY published_at DESC, id DESC
LIMIT 10;

-- 2ページ目以降は直前の行の値を境界にする
SELECT title, published_at, id
FROM blog_posts
WHERE title &@~ '開発'
  AND (published_at, id) < ('2025-10-12 13:27:05', 9999026)
ORDER BY published_at DESC, id DESC
LIMIT 10;
```

### 複合条件検索

```sql
SELECT title, tags, view_count
FROM blog_posts
WHERE 'PostgreSQL' = ANY(tags)
  AND (title &@~ 'パフォーマンス' OR content &@~ 'パフォーマンス')
ORDER BY view_count DESC;
```

### 多言語検索

```sql
SELECT title, content FROM blog_posts
WHERE lang = 'ja' AND content &@~ '全文検索';

SELECT title, content FROM blog_posts
WHERE lang = 'en' AND content &@~ 'search';
```

## メンテナンス

### VACUUM実行

```sql
VACUUM ANALYZE blog_posts;
```

### インデックスサイズ確認

```sql
SELECT
  relname AS tablename,
  indexrelname,
  pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexrelid) DESC;
```

### バックアップ

```bash
docker exec pgroonga-postgres pg_dump -U postgres -Fc postgres > backup.dump
docker exec -i pgroonga-postgres pg_restore -U postgres -d postgres < backup.dump
```

## トラブルシューティング

### インデックスが使用されない

```sql
ANALYZE blog_posts;
```

### MeCab動作確認

```bash
docker exec pgroonga-postgres mecab --version
docker exec pgroonga-postgres sh -c "echo '日本語の形態素解析' | mecab"
```

### コンテナログ確認

```bash
docker logs pgroonga-postgres
```

### apk addでclangやllvmが見つからない

ベースイメージのAlpineが上がり、Dockerfileの`ARG LLVM_VERSION`が指すメジャー版が配布から外れた状態です。`apk info -R postgresql17-dev`が依存している版に合わせてください。

### Groongaのビルドがxsimd関連のエラーで止まる

Alpineのxsimdとgroongaの対応版がずれています。`GROONGA_VERSION`を新しい版へ上げてください。

### PGroongaのビルドがmakefileなしで止まる

`No targets specified and no makefile found`が出る場合、PGXSのMakefileが廃止された4.0.6以降のtarballをmakeで処理しようとしています。`alpine/build.sh`のMeson経路を使ってください。

## クラウドデプロイ

マネージドのPostgreSQLではPGroongaを導入できないため、コンテナベースのデプロイが必要です。

| サービス | 可否 |
|---|---|
| Supabase | カスタム拡張に対応しているため利用できます |
| Render | Dockerデプロイで利用できます |
| Fly.io | カスタムコンテナで利用できます |
| Railway | Dockerのサポートがあるため利用できます |
| AWS RDS | 利用できません |
| GCP Cloud SQL | 利用できません |
| Azure Database | 利用できません |

## ライセンス

このプロジェクトで使用しているソフトウェアのライセンスは次のとおりです。

- PostgreSQL: PostgreSQL License
- PGroonga: Apache License 2.0
- Groonga: LGPL 2.1
- MeCab: BSD-3-Clause
- Valkey: BSD-3-Clause

## 参考資料

- [PGroonga公式ドキュメント](https://pgroonga.github.io/ja/)
- [Groonga公式サイト](https://groonga.org/ja/)
- [MeCab公式サイト](https://taku910.github.io/mecab/)
- [Valkey公式ドキュメント](https://valkey.io/topics/)
- [Use The Index, Luke - ページネーション](https://use-the-index-luke.com/ja/sql/partial-results/fetch-next-page)

## サポート

IssuesとPull Requestsを歓迎します。
