// DSH（DeepSeek Harness）用量统计 — 对齐 Electron src/data/dsh.js。
// 读 ~/.dsh/sessions 的 zstd 压缩会话日志（逐帧解压，对齐 JS 帧循环）。
// token 口径：usage 来自 assistant/chunk 与 assistant/message 的 usage，
// 同 turn/step 只取最后一份；输入 = uncachedInput + cacheRead + cacheWrite。
// 15s TTL 缓存，status() 路径零网络零阻塞。

use std::sync::{Mutex, OnceLock};

use serde_json::{json, Value};

use crate::data::sqlite::home_dir;
use crate::data::Cache;

const SESSIONS_ROOT: &str = ".dsh/sessions";
const CACHE_TTL_MS: u64 = 15_000;
const MAX_SESSION_LIST: usize = 10;
const ZSTD_MAGIC: [u8; 4] = [0x28, 0xb5, 0x2f, 0xfd];

fn cache() -> &'static Mutex<Cache> {
    static CACHE: OnceLock<Mutex<Cache>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(Cache::new()))
}

// 逐帧解压 zstd 流（每帧独立 frame，循环找 magic 边界解压拼接）。
// Rust 无 V8 GC 坑（Electron 版曾因 Buffer 进老生代累积数 GB OOM）。
fn decompress_zstd(buf: &[u8]) -> String {
    fn find_magic(hay: &[u8], from: usize) -> Option<usize> {
        (from..hay.len().saturating_sub(3)).find(|&i| hay[i..i + 4] == ZSTD_MAGIC)
    }
    let mut out = String::new();
    let mut pos = 0;
    while pos < buf.len() {
        let Some(idx) = find_magic(buf, pos) else { break };
        let next = find_magic(buf, idx + 4);
        let end = next.unwrap_or(buf.len());
        match zstd::stream::decode_all(std::io::Cursor::new(&buf[idx..end])) {
            Ok(decoded) => out.push_str(&String::from_utf8_lossy(&decoded)),
            Err(_) => break, // 尾帧不完整（正在写入）：丢弃
        }
        pos = end;
    }
    out
}

#[derive(Default, Clone)]
struct Tokens {
    input: i64,
    cache: i64,
    output: i64,
    reasoning: i64,
}

fn add_tokens(a: &mut Tokens, b: &Tokens) {
    a.input += b.input;
    a.cache += b.cache;
    a.output += b.output;
    a.reasoning += b.reasoning;
}

// 从 usage 记录取单步 token（输入 = billed 口径，对齐 GUI billedInputTokens）
fn tokens_from_usage(u: &Value) -> Option<Tokens> {
    let input = u.get("inputTokens").and_then(|v| v.as_i64());
    let out = u.get("outputTokens").and_then(|v| v.as_i64());
    if input.is_none() && out.is_none() {
        return None;
    }
    let cache = u.get("cacheReadTokens").and_then(|v| v.as_i64()).unwrap_or(0)
        + u.get("cacheWriteTokens").and_then(|v| v.as_i64()).unwrap_or(0);
    Some(Tokens {
        input: input.unwrap_or(0) + cache,
        cache,
        output: out.unwrap_or(0),
        reasoning: u.get("reasoningTokens").and_then(|v| v.as_i64()).unwrap_or(0),
    })
}

// 统计单个会话：精确 token + 标题/模型/工具/时间
// 找当前代数会话日志：优先 session.v<N>.jsonl[.zstd]（N 最大，未来格式升级 v4+ 自动兼容），
// 回退 session.jsonl[.zstd]。DSH 0.1.5+ 格式升 v3 后日志名带版本后缀
// （对齐 dsh-session-format/src/filename.ts：v0=session.jsonl，vN=session.vN.jsonl）。
fn find_log(dir: &std::path::Path) -> Option<std::path::PathBuf> {
    let mut best: Option<(u32, std::path::PathBuf)> = None;
    let mut legacy: Option<std::path::PathBuf> = None;
    for entry in std::fs::read_dir(dir).ok()?.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Some(rest) = name.strip_prefix("session.v") {
            let rest = rest.strip_suffix(".zstd").unwrap_or(rest);
            if let Some(v) = rest.strip_suffix(".jsonl") {
                if let Ok(v) = v.parse::<u32>() {
                    if best.as_ref().map_or(true, |(bv, _)| v > *bv) {
                        best = Some((v, entry.path()));
                    }
                }
            }
        } else if name == "session.jsonl" || name == "session.jsonl.zstd" {
            legacy = Some(entry.path());
        }
    }
    best.map(|(_, p)| p).or(legacy)
}

fn stat_log(log_path: &std::path::Path, sid: &str) -> Option<Value> {
    let buf = std::fs::read(log_path).ok()?;
    let text = if log_path.to_string_lossy().ends_with(".zstd") {
        decompress_zstd(&buf)
    } else {
        String::from_utf8_lossy(&buf).to_string()
    };

    let mut s = json!({
        "id": sid, "title": "", "firstMsg": "", "model": "", "cwd": "",
        "turns": 0, "steps": 0, "toolCalls": 0,
        "createdAt": 0, "lastTs": 0,
    });
    // 同 turn/step 的 usage 只留最后一份（chunk 先行、message 收尾，后到替换不累加）
    let mut step_usage: std::collections::HashMap<String, (Tokens, String, i64)> = std::collections::HashMap::new();
    let mut current_model = String::new();
    let mut tools: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
    let mut turns = 0i64;
    let mut steps = 0i64;
    let mut tool_calls = 0i64;
    let mut created_at = 0i64;
    let mut last_ts = 0i64;

    for line in text.lines() {
        let Ok(j) = serde_json::from_str::<Value>(line) else { continue };
        let t = j.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let d = j.get("data").cloned().unwrap_or(json!({}));
        match t {
            "session" => {
                created_at = j.get("createdAt").and_then(|v| v.as_i64()).unwrap_or(0);
                if let Some(cwd) = j.get("cwd").and_then(|v| v.as_str()) {
                    if !cwd.is_empty() {
                        s["cwd"] = json!(cwd);
                    }
                }
            }
            "turn/start" | "turn/end" => turns += 1,
            "step/start" | "step/end" => steps += 1,
            "tool/call" => {
                tool_calls += 1;
                if let Some(name) = d.get("name").and_then(|v| v.as_str()) {
                    *tools.entry(name.to_string()).or_insert(0) += 1;
                }
            }
            "request/context" => {
                if s.get("model").and_then(|v| v.as_str()).map(|m| m.is_empty()).unwrap_or(true) {
                    s["model"] = d
                        .get("model")
                        .or_else(|| d.get("provider"))
                        .cloned()
                        .unwrap_or(json!(""));
                }
                let provider = d.get("provider").and_then(|v| v.as_str()).unwrap_or("");
                let model = d.get("model").and_then(|v| v.as_str()).unwrap_or("");
                current_model = if provider.is_empty() { model.to_string() } else { format!("{provider}/{model}") };
            }
            "session/title" => {
                if let Some(title) = d.get("title").and_then(|v| v.as_str()) {
                    s["title"] = json!(title);
                }
            }
            "user/message" => {
                let first = s.get("firstMsg").and_then(|v| v.as_str()).map(|m| m.is_empty()).unwrap_or(true);
                if first {
                    if let Some(content) = d.get("content").and_then(|v| v.as_array()) {
                        if let Some(txt) = content.iter().find(|x| {
                            x.get("type").and_then(|v| v.as_str()) == Some("text")
                                && x.get("text").and_then(|v| v.as_str()).is_some()
                        }) {
                            s["firstMsg"] = txt.get("text").cloned().unwrap_or(json!(""));
                        }
                    }
                }
            }
            "assistant/chunk" => {
                // usage 类型 chunk：流的早期样本（token-meter 同源）
                if let Some(chunk) = d.get("chunk") {
                    if chunk.get("type").and_then(|v| v.as_str()) == Some("usage") {
                        if let Some(u) = chunk.get("usage") {
                            if let Some(tk) = tokens_from_usage(u) {
                                let key = format!("{}:{}", d.get("turn").and_then(|v| v.as_i64()).unwrap_or(0), d.get("step").and_then(|v| v.as_i64()).unwrap_or(0));
                                step_usage.insert(key, (tk, current_model.clone(), j.get("time").and_then(|v| v.as_i64()).unwrap_or(0)));
                            }
                        }
                    }
                }
            }
            "assistant/message" => {
                // 组装消息的最终 usage：覆盖同 step 的 chunk 样本
                if let Some(u) = d.get("usage") {
                    if let Some(tk) = tokens_from_usage(u) {
                        let key = format!("{}:{}", d.get("turn").and_then(|v| v.as_i64()).unwrap_or(0), d.get("step").and_then(|v| v.as_i64()).unwrap_or(0));
                        step_usage.insert(key, (tk, current_model.clone(), j.get("time").and_then(|v| v.as_i64()).unwrap_or(0)));
                    }
                }
            }
            _ => {}
        }
        let ts = j.get("time").and_then(|v| v.as_i64()).unwrap_or(0);
        if ts > last_ts {
            last_ts = ts;
        }
    }

    s["turns"] = json!(turns);
    s["steps"] = json!(steps);
    s["toolCalls"] = json!(tool_calls);
    s["createdAt"] = json!(created_at);
    s["lastTs"] = json!(last_ts);

    let mut total = Tokens::default();
    let mut total_today = Tokens::default();
    let mut model_tokens: std::collections::HashMap<String, Tokens> = std::collections::HashMap::new();
    let mut today_model_tokens: std::collections::HashMap<String, Tokens> = std::collections::HashMap::new();
    // 今日窗口：本地自然日 0 点（对齐 JS 端 readUsage 口径）
    let today_start_ms = chrono::Local::now()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_local_timezone(chrono::Local)
        .earliest()
        .unwrap()
        .timestamp_millis();
    for (tk, model, ts) in step_usage.values() {
        add_tokens(&mut total, tk);
        let key = if model.is_empty() { "unknown".into() } else { model.clone() };
        let bucket = model_tokens.entry(key.clone()).or_default();
        add_tokens(bucket, tk);
        if *ts >= today_start_ms {
            add_tokens(&mut total_today, tk);
            let tb = today_model_tokens.entry(key).or_default();
            add_tokens(tb, tk);
        }
    }
    s["tokens"] = json!({
        "input": total.input, "cache": total.cache,
        "output": total.output, "reasoning": total.reasoning,
    });
    s["tokensToday"] = json!({
        "input": total_today.input, "cache": total_today.cache,
        "output": total_today.output, "reasoning": total_today.reasoning,
    });
    let mut model_list: Vec<(String, Tokens)> = model_tokens.into_iter().collect();
    model_list.sort_by(|a, b| b.1.input.cmp(&a.1.input));
    s["modelTokens"] = json!(model_list
        .into_iter()
        .map(|(model, tokens)| json!({
            "model": model,
            "tokens": { "input": tokens.input, "cache": tokens.cache, "output": tokens.output, "reasoning": tokens.reasoning },
        }))
        .collect::<Vec<_>>());
    let mut today_model_list: Vec<(String, Tokens)> = today_model_tokens.into_iter().collect();
    today_model_list.sort_by(|a, b| b.1.input.cmp(&a.1.input));
    s["modelTokensToday"] = json!(today_model_list
        .into_iter()
        .map(|(model, tokens)| json!({
            "model": model,
            "tokens": { "input": tokens.input, "cache": tokens.cache, "output": tokens.output, "reasoning": tokens.reasoning },
        }))
        .collect::<Vec<_>>());

    // 标题兜底：无 session/title 时用首条消息截断
    let title = s.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let first = s.get("firstMsg").and_then(|v| v.as_str()).unwrap_or("");
    if title.is_empty() && !first.is_empty() {
        s["title"] = json!(if first.len() > 40 { format!("{}…", &first[..40]) } else { first.to_string() });
    }
    s["tools"] = json!(tools);
    let mut tool_list: Vec<(String, i64)> = tools.into_iter().collect();
    tool_list.sort_by(|a, b| b.1.cmp(&a.1));
    s["toolList"] = json!(tool_list
        .into_iter()
        .take(3)
        .map(|(name, calls)| json!({ "name": name, "calls": calls }))
        .collect::<Vec<_>>());
    Some(s)
}

// 聚合所有 workspace 的会话统计
fn scan() -> Value {
    let root = home_dir().join(SESSIONS_ROOT);
    let mut workspaces: Vec<Value> = Vec::new();
    let mut sessions: Vec<Value> = Vec::new();
    let mut total = json!({ "sessions": 0, "turns": 0, "steps": 0, "toolCalls": 0 });
    let mut total_tokens = Tokens::default();
    let mut total_today_tokens = Tokens::default();
    let mut latest_ts: i64 = 0;
    let mut global_models: std::collections::HashMap<String, Tokens> = std::collections::HashMap::new();

    let dirs = std::fs::read_dir(&root).map(|it| it.filter_map(|e| e.ok()).collect::<Vec<_>>()).unwrap_or_default();
    for entry in dirs {
        let ws_path = entry.path();
        if !ws_path.is_dir() {
            continue;
        }
        let ws_dir = entry.file_name().to_string_lossy().to_string();
        let fallback_name = ws_dir.trim_matches('-').to_string();
        let mut w = json!({
            "name": fallback_name,
            "sessions": 0, "turns": 0, "steps": 0, "toolCalls": 0,
            "latestTs": 0,
        });
        let mut w_tokens = Tokens::default();
        let mut w_sessions = 0i64;
        let mut w_turns = 0i64;
        let mut w_steps = 0i64;
        let mut w_tool_calls = 0i64;
        let mut w_latest_ts = 0i64;
        let mut w_name = fallback_name.clone();

        let sids = std::fs::read_dir(&ws_path).map(|it| it.filter_map(|e| e.ok()).collect::<Vec<_>>()).unwrap_or_default();
        for sid_entry in sids {
            // 0.1.5-rc.2 起会话日志写 session.v3.jsonl.zstd，旧 session.jsonl.zstd 只是迁移前快照
            let Some(log_path) = find_log(&sid_entry.path()) else { continue };
            let sid = sid_entry.file_name().to_string_lossy().to_string();
            let Some(s) = stat_log(&log_path, &sid) else { continue };
            // workspace 名用首个会话真实 cwd basename 覆盖（目录名是有损编码）
            if w_sessions == 0 {
                if let Some(cwd) = s.get("cwd").and_then(|v| v.as_str()) {
                    if let Some(base) = std::path::Path::new(cwd).file_name() {
                        w_name = base.to_string_lossy().to_string();
                    }
                }
            }
            w_sessions += 1;
            w_turns += s.get("turns").and_then(|v| v.as_i64()).unwrap_or(0);
            w_steps += s.get("steps").and_then(|v| v.as_i64()).unwrap_or(0);
            w_tool_calls += s.get("toolCalls").and_then(|v| v.as_i64()).unwrap_or(0);
            if let Some(tk) = s.get("tokens") {
                add_tokens(&mut w_tokens, &tokens_from_value(tk));
            }
            if let Some(tk) = s.get("tokensToday") {
                add_tokens(&mut total_today_tokens, &tokens_from_value(tk));
            }
            let st = s.get("lastTs").and_then(|v| v.as_i64()).unwrap_or(0);
            if st > w_latest_ts {
                w_latest_ts = st;
            }
            let mut s = s.clone();
            s["ws"] = json!(w_name.clone());
            sessions.push(s);
        }

        if w_sessions == 0 {
            continue;
        }
        w["name"] = json!(w_name);
        w["sessions"] = json!(w_sessions);
        w["turns"] = json!(w_turns);
        w["steps"] = json!(w_steps);
        w["toolCalls"] = json!(w_tool_calls);
        w["tokens"] = json!({
            "input": w_tokens.input, "cache": w_tokens.cache,
            "output": w_tokens.output, "reasoning": w_tokens.reasoning,
        });
        w["latestTs"] = json!(w_latest_ts);
        workspaces.push(w);

        total["sessions"] = json!(total["sessions"].as_i64().unwrap_or(0) + w_sessions);
        total["turns"] = json!(total["turns"].as_i64().unwrap_or(0) + w_turns);
        total["steps"] = json!(total["steps"].as_i64().unwrap_or(0) + w_steps);
        total["toolCalls"] = json!(total["toolCalls"].as_i64().unwrap_or(0) + w_tool_calls);
        add_tokens(&mut total_tokens, &w_tokens);
        if w_latest_ts > latest_ts {
            latest_ts = w_latest_ts;
        }
    }

    workspaces.sort_by(|a, b| b.get("latestTs").and_then(|v| v.as_i64()).cmp(&a.get("latestTs").and_then(|v| v.as_i64())));
    sessions.sort_by(|a, b| b.get("lastTs").and_then(|v| v.as_i64()).cmp(&a.get("lastTs").and_then(|v| v.as_i64())));
    // 全局模型分布（按 billed 输入降序），供大字悬浮 pop
    for s in &sessions {
        if let Some(mts) = s.get("modelTokens").and_then(|v| v.as_array()) {
            for mt in mts {
                let model = mt.get("model").and_then(|v| v.as_str()).unwrap_or("unknown").to_string();
                let bucket = global_models.entry(model).or_default();
                add_tokens(bucket, &tokens_from_value(mt.get("tokens").unwrap_or(&json!({}))));
            }
        }
    }
    let mut models: Vec<(String, Tokens)> = global_models.into_iter().collect();
    models.sort_by(|a, b| b.1.input.cmp(&a.1.input));
    // 今日分模型分布（供 DSH 视图大字旁 ▦ 悬浮）
    let mut today_global_models: std::collections::HashMap<String, Tokens> = std::collections::HashMap::new();
    for s in &sessions {
        if let Some(mts) = s.get("modelTokensToday").and_then(|v| v.as_array()) {
            for mt in mts {
                let model = mt.get("model").and_then(|v| v.as_str()).unwrap_or("unknown").to_string();
                let bucket = today_global_models.entry(model).or_default();
                add_tokens(bucket, &tokens_from_value(mt.get("tokens").unwrap_or(&json!({}))));
            }
        }
    }
    let mut today_models: Vec<(String, Tokens)> = today_global_models.into_iter().collect();
    today_models.sort_by(|a, b| b.1.input.cmp(&a.1.input));

    total["tokens"] = json!({
        "input": total_tokens.input, "cache": total_tokens.cache,
        "output": total_tokens.output, "reasoning": total_tokens.reasoning,
    });
    total["tokensToday"] = json!({
        "input": total_today_tokens.input, "cache": total_today_tokens.cache,
        "output": total_today_tokens.output, "reasoning": total_today_tokens.reasoning,
    });

    json!({
        "workspaces": workspaces,
        "sessions": sessions.into_iter().take(MAX_SESSION_LIST).collect::<Vec<_>>(),
        "total": total,
        "models": models
            .into_iter()
            .map(|(model, tokens)| json!({
                "model": model,
                "tokens": { "input": tokens.input, "cache": tokens.cache, "output": tokens.output, "reasoning": tokens.reasoning },
            }))
            .collect::<Vec<_>>(),
        "todayModels": today_models
            .into_iter()
            .map(|(model, tokens)| json!({
                "model": model,
                "tokens": { "input": tokens.input, "cache": tokens.cache, "output": tokens.output, "reasoning": tokens.reasoning },
            }))
            .collect::<Vec<_>>(),
        "latestTs": latest_ts,
        "updatedAt": chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    })
}

fn tokens_from_value(v: &Value) -> Tokens {
    Tokens {
        input: v.get("input").and_then(|x| x.as_i64()).unwrap_or(0),
        cache: v.get("cache").and_then(|x| x.as_i64()).unwrap_or(0),
        output: v.get("output").and_then(|x| x.as_i64()).unwrap_or(0),
        reasoning: v.get("reasoning").and_then(|x| x.as_i64()).unwrap_or(0),
    }
}

pub fn get_dsh_usage() -> Value {
    let mut guard = cache().lock().unwrap();
    if guard.fresh(CACHE_TTL_MS) {
        return guard.get();
    }
    let v = scan();
    guard.set(v.clone());
    v
}
