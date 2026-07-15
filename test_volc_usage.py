# -*- coding: utf-8 -*-
"""测试火山方舟 OpenAPI 用量查询接口（SigV4 签名）。

凭证从环境变量读取，不硬编码：
  VOLC_AK_ID      Access Key ID
  VOLC_AK_SECRET  Secret Access Key

只做只读查询：GetAFPUsage / GetUsageDetails / GetInferenceUsage / ListSeatAFPUsage。
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone, timedelta

import requests

# ====== 凭证（从环境变量读取，不硬编码） ======
AK_ID = os.environ.get("VOLC_AK_ID", "")
AK_SECRET = os.environ.get("VOLC_AK_SECRET", "")

# ====== 公共参数 ======
HOST = "open.volcengineapi.com"
SERVICE = "ark"
REGION = "cn-beijing"
VERSION = "2024-01-01"
SCHEME = "https"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(secret.encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "request")
    return k_signing


def sign_v4_request(method, action, body: dict):
    """构造一个 SigV4 签名的请求并返回 (url, headers, body_bytes)。"""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_hash = _sha256_hex(body_bytes)

    canonical_uri = "/"
    canonical_querystring = f"Action={action}&Version={VERSION}"

    # signed headers: host;x-content-sha256;x-date
    signed_headers_str = "host;x-content-sha256;x-date"
    canonical_headers = (
        f"host:{HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{amz_date}\n"
    )

    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_querystring,
        canonical_headers,
        signed_headers_str,
        payload_hash,
    ])

    hashed_canonical_request = _sha256_hex(canonical_request.encode("utf-8"))

    credential_scope = f"{date_stamp}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        amz_date,
        credential_scope,
        hashed_canonical_request,
    ])

    signing_key = _get_signing_key(AK_SECRET, date_stamp, REGION, SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={AK_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={signature}"
    )

    headers = {
        "Host": HOST,
        "Content-Type": "application/json; charset=utf-8",
        "X-Date": amz_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
    url = f"{SCHEME}://{HOST}/?{canonical_querystring}"
    return url, headers, body_bytes


def call(action, body=None, label=None):
    body = body or {}
    url, headers, body_bytes = sign_v4_request("POST", action, body)
    print(f"\n{'='*70}")
    print(f"[{label or action}] POST {url}")
    print(f"body: {body}")
    try:
        resp = requests.post(url, headers=headers, data=body_bytes, timeout=30)
        print(f"HTTP {resp.status_code}")
        try:
            print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(resp.text[:2000])
    except Exception as e:
        print(f"EXC: {e!r}")


def _check_creds():
    if not AK_ID or not AK_SECRET:
        print("ERROR: 缺少环境变量 VOLC_AK_ID / VOLC_AK_SECRET")
        print("请先设置：")
        print('  export VOLC_AK_ID="你的AccessKeyID"')
        print('  export VOLC_AK_SECRET="你的SecretAccessKey"')
        return False
    return True


if __name__ == "__main__":
    if not _check_creds():
        raise SystemExit(1)

    # 用最近 14 天作为查询窗口
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    # 1) 套餐 AFP 额度（无需参数，直接返回额度/已用/重置时间）
    call("GetAFPUsage", {}, label="GetAFPUsage 套餐AFP额度")

    # 2) 套餐用量详情（按模型 token 维度）
    #    QueryInterval 必须是 "Day" 或 "Hour"；StartTime/EndTime 格式 YYYY-MM-DD
    call("GetUsageDetails", {
        "QueryInterval": "Day",
        "Filter": {"StartTime": start, "EndTime": today},
    }, label="GetUsageDetails 套餐用量详情(按模型)")

    # 3) 推理 token 用量（StartTime/EndTime 是顶层参数，格式 YYYY-MM-DD）
    call("GetInferenceUsage", {
        "QueryInterval": "Day",
        "StartTime": start,
        "EndTime": today,
    }, label="GetInferenceUsage 推理用量")

    # 4) 席位 AFP 额度用量列表（个人套餐返回空列表，正常）
    call("ListSeatAFPUsage", {"ProjectName": "default"}, label="ListSeatAFPUsage 席位额度列表")
