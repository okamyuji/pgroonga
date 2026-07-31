#!/usr/bin/env python3
"""
PGroonga全文検索API実装例（FastAPI）
"""
from fastapi import FastAPI, Query, HTTPException
from typing import List, Optional
from datetime import datetime
import asyncpg
import os
from pydantic import BaseModel

app = FastAPI(title="PGroonga Search API")


class SearchResult(BaseModel):
    id: int
    title: str
    author: str
    view_count: int
    published_at: datetime


class PaginatedSearchResult(BaseModel):
    results: List[SearchResult]
    has_next: bool
    last_id: Optional[int] = None
    last_published_at: Optional[datetime] = None


async def get_db_connection():
    """データベース接続を取得

    フォールバック値はcompose.yamlのlocalhost検証用設定。本番は環境変数で上書きする。
    """
    return await asyncpg.connect(
        host=os.environ.get('PGHOST', 'localhost'),
        port=int(os.environ.get('PGPORT', '5432')),
        user=os.environ.get('PGUSER', 'postgres'),
        password=os.environ.get('PGPASSWORD', 'password'),
        database=os.environ.get('PGDATABASE', 'postgres')
    )


@app.get("/search", response_model=List[SearchResult])
async def search_posts(
    keyword: str = Query(..., min_length=1, description="検索キーワード"),
    min_views: Optional[int] = Query(None, ge=0, description="最小閲覧数"),
    limit: int = Query(10, le=100, description="取得件数")
):
    """
    基本的なキーワード検索API
    タイトルまたはコンテンツに対して全文検索を実行
    """
    conn = await get_db_connection()

    query = """
        SELECT id, title, author, view_count, published_at
        FROM blog_posts
        WHERE (title &@~ $1 OR content &@~ $1)
    """
    params = [keyword]

    if min_views is not None:
        query += " AND view_count >= $2"
        params.append(min_views)
        query += " ORDER BY view_count DESC LIMIT $3"
        params.append(limit)
    else:
        query += " ORDER BY view_count DESC LIMIT $2"
        params.append(limit)

    try:
        results = await conn.fetch(query, *params)
        return [dict(row) for row in results]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@app.get("/search/paginated", response_model=PaginatedSearchResult)
async def search_posts_paginated(
    keyword: str = Query(..., min_length=1, description="検索キーワード"),
    last_published_at: Optional[str] = Query(None, description="前ページ最終レコードのpublished_at"),
    last_id: Optional[int] = Query(None, description="前ページ最終レコードのid"),
    limit: int = Query(10, le=100, description="取得件数")
):
    """
    シーク法を使った高速ページネーション
    大量データでも一定の高速性を維持
    """
    conn = await get_db_connection()

    # 1件多く取得してhas_nextを判定
    fetch_limit = limit + 1

    if last_published_at and last_id:
        # 2ページ目以降
        query = """
            SELECT id, title, author, view_count, published_at
            FROM blog_posts
            WHERE (title &@~ $1 OR content &@~ $1)
              AND (published_at, id) < ($2, $3)
            ORDER BY published_at DESC, id DESC
            LIMIT $4
        """
        params = [keyword, last_published_at, last_id, fetch_limit]
    else:
        # 1ページ目
        query = """
            SELECT id, title, author, view_count, published_at
            FROM blog_posts
            WHERE (title &@~ $1 OR content &@~ $1)
            ORDER BY published_at DESC, id DESC
            LIMIT $2
        """
        params = [keyword, fetch_limit]

    try:
        results = await conn.fetch(query, *params)
        
        # has_nextフラグを判定
        has_next = len(results) > limit
        actual_results = results[:limit]

        # 最終レコードの情報を取得
        response_last_id = None
        response_last_published_at = None
        if actual_results:
            last_record = actual_results[-1]
            response_last_id = last_record['id']
            response_last_published_at = last_record['published_at']

        return {
            "results": [dict(row) for row in actual_results],
            "has_next": has_next,
            "last_id": response_last_id,
            "last_published_at": response_last_published_at
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@app.get("/search/by-tags")
async def search_by_tags(
    tag: str = Query(..., description="検索対象のタグ"),
    keyword: Optional[str] = Query(None, description="追加の全文検索キーワード"),
    limit: int = Query(10, le=100, description="取得件数")
):
    """
    タグ検索と全文検索の組み合わせ
    配列型のGINインデックスとPGroongaを併用
    """
    conn = await get_db_connection()

    if keyword:
        query = """
            SELECT id, title, tags, view_count
            FROM blog_posts
            WHERE $1 = ANY(tags)
              AND (title &@~ $2 OR content &@~ $2)
            ORDER BY view_count DESC
            LIMIT $3
        """
        params = [tag, keyword, limit]
    else:
        query = """
            SELECT id, title, tags, view_count
            FROM blog_posts
            WHERE $1 = ANY(tags)
            ORDER BY view_count DESC
            LIMIT $2
        """
        params = [tag, limit]

    try:
        results = await conn.fetch(query, *params)
        return [dict(row) for row in results]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@app.get("/suggest")
async def suggest_titles(
    prefix: str = Query(..., min_length=1, description="検索プレフィックス"),
    limit: int = Query(5, le=20, description="候補数")
):
    """
    検索候補（サジェスト）機能
    前方一致検索でオートコンプリートを実現
    """
    conn = await get_db_connection()

    query = """
        SELECT DISTINCT title
        FROM blog_posts
        WHERE title &^ $1
        LIMIT $2
    """

    try:
        results = await conn.fetch(query, prefix, limit)
        return [row['title'] for row in results]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

