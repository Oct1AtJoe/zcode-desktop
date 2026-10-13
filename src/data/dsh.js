'use strict';

// DSH（DeepSeek Harness）用量统计 — 读 ~/.dsh/sessions 的 zstd 压缩会话日志。
// token 口径对齐 DSH GUI（token-meter/StatsLine）：usage 来自 assistant/chunk 的
// usage 类型 chunk 与 assistant/message 的 data.usage，同 turn/step 只取最后一份
// （后到替换，不累加）；输入 = uncachedInput + cacheRead + cacheWrite（billed 口径，
// GUI 的 billedInputTokens）。会话详情：session/title（标题）、user/message（首条
// 消息）、request/context（模型）、tool/call（工具名）。
// 15s TTL 缓存（{ts,payload} 模式），status() 路径零网络零阻塞。

const fs = require('fs');
const path = require('path');
const os = require('os');
const { zstdDecompressSync } = require('node:zlib');

const SESSIONS_ROOT = path.join(os.homedir(), '.dsh', 'sessions');
const CACHE_TTL = 15.0;
const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]); // zstd frame magic
const MAX_SESSION_LIST = 10; // 会话卡列表只回最近 N 个，聚合仍全量

let cache = { ts: 0, payload: null };

// 逐帧解压 zstd 流（每帧独立 frame，循环找 magic 边界解压拼接）
function decompressZstd(buf) {
  let out = '', pos = 0, frames = 0;
  while (pos < buf.length) {
    const idx = buf.indexOf(ZSTD_MAGIC, pos);
    if (idx < 0) break;
    const next = buf.indexOf(ZSTD_MAGIC, idx + 4);
    const end = next < 0 ? buf.length : next;
    try {
      out += zstdDecompressSync(buf.subarray(idx, end)).toString('utf8');
    } catch (e) {
      break; // 尾帧不完整（正在写入）：丢弃
    }
    pos = end;
    // 坑（2026-08-14 实测）：Electron 35 的 zstdDecompressSync 输出 Buffer 进
    // V8 老生代，JS 堆压力不足时 major GC 永不触发（external 不计数），大量帧
    // 的 Buffer 累积到数 GB → 进程 OOM 无声退出（exit -36861，无事件日志）。
    // 每 200 帧用临时大数组制造堆压力强制 major GC：单文件峰值 1.5GB→390MB。
    if (++frames % 200 === 0) {
      const junk = new Array(1 << 22);
      junk.length = 0;
    }
  }
  return out;
}

// 单步 token 桶。input 是 billed 口径（含缓存），cache 单独拆出供展示。
const ZERO = { input: 0, cache: 0, output: 0, reasoning: 0 };

function addTokens(a, b) {
  a.input += b.input || 0; a.cache += b.cache || 0;
  a.output += b.output || 0; a.reasoning += b.reasoning || 0;
}

// 从 usage 记录取单步 token（输入= billed 口径，对齐 GUI billedInputTokens）
function tokensFromUsage(u) {
  if (!u || typeof u !== 'object') return null;
  const input = u.inputTokens, out = u.outputTokens;
  if (typeof input !== 'number' && typeof out !== 'number') return null;
  const cache = (u.cacheReadTokens || 0) + (u.cacheWriteTokens || 0);
  return {
    input: (typeof input === 'number' ? input : 0) + cache,
    cache,
    output: typeof out === 'number' ? out : 0,
    reasoning: u.reasoningTokens || 0,
  };
}

// 统计单个会话：精确 token + 标题/模型/工具/时间
function statLog(logPath, sid) {
  let text;
  try {
    const buf = fs.readFileSync(logPath);
    text = logPath.endsWith('.zstd') ? decompressZstd(buf) : buf.toString('utf8');
  } catch (e) {
    return null;
  }
  const s = {
    id: sid, title: '', firstMsg: '', model: '', cwd: '',
    turns: 0, steps: 0, toolCalls: 0, tools: {},
    tokens: { ...ZERO }, tokensToday: { ...ZERO }, createdAt: 0, lastTs: 0,
  };
  // 同 turn/step 的 usage 只留最后一份（chunk 先行、message 收尾，后到替换不累加）
  const stepUsage = new Map();
  // 按模型聚合：request/context 声明当前请求模型，其后 usage 归该模型
  let currentModel = '';
  const modelTokens = new Map();
  for (const line of text.split('\n')) {
    let j;
    try { j = JSON.parse(line); } catch (e) { continue; }
    if (!j || typeof j !== 'object') continue;
    const t = j.type, d = j.data || {};
    if (t === 'session') {
      s.createdAt = Number(j.createdAt || 0);
      // 真实 cwd（workspace 目录名是有损编码，不可靠；basename 拿最后一层文件夹）
      if (typeof j.cwd === 'string' && j.cwd) s.cwd = j.cwd;
    } else if (t === 'turn/start' || t === 'turn/end') s.turns++;
    else if (t === 'step/start' || t === 'step/end') s.steps++;
    else if (t === 'tool/call') {
      s.toolCalls++;
      if (d.name) s.tools[d.name] = (s.tools[d.name] || 0) + 1;
    } else if (t === 'request/context') {
      if (d.model || d.provider) {
        s.model = s.model || (d.model || d.provider);
        currentModel = (d.provider ? d.provider + '/' : '') + (d.model || '');
      }
    } else if (t === 'session/title') {
      if (d.title) s.title = d.title;
    } else if (t === 'user/message') {
      const c = d.content;
      if (!s.firstMsg && Array.isArray(c)) {
        const txt = c.find(x => x && x.type === 'text' && typeof x.text === 'string');
        if (txt) s.firstMsg = txt.text;
      }
    } else if (t === 'assistant/chunk') {
      // usage 类型 chunk：流的早期样本（token-meter 同源）
      if (d.chunk && d.chunk.type === 'usage' && d.chunk.usage) {
        stepUsage.set(d.turn + ':' + d.step, { tk: tokensFromUsage(d.chunk.usage), model: currentModel, ts: Number(j.time || 0) });
      }
    } else if (t === 'assistant/message') {
      // 组装消息的最终 usage：覆盖同 step 的 chunk 样本
      if (d.usage) stepUsage.set(d.turn + ':' + d.step, { tk: tokensFromUsage(d.usage), model: currentModel, ts: Number(j.time || 0) });
    }
    const ts = Number(j.time || 0);
    if (ts > s.lastTs) s.lastTs = ts;
  }
  // 今日窗口：本地自然日 0 点（与 sqlite readUsage 同口径）
  const todayStartMs = new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate()).getTime();
  const todayModelTokens = new Map();
  for (const { tk, model, ts } of stepUsage.values()) {
    if (!tk) continue;
    addTokens(s.tokens, tk);
    const key = model || 'unknown';
    const bucket = modelTokens.get(key) || { ...ZERO };
    addTokens(bucket, tk);
    modelTokens.set(key, bucket);
    if (ts >= todayStartMs) {
      addTokens(s.tokensToday, tk);
      const tb = todayModelTokens.get(key) || { ...ZERO };
      addTokens(tb, tk);
      todayModelTokens.set(key, tb);
    }
  }
  s.modelTokens = Array.from(modelTokens.entries())
    .map(([model, tokens]) => ({ model, tokens }))
    .sort((a, b) => b.tokens.input - a.tokens.input);
  s.modelTokensToday = Array.from(todayModelTokens.entries())
    .map(([model, tokens]) => ({ model, tokens }))
    .sort((a, b) => b.tokens.input - a.tokens.input);
  // 标题兜底：无 session/title 时用首条消息截断
  if (!s.title && s.firstMsg) s.title = s.firstMsg.length > 40 ? s.firstMsg.slice(0, 40) + '…' : s.firstMsg;
  s.toolList = Object.entries(s.tools).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([name, calls]) => ({ name, calls }));
  return s;
}

// 找当前代数会话日志：优先 session.v<N>.jsonl[.zstd]（N 最大），回退 session.jsonl[.zstd]
// （DSH 0.1.5+ 格式升 v3 后日志名带版本后缀，届时 vN 文件才是最新活数据）
function findLog(dir) {
  let best = null;
  let legacy = null;
  for (const name of fs.readdirSync(dir)) {
    const m = /^session\.v(\d+)\.jsonl(?:\.zstd)?$/.exec(name);
    if (m) {
      const v = Number(m[1]);
      if (!best || v > best[0]) best = [v, path.join(dir, name)];
    } else if (name === 'session.jsonl' || name === 'session.jsonl.zstd') {
      legacy = path.join(dir, name);
    }
  }
  return best ? best[1] : legacy;
}

// 聚合所有 workspace 的会话统计
function scan() {
  const workspaces = [];
  const sessions = [];
  let total = { sessions: 0, turns: 0, steps: 0, toolCalls: 0, tokens: { ...ZERO }, tokensToday: { ...ZERO } };
  let latestTs = 0;
  let dirs;
  try { dirs = fs.readdirSync(SESSIONS_ROOT); } catch (e) { return { workspaces: [], sessions, total, latestTs, updatedAt: new Date().toISOString() }; }

  for (const wsDir of dirs) {
    const wsPath = path.join(SESSIONS_ROOT, wsDir);
    let st;
    try { st = fs.statSync(wsPath); } catch (e) { continue; }
    if (!st.isDirectory()) continue;
    let sids;
    try { sids = fs.readdirSync(wsPath); } catch (e) { continue; }
    const fallbackName = wsDir.replace(/^--|--$/g, '');
    const w = {
      name: fallbackName, // 首个会话真实 cwd basename 会覆盖（目录名是有损编码）
      sessions: 0, turns: 0, steps: 0, toolCalls: 0,
      tokens: { ...ZERO }, tokensToday: { ...ZERO }, latestTs: 0,
    };
    for (const sid of sids) {
      let logPath;
      try { logPath = findLog(path.join(wsPath, sid)); } catch (e) { continue; }
      if (!logPath) continue;
      const s = statLog(logPath, sid);
      if (!s) continue;
      const wsName = s.cwd ? path.basename(s.cwd) : fallbackName;
      if (w.sessions === 0 && wsName !== fallbackName) w.name = wsName;
      w.sessions++; w.turns += s.turns; w.steps += s.steps; w.toolCalls += s.toolCalls;
      addTokens(w.tokens, s.tokens);
      addTokens(w.tokensToday, s.tokensToday);
      if (s.lastTs > w.latestTs) w.latestTs = s.lastTs;
      sessions.push({ ...s, ws: wsName });
    }
    if (!w.sessions) continue;
    workspaces.push(w);
    total.sessions += w.sessions; total.turns += w.turns; total.steps += w.steps;
    total.toolCalls += w.toolCalls; addTokens(total.tokens, w.tokens);
    addTokens(total.tokensToday, w.tokensToday);
    if (w.latestTs > latestTs) latestTs = w.latestTs;
  }
  workspaces.sort((a, b) => b.latestTs - a.latestTs);
  // 会话卡按最近活动排序，截断到 MAX_SESSION_LIST
  sessions.sort((a, b) => b.lastTs - a.lastTs);
  // 全局模型分布（按 billed 输入降序），供大字悬浮 pop
  const modelMap = new Map();
  for (const s of sessions) {
    for (const mt of s.modelTokens || []) {
      const bucket = modelMap.get(mt.model) || { ...ZERO };
      addTokens(bucket, mt.tokens);
      modelMap.set(mt.model, bucket);
    }
  }
  const models = Array.from(modelMap.entries())
    .map(([model, tokens]) => ({ model, tokens }))
    .sort((a, b) => b.tokens.input - a.tokens.input);
  // 今日分模型分布（供 DSH 视图大字旁 ▦ 悬浮）
  const todayModelMap = new Map();
  for (const s of sessions) {
    for (const mt of s.modelTokensToday || []) {
      const bucket = todayModelMap.get(mt.model) || { ...ZERO };
      addTokens(bucket, mt.tokens);
      todayModelMap.set(mt.model, bucket);
    }
  }
  const todayModels = Array.from(todayModelMap.entries())
    .map(([model, tokens]) => ({ model, tokens }))
    .sort((a, b) => b.tokens.input - a.tokens.input);
  return {
    workspaces,
    sessions: sessions.slice(0, MAX_SESSION_LIST),
    total,
    models,
    todayModels,
    latestTs,
    updatedAt: new Date().toISOString(),
  };
}

function getDshUsage() {
  if (cache.payload && (Date.now() - cache.ts) / 1000 < CACHE_TTL) return cache.payload;
  cache = { ts: Date.now(), payload: scan() };
  return cache.payload;
}

module.exports = { getDshUsage, CACHE_TTL, _scan: scan };
