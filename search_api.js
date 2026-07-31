#!/usr/bin/env node
/**
 * PGroonga全文検索API実装例（Express + PostgreSQL）
 */
const express = require('express');
const rateLimit = require('express-rate-limit');
const { Pool } = require('pg');

const app = express();

// レートリミット: DBアクセスを伴う検索APIの乱用を防止
const limiter = rateLimit({
  windowMs: 60 * 1000, // 1分
  max: 60,             // 1分あたり60リクエストまで
  standardHeaders: true,
  legacyHeaders: false
});
app.use(limiter);
const pool = new Pool({
  host: 'localhost',
  port: 5432,
  user: 'postgres',
  password: 'password',
  database: 'postgres'
});

app.use(express.json());

/**
 * 基本的なキーワード検索API
 * タイトルまたはコンテンツに対して全文検索を実行
 */
app.get('/search', async (req, res) => {
  const { keyword, min_views, limit = 10 } = req.query;

  if (!keyword) {
    return res.status(400).json({ error: 'keyword is required' });
  }

  let query = `
    SELECT id, title, author, view_count, published_at
    FROM blog_posts
    WHERE (title &@~ $1 OR content &@~ $1)
  `;

  const params = [keyword];
  let paramIndex = 2;

  if (min_views) {
    query += ` AND view_count >= $${paramIndex}`;
    params.push(parseInt(min_views));
    paramIndex++;
  }

  query += ` ORDER BY view_count DESC LIMIT $${paramIndex}`;
  params.push(parseInt(limit));

  try {
    const result = await pool.query(query, params);
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * シーク法を使った高速ページネーション
 * 大量データでも一定の高速性を維持
 */
app.get('/search/paginated', async (req, res) => {
  const { keyword, last_published_at, last_id, limit = 10 } = req.query;

  if (!keyword) {
    return res.status(400).json({ error: 'keyword is required' });
  }

  // 1件多く取得してhas_nextを判定
  const fetchLimit = parseInt(limit) + 1;

  let query;
  let params;

  if (last_published_at && last_id) {
    // 2ページ目以降
    query = `
      SELECT id, title, author, view_count, published_at
      FROM blog_posts
      WHERE (title &@~ $1 OR content &@~ $1)
        AND (published_at, id) < ($2, $3)
      ORDER BY published_at DESC, id DESC
      LIMIT $4
    `;
    params = [keyword, last_published_at, parseInt(last_id), fetchLimit];
  } else {
    // 1ページ目
    query = `
      SELECT id, title, author, view_count, published_at
      FROM blog_posts
      WHERE (title &@~ $1 OR content &@~ $1)
      ORDER BY published_at DESC, id DESC
      LIMIT $2
    `;
    params = [keyword, fetchLimit];
  }

  try {
    const result = await pool.query(query, params);
    
    // has_nextフラグを判定
    const hasNext = result.rows.length > parseInt(limit);
    const actualResults = result.rows.slice(0, parseInt(limit));

    // 最終レコードの情報を取得
    let responseLastId = null;
    let responseLastPublishedAt = null;
    if (actualResults.length > 0) {
      const lastRecord = actualResults[actualResults.length - 1];
      responseLastId = lastRecord.id;
      responseLastPublishedAt = lastRecord.published_at;
    }

    res.json({
      results: actualResults,
      has_next: hasNext,
      last_id: responseLastId,
      last_published_at: responseLastPublishedAt
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * タグ検索と全文検索の組み合わせ
 * 配列型のGINインデックスとPGroongaを併用
 */
app.get('/search/by-tags', async (req, res) => {
  const { tag, keyword, limit = 10 } = req.query;

  if (!tag) {
    return res.status(400).json({ error: 'tag is required' });
  }

  let query;
  let params;

  if (keyword) {
    query = `
      SELECT id, title, tags, view_count
      FROM blog_posts
      WHERE $1 = ANY(tags)
        AND (title &@~ $2 OR content &@~ $2)
      ORDER BY view_count DESC
      LIMIT $3
    `;
    params = [tag, keyword, parseInt(limit)];
  } else {
    query = `
      SELECT id, title, tags, view_count
      FROM blog_posts
      WHERE $1 = ANY(tags)
      ORDER BY view_count DESC
      LIMIT $2
    `;
    params = [tag, parseInt(limit)];
  }

  try {
    const result = await pool.query(query, params);
    res.json(result.rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

/**
 * 検索候補（サジェスト）機能
 * 前方一致検索でオートコンプリートを実現
 */
app.get('/suggest', async (req, res) => {
  const { prefix, limit = 5 } = req.query;

  if (!prefix) {
    return res.status(400).json({ error: 'prefix is required' });
  }

  const query = `
    SELECT DISTINCT title
    FROM blog_posts
    WHERE title &^ $1
    LIMIT $2
  `;

  try {
    const result = await pool.query(query, [prefix, parseInt(limit)]);
    res.json(result.rows.map(row => row.title));
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});

