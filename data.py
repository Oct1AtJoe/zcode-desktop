"""Data backend for the ZCode desktop widget.

Reads live data from three sources under ~/.zcode:
  1. v2/tasks-index.sqlite       -> current/running task + recent task list
  2. cli/db/db.sqlite model_usage-> persistent, locally-accumulated token
                                    usage (per completed LLM response; never
                                    pruned). This is the authoritative source
                                    for "today" / "total" token stats.
  3. cli/log/zcode-<date>.jsonl  -> real-time tool calls / model activity

Exposed to the frontend through the pywebview JS bridge as `api.status()`.
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

import requests

ZCODE_DIR = Path(os.path.expanduser("~")) / ".zcode"
V2_DIR = ZCODE_DIR / "v2"
CLI_DIR = ZCODE_DIR / "cli"
DB_PATH = V2_DIR / "tasks-index.sqlite"
# model_usage is the authoritative, locally-accumulated token counter that the
# zcode agent maintains. Every completed LLM response writes one row with its
# usage; rows persist across sessions (unlike rollout/*.jsonl, which are
# periodically pruned). This is what the zcode settings panel reports as "today".
MODEL_USAGE_DB = CLI_DIR / "db" / "db.sqlite"
ROLLOUT_DIR = CLI_DIR / "rollout"
LOG_DIR = CLI_DIR / "log"

# Coding plan providers (火山 CodingPlan / AgentPlan) are detected from
# config.json so we can tell which provider is "the plan" even if it changes.
# We accept any provider whose baseURL points at a */plan/* endpoint as a plan.
CONFIG_PATH = V2_DIR / "config.json"

# How many recent log lines to tail for live activity.
LIVE_LOG_TAIL = 400

# A turn.started with no matching turn.completed means the task is actively
# running. But a turn can be left "open" forever if ZCode crashed or was force
# killed mid-turn, so only trust turns newer than this cutoff.
ACTIVE_TURN_FRESH_MS = 30 * 60 * 1000  # 30 minutes

_MS_PER_DAY = 86_400_000

# ---- 火山方舟（Ark）OpenAPI 用量查询 ----
# 凭证读取优先级：系统环境变量 > exe/脚本同目录 .volc.env > 用户家目录 .volc.env。
# .volc.env 格式（KEY=VALUE，每行一个）：
#   VOLC_AK_ID=AKLT...
#   VOLC_AK_SECRET=WlRJ...
# 该文件含敏感凭证，请勿提交到版本库或外发。
if getattr(sys, "frozen", False):
    # PyInstaller --onefile: exe 所在目录（用户放 .volc.env 的地方）。
    _HERE = Path(os.path.dirname(sys.executable))
else:
    _HERE = Path(os.path.dirname(os.path.abspath(__file__)))
# 备用：用户家目录（exe 放在只读位置时仍可配置）。
_HOME_VOLC_ENV = Path(os.path.expanduser("~")) / ".volc.env"
_VOLC_ENV_FILE = _HERE / ".volc.env"


def _load_volc_env() -> None:
    """从 .volc.env 加载火山凭证到 os.environ（仅当系统环境变量未设置时）。

    依次查找：exe/脚本同目录 -> 用户家目录，找到第一个即用。
    """
    for path in (_VOLC_ENV_FILE, _HOME_VOLC_ENV):
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
            break  # 读到一个即可
        except Exception:
            continue


_load_volc_env()

VOLC_AK_ID = os.environ.get("VOLC_AK_ID", "")
VOLC_AK_SECRET = os.environ.get("VOLC_AK_SECRET", "")
VOLC_HOST = "open.volcengineapi.com"
VOLC_SERVICE = "ark"
VOLC_REGION = "cn-beijing"
VOLC_VERSION = "2024-01-01"
# 云端用量刷新较慢且为远程调用，缓存一段时间避免 1.5s 轮询打爆 API。
VOLC_CACHE_TTL = 60.0
# 套餐类型：coding（编程套餐，调 GetCodingPlanUsage）或 agent（AgentPlan，
# 调 GetAFPUsage）。在 .volc.env 里用 VOLC_PLAN_TYPE=coding|agent 选择，未配置
# 时默认 coding（当前在售套餐）。
VOLC_PLAN_TYPE = (os.environ.get("VOLC_PLAN_TYPE", "") or "coding").strip().lower()
if VOLC_PLAN_TYPE not in ("coding", "agent"):
    VOLC_PLAN_TYPE = "coding"
# 套餐档位标签（可选）：火山接口不返回档位（Pro/标准版/Max…），由用户在
# .volc.env 里填 VOLC_PLAN_TIER 自行标注，前端 badge 显示成"编程套餐·Pro"。
# 留空则只显示套餐类型名。
VOLC_PLAN_TIER = (os.environ.get("VOLC_PLAN_TIER", "") or "").strip()

# 套餐开通时间（可选，用于 weekly/monthly 窗口倒推本地 token 明细）。
# 去火山方舟控制台 -> CodingPlan 订阅页查「开通时间/生效时间」，精确到分钟。
# 格式：2026-07-15T23:19:00 或 2026-07-15 23:19:00。未配置时 session 正常
# 统计；weekly/monthly 不聚合本地 token，前端提示用户去配置。
_VOLC_PLAN_START_RAW = (os.environ.get("VOLC_PLAN_START", "") or "").strip()
VOLC_PLAN_START: datetime.datetime | None = None
for _fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
    try:
        VOLC_PLAN_START = datetime.datetime.strptime(_VOLC_PLAN_START_RAW, _fmt)
        break
    except ValueError:
        continue
del _VOLC_PLAN_START_RAW


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _provider_plan_ids() -> set[str]:
    """Return the set of provider ids that are coding plans (CodingPlan / AgentPlan).

    A provider is considered a plan if its baseURL contains '/plan/' (the
    volcengine ark plan endpoint and the zcode-plan endpoint both do) OR its
    name contains 'plan'.
    """
    cfg = _load_config()
    out: set[str] = set()
    for pid, info in cfg.get("provider", {}).items():
        url = (info.get("options", {}) or {}).get("baseURL", "") or ""
        name = (info.get("name", "") or "").lower()
        if "/plan" in url or "plan" in name:
            out.add(pid)
    return out


def _read_tasks() -> list[dict]:
    """Read all non-deleted tasks from the SQLite index, newest first."""
    if not DB_PATH.exists():
        return []
    rows = []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT task_id, title, task_status, provider, model, mode,
                      created_at, updated_at, meta_json
               FROM tasks
               WHERE deleted = 0
               ORDER BY updated_at DESC
               LIMIT 40"""
        )
        for r in cur.fetchall():
            meta = {}
            if r["meta_json"]:
                try:
                    meta = json.loads(r["meta_json"])
                except Exception:
                    meta = {}
            rows.append(
                {
                    "taskId": r["task_id"],
                    "title": r["title"] or "(未命名任务)",
                    "status": r["task_status"],
                    "provider": r["provider"],
                    "model": r["model"],
                    "mode": r["mode"],
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                    "workspacePath": meta.get("workspacePath", ""),
                    "thoughtLevel": meta.get("thoughtLevel", ""),
                }
            )
        conn.close()
    except Exception:
        return []

    # ZCode lags at flipping task_status back to 'running' when a new message
    # is sent in an already-completed task, and never updates `mode` when the
    # user switches modes mid-session (it records the creation-time mode).
    # Override both with real-time signals from the log: any task whose session
    # currently has an open turn is running, and each session's last
    # `session.mode.updated` is its current mode.
    active = _active_session_ids()
    modes = _session_modes()
    for t in rows:
        if t["taskId"] in active:
            t["status"] = "running"
        if t["taskId"] in modes:
            t["mode"] = modes[t["taskId"]]
    return rows


def _read_task_tokens(task_ids: list[str]) -> dict:
    """Aggregate token usage per task from `model_usage`.

    `tasks.task_id` is the same value as `model_usage.session_id`, so we can
    group token totals back onto each task in a single query.
    Returns { task_id: {total, input, output, requests} }.
    """
    if not task_ids or not MODEL_USAGE_DB.exists():
        return {}
    out: dict = {}
    try:
        conn = sqlite3.connect(f"file:{MODEL_USAGE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in task_ids)
        cur.execute(
            f"""SELECT session_id,
                       COALESCE(SUM(computed_total_tokens),0) AS total,
                       COALESCE(SUM(input_tokens),0)        AS input,
                       COALESCE(SUM(output_tokens),0)       AS output,
                       COUNT(*)                              AS requests
                FROM model_usage
                WHERE status = 'completed' AND session_id IN ({placeholders})
                GROUP BY session_id""",
            task_ids,
        )
        for r in cur.fetchall():
            out[r["session_id"]] = {
                "total": r["total"],
                "input": r["input"],
                "output": r["output"],
                "requests": r["requests"],
            }
        conn.close()
    except Exception:
        return {}
    return out


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")
    except Exception:
        return ""


def _read_usage() -> dict:
    """Aggregate token usage from the authoritative `model_usage` SQLite table.

    The zcode agent writes one row per completed LLM response with its usage,
    accumulating it locally across all sessions/tasks. This is the same source
    the zcode settings panel reports as "today" / "total" - it is persistent
    (never pruned), unlike the rollout/*.jsonl files used previously.

    The headline number is TODAY's cumulative usage across all tasks (not just
    the latest request), matching what the user expects.
    """
    if not MODEL_USAGE_DB.exists():
        return _empty_usage()

    now = datetime.datetime.now()
    today_start_ms = int(
        datetime.datetime.combine(now.date(), datetime.time.min).timestamp() * 1000
    )
    week_ago_ms = today_start_ms - 7 * _MS_PER_DAY

    try:
        conn = sqlite3.connect(f"file:{MODEL_USAGE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
    except Exception:
        return _empty_usage()

    try:
        # Only count completed requests (skip running/cancelled/error rows so the
        # number matches the panel's settled usage).
        ok = "status = 'completed'"

        def _sum(where: str, params=()):
            cur.execute(
                f"""SELECT COALESCE(SUM(input_tokens),0) AS ti,
                           COALESCE(SUM(output_tokens),0) AS toks,
                           COALESCE(SUM(computed_total_tokens),0) AS tt,
                           COALESCE(SUM(cache_read_input_tokens),0) AS cr,
                           COALESCE(SUM(reasoning_tokens),0) AS rt,
                           COUNT(*) AS reqs
                    FROM model_usage WHERE {where}""",
                params,
            )
            return cur.fetchone()

        today_row = _sum(f"{ok} AND completed_at >= ?", (today_start_ms,))
        week_row = _sum(f"{ok} AND completed_at >= ?", (week_ago_ms,))
        all_row = _sum(ok)

        # Per-model breakdown for today (so the user can see glm-5.2 vs others).
        # model_id is folded to lowercase so "GLM-5.2" and "glm-5.2" merge.
        cur.execute(
            f"""SELECT LOWER(model_id) AS mid,
                      COUNT(*) AS reqs,
                      COALESCE(SUM(input_tokens),0) AS ti,
                      COALESCE(SUM(output_tokens),0) AS toks,
                      COALESCE(SUM(computed_total_tokens),0) AS tt
               FROM model_usage
               WHERE {ok} AND completed_at >= ?
               GROUP BY mid
               ORDER BY tt DESC""",
            (today_start_ms,),
        )
        models = [
            {
                "model": r["mid"],
                "requests": r["reqs"],
                "inputTokens": r["ti"],
                "outputTokens": r["toks"],
                "totalTokens": r["tt"],
            }
            for r in cur.fetchall()
        ]

        # Most recent activity timestamp.
        cur.execute(
            "SELECT MAX(completed_at) AS mx FROM model_usage WHERE completed_at IS NOT NULL"
        )
        last_ms = cur.fetchone()["mx"]
        last_ts = ""
        if last_ms:
            try:
                last_ts = datetime.datetime.fromtimestamp(last_ms / 1000).isoformat()
            except Exception:
                last_ts = ""
    except Exception:
        conn.close()
        return _empty_usage()
    conn.close()

    today_in, today_out = today_row["ti"], today_row["toks"]
    return {
        "label": "模型用量",
        # Today is the headline.
        "today": {
            "inputTokens": today_in,
            "outputTokens": today_out,
            "totalTokens": today_row["tt"],
            "cacheReadTokens": today_row["cr"],
            "reasoningTokens": today_row["rt"],
            "requests": today_row["reqs"],
        },
        "week": {
            "inputTokens": week_row["ti"],
            "outputTokens": week_row["toks"],
            "totalTokens": week_row["tt"],
            "requests": week_row["reqs"],
        },
        "total": {  # all-time
            "inputTokens": all_row["ti"],
            "outputTokens": all_row["toks"],
            "totalTokens": all_row["tt"],
            "cacheReadTokens": all_row["cr"],
            "reasoningTokens": all_row["rt"],
            "requests": all_row["reqs"],
        },
        "grandTotal": {
            "inputTokens": all_row["ti"],
            "outputTokens": all_row["toks"],
            "totalTokens": all_row["tt"],
        },
        # Keep legacy field names so the frontend degrades gracefully; point
        # them at today so the in/out ratio reflects the current day.
        "models": models,
        "lastActivity": last_ts,
        "updatedAt": now.isoformat(timespec="seconds"),
    }


def _empty_usage() -> dict:
    z = {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cacheReadTokens": 0,
        "reasoningTokens": 0,
        "requests": 0,
    }
    return {
        "label": "模型用量",
        "today": dict(z),
        "week": dict(z),
        "total": dict(z),
        "grandTotal": dict(z),
        "models": [],
        "lastActivity": "",
        "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }


# ---- 火山方舟 OpenAPI 调用（SigV4 签名） ----
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _volc_signing_key(date_stamp: str) -> bytes:
    k_date = _hmac_sha256(VOLC_AK_SECRET.encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, VOLC_REGION)
    k_service = _hmac_sha256(k_region, VOLC_SERVICE)
    return _hmac_sha256(k_service, "request")


def _volc_call(action: str, body: dict) -> dict | None:
    """对一个火山方舟 OpenAPI Action 做 SigV4 签名 POST，返回 Result 或 None。

    纯只读调用，任何异常都吞掉返回 None，避免影响本地用量展示。
    """
    if not VOLC_AK_ID or not VOLC_AK_SECRET:
        return None
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload_hash = _sha256_hex(body_bytes)

        canonical_querystring = f"Action={action}&Version={VOLC_VERSION}"
        signed_headers = "host;x-content-sha256;x-date"
        canonical_headers = (
            f"host:{VOLC_HOST}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{amz_date}\n"
        )
        canonical_request = "\n".join([
            "POST", "/", canonical_querystring, canonical_headers,
            signed_headers, payload_hash,
        ])
        credential_scope = f"{date_stamp}/{VOLC_REGION}/{VOLC_SERVICE}/request"
        string_to_sign = "\n".join([
            "HMAC-SHA256", amz_date, credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        signing_key = _volc_signing_key(date_stamp)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={VOLC_AK_ID}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Host": VOLC_HOST,
            "Content-Type": "application/json; charset=utf-8",
            "X-Date": amz_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": authorization,
        }
        url = f"https://{VOLC_HOST}/?{canonical_querystring}"
        resp = requests.post(url, headers=headers, data=body_bytes, timeout=8)
        data = resp.json()
        if resp.status_code != 200:
            return None
        return data.get("Result") or {}
    except Exception:
        return None


# 云端用量缓存：(timestamp, payload)
_VOLC_CACHE: list = [0.0, None]


def _plan_window_models(start_ms: int) -> list[dict]:
    """按套餐窗口起点聚合本地 model_usage 表的分模型 token 明细。

    在 [start_ms, now] 区间内，按 model_id 分组求和 computed_total_tokens，
    返回按 token 降序的明细列表。窗口倒推自云端 ResetTimestamp（session 减 5h、
    weekly 减 7d、monthly 月份减 1），与云端 Percent 互补--Percent 反映额度水位，
    本地明细反映「这个窗口各模型烧了多少 token」。异常/无库时返回空列表。

    只统计套餐 provider（_provider_plan_ids）的用量，避免把非 plan 模型
    （如 mimo / gpt / deepseek 等自带 key 的 provider）计入套餐窗口明细。
    """
    if not MODEL_USAGE_DB.exists() or not start_ms:
        return []
    plan_pids = _provider_plan_ids()
    if not plan_pids:
        return []
    try:
        conn = sqlite3.connect(f"file:{MODEL_USAGE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        placeholders = ",".join("?" for _ in plan_pids)
        cur.execute(
            f"""SELECT LOWER(model_id) AS mid,
                       COUNT(*) AS reqs,
                       COALESCE(SUM(computed_total_tokens), 0) AS tt
                FROM model_usage
                WHERE status = 'completed'
                  AND completed_at >= ? AND completed_at <= ?
                  AND provider_id IN ({placeholders})
                GROUP BY mid
                ORDER BY tt DESC""",
            (start_ms, now_ms, *plan_pids),
        )
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    total = sum(r["tt"] for r in rows) or 0
    return [
        {
            "model": r["mid"],
            "tokens": r["tt"],
            "requests": r["reqs"],
            "pct": round(r["tt"] / total * 100, 1) if total else 0.0,
        }
        for r in rows
    ]


def _parse_coding_plan(cp: dict) -> dict:
    """解析 GetCodingPlanUsage 的 Result。

    CodingPlan 不返回绝对 Quota/Used，只返回各窗口已用百分比（Percent，单位为
    百分点 0-100，非 0-1 比例）与重置时间；桶为 session / weekly / monthly。

    同时根据 ResetTimestamp 倒推各窗口的本地 token 聚合起点：
      - session : reset - 5h（不依赖开通日）
      - weekly  : max(reset - 7d, VOLC_PLAN_START)
      - monthly : max(reset 月份-1, VOLC_PLAN_START)
    weekly/monthly 在 VOLC_PLAN_START 未配置时标记 needsConfig，由 _read_plan_usage
    跳过聚合、前端提示用户去 .volc.env 配置。
    """
    buckets: list[dict] = []
    label_map = {"session": "会话", "weekly": "每周", "monthly": "每月"}
    for item in cp.get("QuotaUsage", []) or []:
        level = item.get("Level", "") or ""
        if level not in label_map:
            continue
        used_pct = round(float(item.get("Percent", 0.0) or 0.0), 1)
        remaining_pct = round(max(0.0, 100.0 - used_pct), 1)
        reset_ts = item.get("ResetTimestamp") or 0
        reset_ms = (reset_ts * 1000) if reset_ts else 0

        # 倒推本地 token 聚合的窗口起点（毫秒）。
        window_start_ms = 0
        needs_config = False
        if reset_ms:
            reset_dt = datetime.datetime.fromtimestamp(reset_ts)
            if level == "session":
                window_start_ms = int((reset_dt - datetime.timedelta(hours=5)).timestamp() * 1000)
            elif level == "weekly":
                if VOLC_PLAN_START is None:
                    needs_config = True
                else:
                    start = reset_dt - datetime.timedelta(days=7)
                    if start < VOLC_PLAN_START:
                        start = VOLC_PLAN_START
                    window_start_ms = int(start.timestamp() * 1000)
            elif level == "monthly":
                if VOLC_PLAN_START is None:
                    needs_config = True
                else:
                    # 月份减 1（年进位），归零到当天 00:00 再取 max(开通时刻)。
                    m = reset_dt.month - 1 or 12
                    y = reset_dt.year - (reset_dt.month == 1)
                    start = datetime.datetime(y, m, reset_dt.day, 0, 0, 0)
                    if start < VOLC_PLAN_START:
                        start = VOLC_PLAN_START
                    window_start_ms = int(start.timestamp() * 1000)

        buckets.append({
            "key": level,
            "label": label_map[level],
            "quota": 100,
            "used": used_pct,
            "remaining": remaining_pct,
            "remainingPct": remaining_pct,
            "usedPct": used_pct,
            "resetMs": reset_ms,
            "windowStart": window_start_ms,
            "needsConfig": needs_config,
            "models": [],
        })
    return {
        "rawStatus": cp.get("Status", "") or "",
        "buckets": buckets,
    }


def _parse_agent_plan(afp: dict) -> dict:
    """解析 GetAFPUsage 的 Result（旧 AgentPlan 套餐）。

    返回绝对 Quota/Used/ResetTime；桶为 AFPFiveHour / AFPWeekly / AFPMonthly。
    """
    buckets: list[dict] = []
    plan_type = afp.get("PlanType", "") or ""
    label_map = {
        "AFPFiveHour": "5小时",
        "AFPWeekly": "每周",
        "AFPMonthly": "每月",
    }
    for key, label in label_map.items():
        b = afp.get(key)
        if not b:
            continue
        quota = b.get("Quota", 0) or 0
        used = b.get("Used", 0) or 0
        buckets.append({
            "key": key,
            "label": label,
            "quota": quota,
            "used": used,
            "remaining": max(0.0, quota - used),
            "remainingPct": round((quota - used) / quota * 100, 1) if quota else 0.0,
            "usedPct": round(used / quota * 100, 1) if quota else 0.0,
            "resetMs": b.get("ResetTime") or 0,
        })
    # agent 套餐没有独立的运行状态字段，PlanType 即档位标识（如 AFPMonthly）。
    return {
        "rawStatus": "",
        "buckets": buckets,
    }


def _read_plan_usage() -> dict:
    """读取火山方舟套餐的云端用量。

    按 VOLC_PLAN_TYPE 选择套餐类型：coding -> GetCodingPlanUsage（编程套餐，
    仅百分比），agent -> GetAFPUsage（旧 AgentPlan，绝对额度）。两种解析结果
    归一化成相同结构（status / buckets[quota|used|remaining|remainingPct|
    usedPct|resetMs]），前端按统一逻辑渲染。

    与本地 model_usage（token 计数）完全独立，二者并存展示。云端数据刷新较慢，
    缓存 VOLC_CACHE_TTL 秒；未配置凭证 / 调用失败时返回空结构（前端据此隐藏
    云端区块），不影响本地 token 展示。
    """
    now_ts = datetime.datetime.now().timestamp()
    cached_ts, cached = _VOLC_CACHE
    if cached is not None and (now_ts - cached_ts) < VOLC_CACHE_TTL:
        return cached

    empty = {
        "enabled": bool(VOLC_AK_ID and VOLC_AK_SECRET),
        "planType": VOLC_PLAN_TYPE,
        "tier": VOLC_PLAN_TIER,
        "rawStatus": "",
        "buckets": [],
        "error": "",
        "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    if not empty["enabled"]:
        _VOLC_CACHE[0] = now_ts
        _VOLC_CACHE[1] = empty
        return empty

    action = "GetCodingPlanUsage" if VOLC_PLAN_TYPE == "coding" else "GetAFPUsage"
    resp = _volc_call(action, {})
    if resp is None:
        empty["error"] = "调用失败"
        _VOLC_CACHE[0] = now_ts
        _VOLC_CACHE[1] = empty
        return empty

    parsed = (_parse_coding_plan if VOLC_PLAN_TYPE == "coding" else _parse_agent_plan)(resp)
    raw_status = parsed["rawStatus"]

    # coding 套餐：对每个已倒推出 windowStart 且不需要配置的 bucket，聚合本地
    # model_usage 的分模型 token 明细。needsConfig=True 的 bucket（未配置
    # VOLC_PLAN_START）跳过，models 保持空列表，由前端提示用户去配置。
    if VOLC_PLAN_TYPE == "coding":
        for b in parsed["buckets"]:
            if b.get("needsConfig") or not b.get("windowStart"):
                continue
            b["models"] = _plan_window_models(b["windowStart"])

    result = {
        "enabled": True,
        "planType": VOLC_PLAN_TYPE,
        "tier": VOLC_PLAN_TIER,
        "rawStatus": raw_status,
        "buckets": parsed["buckets"],
        # coding 套餐 Status 不是 Running 时（如已到期/暂停），提示用户。
        "error": ("套餐未生效（%s）" % raw_status) if (VOLC_PLAN_TYPE == "coding" and raw_status and raw_status != "Running") else "",
        "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _VOLC_CACHE[0] = now_ts
    _VOLC_CACHE[1] = result
    return result


def _read_live_activity() -> dict:
    """Tail the zcode log to find the most recent tool call / model activity.

    Returns the latest tool call (started without a matching completed = running)
    plus recent events for the activity feed.
    """
    # pick today's (or newest) log file
    files = sorted(glob.glob(str(LOG_DIR / "zcode-*.jsonl")))
    if not files:
        return {"currentTool": None, "activity": [], "turnActive": False}
    fp = files[-1]
    try:
        with open(fp, encoding="utf-8") as f:
            lines = f.readlines()[-LIVE_LOG_TAIL:]
    except Exception:
        return {"currentTool": None, "activity": [], "turnActive": False}

    # map toolCallId -> {started, completed}
    tool_calls: dict[str, dict] = {}
    events: list[dict] = []
    turn_active = False

    for line in lines:
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        ev = obj.get("event", "")
        ts = obj.get("timestamp", "")
        ctx = obj.get("context") or {}
        sess = obj.get("sessionId", "")
        tool_name = ctx.get("toolName", "")

        if ev == "tool.call.started":
            tcid = obj.get("toolCallId", "")
            tool_calls[tcid] = {
                "tool": tool_name,
                "startedAt": ts,
                "completedAt": None,
                "durationMs": None,
                "sessionId": sess,
            }
            events.append(
                {
                    "type": "tool_start",
                    "tool": tool_name,
                    "ts": ts,
                    "sessionId": sess,
                }
            )
        elif ev == "tool.call.completed":
            tcid = obj.get("toolCallId", "")
            if tcid in tool_calls:
                tool_calls[tcid]["completedAt"] = ts
                tool_calls[tcid]["durationMs"] = obj.get("durationMs")
            events.append(
                {
                    "type": "tool_end",
                    "tool": tool_name,
                    "ts": ts,
                    "durationMs": obj.get("durationMs"),
                    "sessionId": sess,
                }
            )
        elif ev == "model.request.completed":
            events.append(
                {
                    "type": "model",
                    "ts": ts,
                    "sessionId": sess,
                    "model": ctx.get("modelId", ""),
                    "durationMs": obj.get("durationMs"),
                }
            )
        elif ev == "turn.started":
            turn_active = True
            events.append({"type": "turn_start", "ts": ts, "sessionId": sess})
        elif ev == "turn.completed":
            turn_active = False
            events.append({"type": "turn_end", "ts": ts, "sessionId": sess})

    # a tool is "currently running" if it started but never completed
    running_tool = None
    for tc in tool_calls.values():
        if tc["completedAt"] is None:
            running_tool = tc
            break

    # last 8 events for the feed, newest last
    activity = events[-8:]
    return {
        "currentTool": running_tool,
        "activity": activity,
        "turnActive": turn_active,
        "logFile": os.path.basename(fp),
    }


def _read_recent_log_lines() -> list[str]:
    """Read all lines of the newest zcode log file.

    Both `_active_session_ids` (turn status) and `_session_modes` (current
    mode) need to scan the log; sharing one read halves the I/O on each poll.
    The single-day file is ~10k lines / ~60ms to parse, and turn/mode events
    are rare (~2-3%), so a whole-file scan (not a fixed tail) is both cheap
    and necessary -- a long turn with many tool calls can push its
    `turn.started` past any fixed tail window.
    """
    files = sorted(glob.glob(str(LOG_DIR / "zcode-*.jsonl")))
    if not files:
        return []
    try:
        with open(files[-1], encoding="utf-8") as f:
            return f.readlines()
    except Exception:
        return []


def _active_session_ids() -> set[str]:
    """Return top-level sessionIds that currently have an open (running) turn.

    ZCode's tasks-index DB updates `task_status` back to 'running' with a lag
    when a new message is sent in an already-completed task -- the widget keeps
    showing 'completed' until ZCode rewrites the row (often only on task
    switch). The zcode log, by contrast, emits `turn.started` immediately when a
    turn begins and `turn.completed` when it ends, and its `sessionId` is the
    same value as `tasks.task_id`. So a top-level session whose last turn event
    is `turn.started` (with no later `turn.completed`) is running *right now*.

    Only top-level sessions are considered; subagent sessions
    (`sess_subagent_*`) are ignored since they are not user tasks. A freshness
    cutoff discards turns left open by a crash/kill long ago.
    """
    lines = _read_recent_log_lines()
    if not lines:
        return set()

    # last turn state per top-level sessionId: True=open, False=closed
    last_turn_open: dict[str, bool] = {}
    last_turn_ts: dict[str, str] = {}
    now_ms = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
    for line in lines:
        # cheap pre-filter: skip lines that can't be turn events without a
        # full json parse (turn events are ~2% of lines).
        if '"turn.' not in line:
            continue
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        ev = obj.get("event", "")
        if ev not in ("turn.started", "turn.completed"):
            continue
        sess = obj.get("sessionId", "")
        # only top-level user sessions (not subagents)
        if not sess.startswith("sess_") or "subagent" in sess:
            continue
        last_turn_open[sess] = ev == "turn.started"
        last_turn_ts[sess] = obj.get("timestamp", "")

    active: set[str] = set()
    for sess, is_open in last_turn_open.items():
        if not is_open:
            continue
        ts = last_turn_ts.get(sess, "")
        try:
            ts_ms = datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            ).timestamp() * 1000
        except Exception:
            # unparseable timestamp -> trust it (safer to show running)
            active.add(sess)
            continue
        if now_ms - ts_ms <= ACTIVE_TURN_FRESH_MS:
            active.add(sess)
    return active


def _session_modes() -> dict[str, str]:
    """Return each top-level session's most recent mode from the log.

    Mirrors the status fix: `tasks.mode` in the DB is the mode the task was
    *created* with and is not updated when the user switches modes mid-session
    (e.g. yolo -> plan -> edit). The log emits `session.mode.updated` with the
    session's `sessionId` (== `tasks.task_id`) and `context.mode`, so the last
    such event per session is its current mode. Returns {sessionId: mode};
    sessions with no mode-update event keep whatever the DB recorded.
    """
    lines = _read_recent_log_lines()
    if not lines:
        return {}
    last_mode: dict[str, str] = {}
    for line in lines:
        if "session.mode.updated" not in line:
            continue
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        if obj.get("event") != "session.mode.updated":
            continue
        sess = obj.get("sessionId", "")
        if not sess.startswith("sess_") or "subagent" in sess:
            continue
        mode = (obj.get("context") or {}).get("mode", "")
        if mode:
            last_mode[sess] = mode
    return last_mode


class Api:
    """JS bridge exposed to the webview as `api`."""

    def status(self):
        tasks = _read_tasks()
        # attach per-task token usage (tasks.task_id == model_usage.session_id)
        task_tokens = _read_task_tokens([t["taskId"] for t in tasks])
        _zero = {"total": 0, "input": 0, "output": 0, "requests": 0}
        for t in tasks:
            t["tokens"] = task_tokens.get(t["taskId"], _zero)
        # the "current task" is the most recently updated running task; if none
        # running, fall back to the newest task overall.
        current = None
        for t in tasks:
            if t["status"] == "running":
                current = t
                break
        if current is None and tasks:
            current = tasks[0]

        # normalize datetimes to readable strings + keep relative time
        for t in tasks:
            t["updatedAtLabel"] = _fmt_ts(t["updatedAt"])
            t["createdAtLabel"] = _fmt_ts(t["createdAt"])

        if current:
            current["updatedAtLabel"] = _fmt_ts(current["updatedAt"])
            current["createdAtLabel"] = _fmt_ts(current["createdAt"])

        return {
            "currentTask": current,
            "recentTasks": tasks[:8],
            "usage": _read_usage(),
            "planUsage": _read_plan_usage(),
            "live": _read_live_activity(),
            "now": datetime.datetime.now().strftime("%H:%M:%S"),
        }

    # allow the frontend to trigger a close from a button. The launcher sets
    # self.window after creating the webview window.
    def quit(self):
        win = getattr(self, "window", None)
        if win is not None:
            win.destroy()
            return
        import webview

        for win in webview.windows:
            win.destroy()

    # ---- custom window dragging (easy_drag is buggy in pywebview 5.4) ----
    def getPos(self):
        win = getattr(self, "window", None)
        if win is None:
            return {"x": 0, "y": 0, "w": 0, "h": 0}
        return {"x": win.x, "y": win.y, "w": win.width, "h": win.height}

    def moveWindow(self, x, y):
        win = getattr(self, "window", None)
        if win is not None:
            win.move(int(x), int(y))

    def resizeWindow(self, w, h):
        win = getattr(self, "window", None)
        if win is not None:
            win.resize(int(w), int(h))

    def openTask(self, workspacePath):
        """Open ZCode and navigate to the task's workspace.

        Uses the registered zcode:// protocol with the open-project route.
        ZCode cannot jump to an existing session by ID, so we open the
        workspace directory instead.
        """
        if not workspacePath:
            return
        import urllib.parse
        import webbrowser

        encoded = urllib.parse.quote(workspacePath, safe="")
        url = f"zcode://open-project?directory={encoded}"
        webbrowser.open(url)
