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

# The AgentPlan provider id (火山 AgentPlan) is read from config.json so we can
# detect which provider is "the plan" even if it changes. We also accept any
# provider whose baseURL points at a */plan/* endpoint as a coding plan.
CONFIG_PATH = V2_DIR / "config.json"

# How many recent log lines to tail for live activity.
LIVE_LOG_TAIL = 400

_MS_PER_DAY = 86_400_000

# ---- 火山方舟（Ark）OpenAPI 用量查询 ----
# 凭证读取优先级：系统环境变量 > 项目目录下的 .volc.env 文件。
# .volc.env 格式（KEY=VALUE，每行一个）：
#   VOLC_AK_ID=AKLT...
#   VOLC_AK_SECRET=WlRJ...
# 该文件含敏感凭证，请勿提交到版本库或外发。
_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
_VOLC_ENV_FILE = _HERE / ".volc.env"


def _load_volc_env() -> None:
    """从 .volc.env 加载火山凭证到 os.environ（仅当系统环境变量未设置时）。"""
    if not _VOLC_ENV_FILE.exists():
        return
    try:
        for line in _VOLC_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_volc_env()

VOLC_AK_ID = os.environ.get("VOLC_AK_ID", "")
VOLC_AK_SECRET = os.environ.get("VOLC_AK_SECRET", "")
VOLC_HOST = "open.volcengineapi.com"
VOLC_SERVICE = "ark"
VOLC_REGION = "cn-beijing"
VOLC_VERSION = "2024-01-01"
# 云端用量刷新较慢且为远程调用，缓存一段时间避免 1.5s 轮询打爆 API。
VOLC_CACHE_TTL = 60.0


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _provider_plan_ids() -> set[str]:
    """Return the set of provider ids that are coding plans (AgentPlan / zai plan).

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
    return rows


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


def _read_plan_usage() -> dict:
    """读取火山方舟 AgentPlan 套餐的云端额度 + 用量明细。

    与本地 model_usage（token 计数）完全独立，二者并存展示。云端数据刷新较
    慢，缓存 VOLC_CACHE_TTL 秒；未配置凭证 / 调用失败时返回空结构（前端据此
    隐藏云端区块），不影响本地 token 展示。
    """
    now_ts = datetime.datetime.now().timestamp()
    cached_ts, cached = _VOLC_CACHE
    if cached is not None and (now_ts - cached_ts) < VOLC_CACHE_TTL:
        return cached

    empty = {
        "enabled": bool(VOLC_AK_ID and VOLC_AK_SECRET),
        "planType": "",
        "buckets": [],
        "error": "",
        "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    if not empty["enabled"]:
        _VOLC_CACHE[0] = now_ts
        _VOLC_CACHE[1] = empty
        return empty

    # 1) 套餐 AFP 额度（5小时/日/周/月 的额度、已用、重置时间）
    afp = _volc_call("GetAFPUsage", {})
    buckets: list[dict] = []
    plan_type = ""
    if afp is None:
        empty["error"] = "调用失败"
        _VOLC_CACHE[0] = now_ts
        _VOLC_CACHE[1] = empty
        return empty
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

    result = {
        "enabled": True,
        "planType": plan_type,
        "buckets": buckets,
        "error": "",
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


class Api:
    """JS bridge exposed to the webview as `api`."""

    def status(self):
        tasks = _read_tasks()
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
