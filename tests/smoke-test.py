#!/usr/bin/env python3
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path


BASE = "http://127.0.0.1:8080"
password = Path("/etc/finops-ai/admin_password").read_text(encoding="utf-8").strip()
cookie_jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))


def request(path: str, method: str = "GET", body: dict | None = None, timeout: int = 240) -> dict:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read())


def summary(path: str, result: dict) -> None:
    data = result.get("data")
    print(
        f"PASS {path} status={result.get('status', 'HTTP_OK')} "
        f"items={len(data) if isinstance(data, list) else '-'} "
        f"errors={len(result.get('errors', []))}"
    )


failures: list[str] = []
sensitive_pattern = re.compile(r"(?<!\d)\d{12}(?!\d)|AKIA[A-Z0-9]{16}|sk-[a-f0-9]{32}", re.IGNORECASE)

try:
    result = request(
        "/api/v1/auth/login",
        "POST",
        {"username": "finopsadmin", "password": password},
    )
    summary("auth/login", result)
except Exception as exc:
    print(f"FAIL auth/login {exc}")
    sys.exit(1)

today = date.today()
month_start = today.replace(day=1).isoformat()
tomorrow = (today + timedelta(days=1)).isoformat()

checks = [
    ("/api/v1/health", "GET", None),
    (f"/api/v1/cost/summary?start={month_start}&end={tomorrow}", "GET", None),
    (f"/api/v1/cost/trend?start={month_start}&end={tomorrow}", "GET", None),
    (f"/api/v1/cost/breakdown?start={month_start}&end={tomorrow}&dimension=SERVICE", "GET", None),
    ("/api/v1/anomalies", "GET", None),
    ("/api/v1/recommendations/summary", "GET", None),
    ("/api/v1/recommendations/idle?days=14", "GET", None),
    ("/api/v1/recommendations/rds?days=14", "GET", None),
    ("/api/v1/recommendations/aurora?days=14", "GET", None),
    (f"/api/v1/reconciliation/ce-cur?start={month_start}&end={tomorrow}", "GET", None),
    ("/api/v1/quicksight/cid/embed-url", "GET", None),
    ("/api/v1/ai/config", "GET", None),
    ("/api/v1/ai/test", "POST", {}),
    ("/api/v1/ai/chat", "POST", {"question": "本月两个账号的总成本是多少？请注明数据来源。"}),
]

for path, method, body in checks:
    try:
        result = request(path, method, body)
        summary(path.split("?")[0], result)
        if "/recommendations/" in path and path.split("?")[0].rsplit("/", 1)[-1] in {"idle", "rds", "aurora"}:
            with_cost = sum(1 for item in result.get("data", []) if item.get("actual_monthly_cost") is not None)
            print(f"  EVIDENCE actual_monthly_cost={with_cost}/{len(result.get('data', []))}")
            if path.startswith("/api/v1/recommendations/idle") and result.get("data"):
                recommendation_id = result["data"][0]["recommendation_id"]
                evidence = request(f"/api/v1/recommendations/{recommendation_id}/evidence")
                if evidence.get("status") != "OK" or not evidence.get("rule_id"):
                    raise RuntimeError("recommendation evidence endpoint returned incomplete data")
                print("  PASS recommendation evidence endpoint")
        if sensitive_pattern.search(json.dumps(result, ensure_ascii=False)):
            raise RuntimeError("sensitive identifier or credential pattern found in API response")
        if path.endswith("embed-url") and not result.get("embed_url"):
            raise RuntimeError("embed_url missing")
        if path.endswith("ai/test") and result.get("status") != "OK":
            raise RuntimeError(f"AI test status={result.get('status')}")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        detail = ""
        if isinstance(exc, urllib.error.HTTPError):
            try:
                detail = exc.read().decode()[:500]
            except Exception:
                detail = ""
        message = f"{path}: {exc} {detail}".strip()
        failures.append(message)
        print(f"FAIL {message}")

if failures:
    print(f"SMOKE_TEST_FAILED count={len(failures)}")
    sys.exit(1)
print("SMOKE_TEST_PASS")
