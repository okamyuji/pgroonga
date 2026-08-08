#!/usr/bin/env python3
"""PGroonga検索の前にキャッシュ層を置いたとき、TTLが切れた瞬間に何が起きるかを計測する。

アプリが複数インスタンスある前提で、同じ検索語のキャッシュが失効した直後に
一斉アクセスが来る状況を再現する。5つの方式を同じ並列度で走らせる。

- nocache:    キャッシュを使わず毎回PostgreSQLへ問い合わせる基準線
- naive:      TTLキャッシュのみ。失効直後は全リクエストがDBへ流れる
- local-lock: プロセス内の排他のみ。インスタンス数だけDBクエリが並ぶ
- dist-lock:  Valkey上の分散ロックで再構築を1本に絞る
- swr:        論理期限切れの間は古い結果を返し、裏で1本だけ再構築する

比較の公平さのため、nocacheも他と同じ並列度で測る。直列のnocacheと並列のnaiveを
比べると「キャッシュを入れると遅くなる」という誤った読み方になるため。
"""

import json
import multiprocessing as mp
import os
import statistics
import sys
import threading
import time
import uuid

import psycopg
import redis

DSN = os.environ.get(
    "BENCH_DSN", "postgresql://postgres:password@pgroonga:5432/postgres"
)
CACHE_URL = os.environ.get("BENCH_CACHE_URL", "redis://valkey:6379/0")

PROCESSES = int(os.environ.get("BENCH_PROCESSES", "4"))
THREADS = int(os.environ.get("BENCH_THREADS", "4"))
REQUESTS_PER_THREAD = int(os.environ.get("BENCH_REQUESTS_PER_THREAD", "3"))
PAGE_SIZE = int(os.environ.get("BENCH_PAGE_SIZE", "20"))
HOT_KEYWORD = os.environ.get("BENCH_HOT_KEYWORD", "全文検索")

# 事前に入れたキャッシュがバースト開始の直前に切れるよう、短い論理期限で仕込む
PRIME_TTL = int(os.environ.get("BENCH_PRIME_TTL", "2"))
CACHE_TTL = int(os.environ.get("BENCH_CACHE_TTL", "60"))
# ロック保持者が落ちても待機側が固まらないよう、再構築時間より十分長く、
# かつ無限には待たない値にする
LOCK_TTL_MS = int(os.environ.get("BENCH_LOCK_TTL_MS", "30000"))
LOCK_WAIT_MS = int(os.environ.get("BENCH_LOCK_WAIT_MS", "20000"))
# 論理期限が切れても古い結果を返せるよう、物理TTLは論理期限より長く持たせる
STALE_TTL = int(os.environ.get("BENCH_STALE_TTL", "600"))

SEARCH_SQL = """
SELECT id
FROM blog_posts
WHERE content &@~ %s
ORDER BY view_count DESC, id DESC
LIMIT %s
"""
COUNT_SQL = "SELECT count(*) FROM blog_posts WHERE content &@~ %s"
HYDRATE_SQL = "SELECT id, title, view_count FROM blog_posts WHERE id = ANY(%s)"

DB_QUERY_COUNTER = "bench:db_queries"
RESULT_KEY = "search:v1:" + HOT_KEYWORD
LOCK_KEY = "lock:" + RESULT_KEY

# 同一プロセス内の重複取得をまとめるための排他。local-lockでのみ使う
_local_locks: dict[str, threading.Lock] = {}
_local_locks_guard = threading.Lock()

# swrで起動した背景の再構築。プロセス終了前に完了を待つために保持する
_refresh_threads: list[threading.Thread] = []


def local_lock_for(key: str) -> threading.Lock:
    with _local_locks_guard:
        return _local_locks.setdefault(key, threading.Lock())


class CacheUnavailable(Exception):
    """キャッシュに到達できない状態。検索そのものはDBだけで完結できる。"""


def cache_get(cache, key: str):
    try:
        return cache.get(key)
    except redis.RedisError as exc:
        raise CacheUnavailable(str(exc)) from exc


def search_db(conn, cache) -> dict:
    """PostgreSQLで検索を実行し、IDの並びと総件数を返す。"""
    try:
        cache.incr(DB_QUERY_COUNTER)
    except redis.RedisError:
        # 計測用の集計はキャッシュ障害時に落としてよい。検索の成否とは無関係
        pass
    with conn.cursor() as cur:
        cur.execute(SEARCH_SQL, (HOT_KEYWORD, PAGE_SIZE))
        ids = [row[0] for row in cur.fetchall()]
        cur.execute(COUNT_SQL, (HOT_KEYWORD,))
        total = cur.fetchone()[0]
    return {"ids": ids, "total": total}


def hydrate(conn, ids: list[int]) -> None:
    """画面に出す行は主キーで引き直す。キャッシュにはIDと総件数しか置かないため。"""
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(HYDRATE_SQL, (ids,))
        cur.fetchall()


def store(cache, payload: dict, ttl: int, logical_ttl: int | None = None) -> None:
    body = dict(payload)
    if logical_ttl is not None:
        body["expires_at"] = time.time() + logical_ttl
    try:
        cache.setex(RESULT_KEY, ttl, json.dumps(body))
    except redis.RedisError:
        # 書き込めなくても応答は返せる。次のリクエストがまたDBを引くだけ
        pass


def op_nocache(conn, cache) -> str:
    payload = search_db(conn, cache)
    hydrate(conn, payload["ids"])
    return "db"


def op_naive(conn, cache) -> str:
    cached = cache_get(cache, RESULT_KEY)
    if cached is not None:
        hydrate(conn, json.loads(cached)["ids"])
        return "hit"
    payload = search_db(conn, cache)
    store(cache, payload, CACHE_TTL)
    hydrate(conn, payload["ids"])
    return "db"


def op_local_lock(conn, cache) -> str:
    cached = cache_get(cache, RESULT_KEY)
    if cached is not None:
        hydrate(conn, json.loads(cached)["ids"])
        return "hit"
    with local_lock_for(RESULT_KEY):
        # 待っている間に同じプロセスの別スレッドが書き込んでいることがある
        cached = cache_get(cache, RESULT_KEY)
        if cached is not None:
            hydrate(conn, json.loads(cached)["ids"])
            return "hit"
        payload = search_db(conn, cache)
        store(cache, payload, CACHE_TTL)
    hydrate(conn, payload["ids"])
    return "db"


def acquire(cache, token: str) -> bool:
    try:
        return bool(cache.set(LOCK_KEY, token, nx=True, px=LOCK_TTL_MS))
    except redis.RedisError as exc:
        raise CacheUnavailable(str(exc)) from exc


def release(cache, token: str) -> None:
    # 自分が取ったロックだけを消す。期限切れ後に他者が取り直したロックを
    # 消してしまうと、排他が成立しなくなるため
    script = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        cache.eval(script, 1, LOCK_KEY, token)
    except redis.RedisError:
        pass


def rebuild_under_lock(conn, cache, logical_ttl: int | None, physical_ttl: int) -> dict:
    payload = search_db(conn, cache)
    store(cache, payload, physical_ttl, logical_ttl)
    return payload


def op_dist_lock(conn, cache) -> str:
    cached = cache_get(cache, RESULT_KEY)
    if cached is not None:
        hydrate(conn, json.loads(cached)["ids"])
        return "hit"

    token = uuid.uuid4().hex
    if acquire(cache, token):
        try:
            payload = rebuild_under_lock(conn, cache, None, CACHE_TTL)
        finally:
            release(cache, token)
        hydrate(conn, payload["ids"])
        return "db"

    deadline = time.time() + LOCK_WAIT_MS / 1000.0
    while time.time() < deadline:
        time.sleep(0.05)
        cached = cache_get(cache, RESULT_KEY)
        if cached is not None:
            hydrate(conn, json.loads(cached)["ids"])
            return "wait-hit"
    # ロック保持者が落ちた場合に待ち続けないための逃げ道
    payload = search_db(conn, cache)
    hydrate(conn, payload["ids"])
    return "fallback-db"


def op_swr(conn, cache) -> str:
    cached = cache_get(cache, RESULT_KEY)
    if cached is None:
        token = uuid.uuid4().hex
        if acquire(cache, token):
            try:
                payload = rebuild_under_lock(conn, cache, CACHE_TTL, STALE_TTL)
            finally:
                release(cache, token)
            hydrate(conn, payload["ids"])
            return "db"
        deadline = time.time() + LOCK_WAIT_MS / 1000.0
        while time.time() < deadline:
            time.sleep(0.05)
            cached = cache_get(cache, RESULT_KEY)
            if cached is not None:
                break
        if cached is None:
            payload = search_db(conn, cache)
            hydrate(conn, payload["ids"])
            return "fallback-db"

    payload = json.loads(cached)
    fresh = payload.get("expires_at", 0) > time.time()
    hydrate(conn, payload["ids"])
    if fresh:
        return "hit"

    # 論理期限は切れているので、古い結果を返したうえで裏側で1本だけ作り直す。
    # daemon=Trueにするとプロセス終了時に再構築が途中で殺され、DBクエリ数が
    # 実態より少なく見えてしまうので、計測側で最後まで待てるよう保持する
    token = uuid.uuid4().hex
    if acquire(cache, token):
        thread = threading.Thread(target=background_refresh, args=(token,))
        thread.start()
        _refresh_threads.append(thread)
    return "stale-hit"


def background_refresh(token: str) -> None:
    try:
        with psycopg.connect(DSN) as conn:
            cache = redis.Redis.from_url(CACHE_URL)
            rebuild_under_lock(conn, cache, CACHE_TTL, STALE_TTL)
            release(cache, token)
    except (psycopg.Error, redis.RedisError, OSError) as exc:
        # 再構築に失敗しても古い結果の提供は続ける。ここで例外を伝播させると
        # stale応答まで巻き添えで止まる
        print(f"[swr] 背景での再構築に失敗: {exc}", file=sys.stderr, flush=True)


OPERATIONS = {
    "nocache": op_nocache,
    "naive": op_naive,
    "local-lock": op_local_lock,
    "dist-lock": op_dist_lock,
    "swr": op_swr,
}


def worker_process(mode: str, out_queue: mp.Queue) -> None:
    """1プロセスがアプリの1インスタンスに対応する。"""
    records: list[tuple[float, str]] = []
    lock = threading.Lock()

    def run_thread() -> None:
        with psycopg.connect(DSN) as conn:
            cache = redis.Redis.from_url(CACHE_URL)
            op = OPERATIONS[mode]
            for _ in range(REQUESTS_PER_THREAD):
                started = time.perf_counter()
                try:
                    kind = op(conn, cache)
                except CacheUnavailable:
                    # キャッシュに到達できないだけなら検索は成立する。
                    # ここで失敗を返すと、速くするために足したはずのValkeyが
                    # 単一障害点になってしまう
                    payload = search_db(conn, cache)
                    hydrate(conn, payload["ids"])
                    kind = "degraded-db"
                except (psycopg.Error, OSError) as exc:
                    kind = f"error:{type(exc).__name__}"
                elapsed = (time.perf_counter() - started) * 1000.0
                with lock:
                    records.append((elapsed, kind))

    threads = [threading.Thread(target=run_thread) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 応答時間の記録は先に確定させる。背景の再構築は利用者を待たせないので
    # 応答時間には含めないが、DBへの負荷としては数える必要がある
    out_queue.put(records)
    for t in _refresh_threads:
        t.join(timeout=120)


class DbActivityMonitor(threading.Thread):
    """PostgreSQL側で同時に走っている検索の本数を実測する。

    クライアント側の集計だけだと、待機に回ったリクエストとDBに届いたクエリの
    区別がつかないため、DBから直接見る。
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.max_active = 0
        self._stop = threading.Event()

    def run(self) -> None:
        sql = (
            "SELECT count(*) FROM pg_stat_activity "
            # 全文検索の演算子を含むクエリだけ数える。主キーでの引き直しは
            # 一瞬で終わるため、混ぜるとDBの詰まり具合が見えなくなる
            "WHERE state = 'active' AND query LIKE '%&@~%' "
            "AND pid <> pg_backend_pid()"
        )
        with psycopg.connect(DSN) as conn:
            conn.autocommit = True
            while not self._stop.is_set():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    self.max_active = max(self.max_active, cur.fetchone()[0])
                time.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def prime_cache(mode: str, cache: redis.Redis) -> None:
    """バースト開始の直前に期限が切れるようキャッシュを仕込む。

    naiveとlocal-lockとdist-lockは物理的に消えた状態から、swrは論理期限だけ
    切れて古い結果が残った状態から始まる。これがTTL切れの瞬間そのものになる。
    """
    try:
        cache.flushdb()
    except redis.RedisError as exc:
        # キャッシュが落ちている状態での挙動を測る場合、準備段階も当然失敗する
        print(
            f"[{mode}] キャッシュに到達できません: {exc}", file=sys.stderr, flush=True
        )
        return
    if mode == "nocache":
        return
    with psycopg.connect(DSN) as conn:
        payload = search_db(conn, cache)
    if mode == "swr":
        store(cache, payload, STALE_TTL, PRIME_TTL)
    else:
        store(cache, payload, PRIME_TTL)
    cache.set(DB_QUERY_COUNTER, 0)
    time.sleep(PRIME_TTL + 1)


def run_mode(mode: str) -> dict:
    cache = redis.Redis.from_url(CACHE_URL)
    print(f"[{mode}] キャッシュ準備", file=sys.stderr, flush=True)
    prime_cache(mode, cache)

    monitor = DbActivityMonitor()
    monitor.start()
    queue: mp.Queue = mp.Queue()
    started = time.perf_counter()
    procs = [
        mp.Process(target=worker_process, args=(mode, queue)) for _ in range(PROCESSES)
    ]
    for p in procs:
        p.start()
    records: list[tuple[float, str]] = []
    for _ in procs:
        records.extend(queue.get())
    for p in procs:
        p.join()
    wall = time.perf_counter() - started
    monitor.stop()
    time.sleep(0.3)

    latencies = [r[0] for r in records]
    kinds: dict[str, int] = {}
    for _, kind in records:
        kinds[kind] = kinds.get(kind, 0) + 1
    try:
        db_queries = int(cache.get(DB_QUERY_COUNTER) or 0)
    except redis.RedisError:
        db_queries = -1  # キャッシュが落ちている試験では集計できない

    result = {
        "mode": mode,
        "requests": len(latencies),
        "wall_seconds": round(wall, 2),
        "db_queries": db_queries,
        "max_db_concurrency": monitor.max_active,
        "p50": round(percentile(latencies, 0.50), 1),
        "p95": round(percentile(latencies, 0.95), 1),
        "p99": round(percentile(latencies, 0.99), 1),
        "max": round(max(latencies), 1) if latencies else 0.0,
        "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "kinds": kinds,
    }
    print(f"[{mode}] 完了 {result}", file=sys.stderr, flush=True)
    return result


def main() -> int:
    concurrency = PROCESSES * THREADS
    print(
        f"インスタンス{PROCESSES} × スレッド{THREADS} = 同時{concurrency}, "
        f"1スレッドあたり{REQUESTS_PER_THREAD}回, 検索語「{HOT_KEYWORD}」",
        file=sys.stderr,
        flush=True,
    )
    selected = os.environ.get("BENCH_MODES", "")
    modes = [m for m in selected.split(",") if m in OPERATIONS] or list(OPERATIONS)
    results = [run_mode(mode) for mode in modes]

    header = (
        f"{'方式':<11}{'DB実行':>7}{'DB同時':>7}"
        f"{'p50ms':>10}{'p95ms':>10}{'p99ms':>10}{'最大ms':>10}{'所要秒':>8}"
    )
    print(header)
    for r in results:
        print(
            f"{r['mode']:<11}{r['db_queries']:>7}{r['max_db_concurrency']:>7}"
            f"{r['p50']:>10}{r['p95']:>10}{r['p99']:>10}"
            f"{r['max']:>10}{r['wall_seconds']:>8}"
        )
    print()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
