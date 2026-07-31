# PGroonga + MeCab 日本語全文検索環境

PostgreSQL 17 + PGroonga + MeCabによる高精度な日本語全文検索環境をDockerで構築します。

## 特徴

- ✅ **高精度な日本語検索**: MeCab形態素解析による名詞・動詞・形容詞の正確な認識
- ✅ **大規模データ対応**: 1000万件のデータで実証済み
- ✅ **高速ページネーション**: シーク法による一定の応答時間（30〜50ms）
- ✅ **多言語対応**: 日本語・英語・中国語など複数言語の検索をサポート
- ✅ **完全再現可能**: Docker Composeで環境構築が完結

## クイックスタート

```bash
# リポジトリをクローン
git clone https://github.com/okamyuji/pgroonga.git
cd pgroonga

# コンテナを起動（初回は10-15分かかります）
docker compose up -d

# PostgreSQLに接続
docker exec -it pgroonga-postgres psql -U postgres

# 検索例
SELECT title FROM blog_posts WHERE title &@~ '開発' LIMIT 10;
```

## 構成

```shell
pgroonga/
├── Dockerfile              # マルチステージビルド（Alpine base）
├── compose.yaml            # Docker Compose設定
├── alpine/
│   └── build.sh           # MeCab, Groonga, PGroongaのビルドスクリプト
├── init/
│   ├── 01-pgroonga-setup.sql  # 初期テーブル作成
│   └── 02-sample-data.sql     # 1000万件のサンプルデータ生成
├── search_api.py          # FastAPI実装例
└── search_api.js          # Express実装例
```

## バージョン

- PostgreSQL: 17
- PGroonga: 4.0.4
- Groonga: 15.1.7
- MeCab: 0.996.12 (IPA辞書)

## データ量

初期セットアップで自動的に以下のデータが投入されます:

- **blog_posts**: 10,000,000件（1000万件）
- 検索インデックス: PGroonga + MeCab
- ページネーション用インデックス: B-tree複合インデックス

## API実装例

### Python (FastAPI)

```bash
pip install fastapi uvicorn asyncpg pydantic
uvicorn search_api:app --reload
```

**エンドポイント:**

- `GET /search` - 基本検索
- `GET /search/paginated` - シーク法ページネーション
- `GET /search/by-tags` - タグ検索
- `GET /suggest` - オートコンプリート

### Node.js (Express)

```bash
npm install express pg
node search_api.js
```

同様のエンドポイントを提供します。

DB接続情報は環境変数 `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` で上書きできます（未指定時は compose.yaml のlocalhost検証用の値にフォールバック）。

```bash
PGPASSWORD=your-password node search_api.js
```

## パフォーマンス

- **1000万件データでの実測値:**

| ページ | OFFSET法 | シーク法 | 改善率 |
|--------|---------|---------|--------|
| 1ページ目 | 42ms | 42ms | - |
| 11ページ目 | 42ms | 39ms | 7% |
| 101ページ目 | **2.2秒** | 47ms | **98%** |
| 1001ページ目 | **2.4秒** | 47ms | **98%** |

- **推奨: OFFSET 1000以上ではシーク法必須**

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

-- 2ページ目以降
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
-- 日本語検索
SELECT title, content 
FROM blog_posts 
WHERE lang = 'ja' AND content &@~ '全文検索';

-- 英語検索
SELECT title, content 
FROM blog_posts 
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
# 論理バックアップ
docker exec pgroonga-postgres pg_dump -U postgres -Fc postgres > backup.dump

# リストア
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

## クラウドデプロイ

### 対応サービス

- ✅ Supabase (カスタム拡張対応)
- ✅ Render (Dockerデプロイ)
- ✅ Fly.io (カスタムコンテナ)
- ✅ Railway (Dockerサポート)
- ❌ AWS RDS (非対応)
- ❌ GCP Cloud SQL (非対応)
- ❌ Azure Database (非対応)

**マネージドPostgreSQLではPGroongaが使えません。コンテナベースのデプロイが必須です。**

## ライセンス

このプロジェクトで使用しているソフトウェアのライセンス

- PostgreSQL: PostgreSQL License
- PGroonga: Apache License 2.0
- Groonga: LGPL 2.1
- MeCab: BSD-3-Clause

## 参考資料

- [PGroonga公式ドキュメント](https://pgroonga.github.io/ja/)
- [Groonga公式サイト](https://groonga.org/ja/)
- [MeCab公式サイト](https://taku910.github.io/mecab/)
- [Use The Index, Luke - ページネーション](https://use-the-index-luke.com/ja/sql/partial-results/fetch-next-page)

## サポート

Issues and Pull Requestsを歓迎します。
