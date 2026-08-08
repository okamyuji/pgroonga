#!/usr/bin/env bash
# Valkey/Redisの全文検索が日本語をどう扱うかを実際に動かして確かめる。
# PGroongaと違ってどちらも形態素解析器を持たないため、同じ文書と同じ検索語で
# 何がヒットして何がヒットしないかを並べて出す。
set -euo pipefail

VALKEY_PORT="${VALKEY_PORT:-16379}"
REDIS_PORT="${REDIS_PORT:-16380}"
VALKEY_NAME="${VALKEY_NAME:-cjk-valkey}"
REDIS_NAME="${REDIS_NAME:-cjk-redis}"

cleanup() {
  docker rm -f "$VALKEY_NAME" "$REDIS_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
docker run -d --name "$VALKEY_NAME" -p "${VALKEY_PORT}:6379" valkey/valkey-bundle:latest >/dev/null
docker run -d --name "$REDIS_NAME" -p "${REDIS_PORT}:6379" redis:8 >/dev/null

# モジュールのロードとインデックス生成が終わるまで待つ
for _ in $(seq 1 30); do
  if docker exec "$VALKEY_NAME" valkey-cli PING >/dev/null 2>&1 &&
    docker exec "$REDIS_NAME" redis-cli PING >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

DOCS=(
  "きのう渋谷でラーメンを食べました"
  "コンピュータの性能を計測する"
  "東京都の人口は増えています"
  "世田谷の食堂"
)
QUERIES=(渋谷 ラーメン 東京 東京都 人口 性能 コンピュータ)

echo "=== 投入する文書 ==="
for i in "${!DOCS[@]}"; do echo "  doc:$((i + 1))  ${DOCS[$i]}"; done
echo

docker exec "$VALKEY_NAME" valkey-cli FT.CREATE jaidx ON HASH PREFIX 1 doc: \
  SCHEMA content TEXT >/dev/null
docker exec "$REDIS_NAME" redis-cli FT.CREATE jaidx ON HASH PREFIX 1 doc: \
  SCHEMA content TEXT >/dev/null
# Redisは中国語向けの分割器を持つので、日本語に適用した場合も併せて見る
docker exec "$REDIS_NAME" redis-cli FT.CREATE zhidx ON HASH PREFIX 1 zh: \
  LANGUAGE CHINESE SCHEMA content TEXT >/dev/null

for i in "${!DOCS[@]}"; do
  n=$((i + 1))
  docker exec "$VALKEY_NAME" valkey-cli HSET "doc:$n" content "${DOCS[$i]}" >/dev/null
  docker exec "$REDIS_NAME" redis-cli HSET "doc:$n" content "${DOCS[$i]}" >/dev/null
  docker exec "$REDIS_NAME" redis-cli HSET "zh:$n" content "${DOCS[$i]}" >/dev/null
done
sleep 3

hits() { docker exec "$1" "$2" FT.SEARCH "$3" "$4" NOCONTENT | head -1; }

printf '%-14s %-14s %-16s %s\n' "検索語" "Valkey既定" "Redis既定" "Redis(CHINESE)"
for q in "${QUERIES[@]}"; do
  printf '%-14s %-14s %-16s %s\n' \
    "$q" \
    "$(hits "$VALKEY_NAME" valkey-cli jaidx "$q")件" \
    "$(hits "$REDIS_NAME" redis-cli jaidx "$q")件" \
    "$(hits "$REDIS_NAME" redis-cli zhidx "$q")件"
done

echo
echo "=== Redisの中国語分割器が日本語をどう切るか ==="
for s in "東京都の人口は増えています" "きのう渋谷でラーメンを食べました"; do
  echo "--- $s"
  docker exec "$REDIS_NAME" redis-cli FT.EXPLAINCLI zhidx "$s"
done

echo "=== 検索語「渋谷」でヒットした文書 (Redis CHINESE) ==="
docker exec "$REDIS_NAME" redis-cli FT.SEARCH zhidx "渋谷"
