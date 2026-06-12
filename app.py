#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


APP_NAME = "PT Login Keeper"
DEFAULT_SUCCESS_KEYWORDS = "退出\n用户中心\n控制面板\n上传量\n下载量\n魔力\n积分"
DEFAULT_FAILURE_KEYWORDS = "登录\n登入\n注册\nlogin\npassword"
STATE_LOCK = threading.Lock()
RUN_ALL_EVENT = threading.Event()
RUN_SITE_EVENTS: set[str] = set()


@dataclass
class CheckResult:
    status: str
    message: str
    http_status: int | None = None
    final_url: str = ""
    stats: dict[str, Any] | None = None


def config_dir() -> Path:
    path = Path(os.getenv("CONFIG_DIR", "/config"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


def now_ts() -> int:
    return int(time.time())


def format_time(ts: int | str | None) -> str:
    if not ts:
        return "-"
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {
            "settings": {
                "warn_days": 7,
                "notify_cooldown_hours": 12,
                "webhook_url": "",
                "wecom_robot_webhook": "",
                "wecom_app_corpid": "",
                "wecom_app_agentid": "",
                "wecom_app_secret": "",
                "wecom_app_touser": "@all",
                "serverchan_sendkey": "",
                "pushplus_token": "",
            },
            "sites": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.warning("Failed to load config: %s", exc)
        return {"settings": {}, "sites": []}
    if not isinstance(data, dict):
        return {"settings": {}, "sites": []}
    data.setdefault("settings", {})
    data.setdefault("sites", [])
    return data


def save_config(data: dict[str, Any]) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def split_lines(value: str | None) -> list[str]:
    if not value:
        return []
    return [line.strip() for line in re.split(r"[\n,;]+", value) if line.strip()]


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_site(data: dict[str, Any], site_id: str) -> dict[str, Any] | None:
    for site in data.get("sites", []):
        if site.get("id") == site_id:
            return site
    return None


def normalize_site(form: dict[str, list[str]], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = dict(existing or {})
    site_id = form_value(form, "id") or existing.get("id") or uuid.uuid4().hex[:12]
    interval_hours = max(as_int(form_value(form, "interval_hours"), 600), 1)
    expire_days = max(as_int(form_value(form, "expire_days"), 30), 1)
    stats_report_interval_days = max(as_int(form_value(form, "stats_report_interval_days"), 25), 1)
    manual_login_reminder_days = max(as_int(form_value(form, "manual_login_reminder_days"), 30), 1)
    request_headers = form_value(form, "request_headers").strip()
    extracted = parse_request_headers(request_headers)
    cookie = form_value(form, "cookie").strip() or extracted.get("cookie", "")
    authorization = form_value(form, "authorization").strip() or extracted.get("authorization", "")
    user_agent = form_value(form, "user_agent").strip() or extracted.get("user-agent", "")
    referer = form_value(form, "referer").strip() or extracted.get("referer", "")
    accept_language = form_value(form, "accept_language").strip() or extracted.get("accept-language", "")
    check_method = (form_value(form, "check_method").strip() or existing.get("check_method") or "GET").upper()
    if check_method not in {"GET", "POST"}:
        check_method = "GET"
    mteam_sign = "mteam_sign" in form
    if "mteam_sign_present" not in form and existing and "mteam_sign" in existing:
        mteam_sign = as_bool(existing.get("mteam_sign"), False)
    stats_report_enabled = "stats_report_enabled" in form
    if "stats_report_enabled_present" not in form:
        stats_report_enabled = as_bool(existing.get("stats_report_enabled"), True)
    manual_login_reminder_enabled = "manual_login_reminder_enabled" in form
    if "manual_login_reminder_enabled_present" not in form:
        manual_login_reminder_enabled = as_bool(existing.get("manual_login_reminder_enabled"), True)
    return {
        **existing,
        "id": site_id,
        "created_at": existing.get("created_at") or now_ts(),
        "enabled": "enabled" in form,
        "name": form_value(form, "name").strip(),
        "url": form_value(form, "url").strip(),
        "check_url": form_value(form, "check_url").strip(),
        "check_method": check_method,
        "mteam_sign": mteam_sign,
        "request_headers": request_headers,
        "cookie": cookie,
        "authorization": authorization,
        "did": form_value(form, "did").strip() or extracted.get("did", "") or existing.get("did", ""),
        "visitor_id": (
            form_value(form, "visitor_id").strip()
            or extracted.get("visitorid", "")
            or extracted.get("visitorId", "")
            or existing.get("visitor_id", "")
        ),
        "api_version": form_value(form, "api_version").strip() or extracted.get("version", "") or existing.get("api_version", ""),
        "web_version": (
            form_value(form, "web_version").strip()
            or extracted.get("webversion", "")
            or extracted.get("webVersion", "")
            or existing.get("web_version", "")
        ),
        "user_agent": user_agent,
        "referer": referer,
        "accept_language": accept_language,
        "success_keywords": form_value(form, "success_keywords").strip(),
        "failure_keywords": form_value(form, "failure_keywords").strip(),
        "interval_hours": interval_hours,
        "expire_days": expire_days,
        "stats_report_enabled": stats_report_enabled,
        "stats_report_interval_days": stats_report_interval_days,
        "manual_login_reminder_enabled": manual_login_reminder_enabled,
        "manual_login_reminder_days": manual_login_reminder_days,
    }


def form_value(form: dict[str, list[str]], name: str, default: str = "") -> str:
    values = form.get(name)
    if not values:
        return default
    return values[0]


def parse_request_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not raw.strip():
        return headers

    def add_header(line: str) -> None:
        if ":" not in line:
            return
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if name and value:
            headers[name] = value

    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = []
    if parts and parts[0].lower() == "curl":
        idx = 1
        while idx < len(parts):
            part = parts[idx]
            if part in {"-H", "--header"} and idx + 1 < len(parts):
                add_header(parts[idx + 1])
                idx += 2
                continue
            if part in {"-A", "--user-agent"} and idx + 1 < len(parts):
                headers["user-agent"] = parts[idx + 1]
                idx += 2
                continue
            if part in {"-e", "--referer"} and idx + 1 < len(parts):
                headers["referer"] = parts[idx + 1]
                idx += 2
                continue
            idx += 1

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        add_header(line)

    known = {
        "cookie",
        "authorization",
        "user-agent",
        "referer",
        "accept-language",
        "did",
        "visitorid",
        "version",
        "webversion",
    }
    for idx, line in enumerate(lines[:-1]):
        key = line.rstrip(":").strip().lower()
        if key in known and key not in headers and ":" not in line:
            headers[key] = lines[idx + 1].strip()
    return headers


def make_mteam_signature(url: str, method: str, timestamp: str) -> str:
    secret = os.getenv("MTEAM_SECRET", "HLkPcWmycL57mfJt")
    path = urlparse(url).path or "/"
    message = f"{method.upper()}&{path}&{timestamp}"
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def format_bytes_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_idx = 0
    while size >= 1024 and unit_idx < len(units) - 1:
        size /= 1024
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)} {units[unit_idx]}"
    return f"{size:.2f} {units[unit_idx]}"


def format_number_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def format_ratio_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def extract_json_site_stats(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    member_count = data.get("memberCount")
    if not isinstance(member_count, dict):
        member_count = data.get("member_count") if isinstance(data.get("member_count"), dict) else {}
    member_status = data.get("memberStatus")
    if not isinstance(member_status, dict):
        member_status = data.get("member_status") if isinstance(data.get("member_status"), dict) else {}

    stats = {
        "username": str(first_present(data.get("username"), data.get("name"), data.get("userName"))),
        "uploaded": format_bytes_value(first_present(member_count.get("uploaded"), data.get("uploaded"))),
        "downloaded": format_bytes_value(first_present(member_count.get("downloaded"), data.get("downloaded"))),
        "share_rate": format_ratio_value(first_present(member_count.get("shareRate"), data.get("shareRate"), data.get("ratio"))),
        "bonus": format_number_value(first_present(member_count.get("bonus"), data.get("bonus"))),
        "last_login": str(first_present(member_status.get("lastLogin"), data.get("lastLogin"))),
        "last_browse": str(first_present(member_status.get("lastBrowse"), data.get("lastBrowse"))),
    }
    return {key: value for key, value in stats.items() if value}


def extract_html_site_stats(text: str) -> dict[str, str]:
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = html.unescape(re.sub(r"\s+", " ", plain))

    def find_value(patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, plain, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    uploaded = find_value([
        r"(?:上传量|已上传|Uploaded)\s*[:：]?\s*([0-9.,]+\s*(?:[KMGTPE]i?B|[KMGTPE]B|TB|GB|MB|KB|B))",
    ])
    downloaded = find_value([
        r"(?:下载量|已下载|Downloaded)\s*[:：]?\s*([0-9.,]+\s*(?:[KMGTPE]i?B|[KMGTPE]B|TB|GB|MB|KB|B))",
    ])
    share_rate = find_value([
        r"(?:分享率|分享比率|Share\s*Ratio|Ratio)\s*[:：]?\s*([0-9.,]+)",
    ])
    bonus = find_value([
        r"(?:魔力|魔力值|积分|Bonus)\s*[:：]?\s*([0-9.,]+)",
    ])
    stats = {
        "uploaded": uploaded,
        "downloaded": downloaded,
        "share_rate": share_rate,
        "bonus": bonus,
    }
    return {key: value for key, value in stats.items() if value}


def extract_site_stats(text: str) -> dict[str, str]:
    stats = extract_json_site_stats(text)
    if stats:
        return stats
    return extract_html_site_stats(text)


def build_stats_report_body(site: dict[str, Any], stats: dict[str, Any]) -> str:
    lines = [f"站点：{site.get('name')}"]
    if stats.get("username"):
        lines.append(f"用户：{stats.get('username')}")
    if stats.get("uploaded"):
        lines.append(f"上传量：{stats.get('uploaded')}")
    if stats.get("downloaded"):
        lines.append(f"下载量：{stats.get('downloaded')}")
    if stats.get("share_rate"):
        lines.append(f"分享率：{stats.get('share_rate')}")
    if stats.get("bonus"):
        lines.append(f"魔力/积分：{stats.get('bonus')}")
    if stats.get("last_login"):
        lines.append(f"站点记录上次登录：{stats.get('last_login')}")
    if stats.get("last_browse"):
        lines.append(f"站点记录上次浏览：{stats.get('last_browse')}")
    lines.append(f"检测时间：{format_time(now_ts())}")
    return "\n".join(lines)


def check_site(site: dict[str, Any]) -> CheckResult:
    cookie = str(site.get("cookie") or "").strip()
    authorization = str(site.get("authorization") or "").strip()
    if not cookie and not authorization:
        return CheckResult("missing_auth", "未填写 Cookie 或 Authorization")

    check_url = str(site.get("check_url") or site.get("url") or "").strip()
    if not check_url:
        return CheckResult("bad_config", "未填写检测地址")

    session = requests.Session()
    headers = {
        "User-Agent": str(site.get("user_agent") or os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        )).strip(),
        "Accept-Language": str(site.get("accept_language") or "zh-CN,zh;q=0.9,en;q=0.7").strip(),
    }
    if cookie:
        headers["Cookie"] = cookie
    if authorization:
        # M-Team's API is case-sensitive in practice, even though HTTP header
        # names should be case-insensitive. Lowercase keeps it compatible.
        headers["authorization"] = authorization
    referer = str(site.get("referer") or "").strip()
    if referer:
        headers["Referer"] = referer
    if str(site.get("did") or "").strip():
        headers["did"] = str(site.get("did")).strip()
    if str(site.get("visitor_id") or "").strip():
        headers["visitorId"] = str(site.get("visitor_id")).strip()
    if str(site.get("api_version") or "").strip():
        headers["version"] = str(site.get("api_version")).strip()
    if str(site.get("web_version") or "").strip():
        headers["webVersion"] = str(site.get("web_version")).strip()
    session.headers.update(headers)

    timeout = as_int(os.getenv("REQUEST_TIMEOUT"), 30)
    method = str(site.get("check_method") or "GET").strip().upper()
    if method not in {"GET", "POST"}:
        method = "GET"
    params: dict[str, str] = {}
    files: dict[str, tuple[None, str]] | None = None
    if as_bool(site.get("mteam_sign"), False):
        timestamp = str(int(time.time() * 1000))
        params["_timestamp"] = timestamp
        params["_sgin"] = make_mteam_signature(check_url, method, timestamp)
        session.headers.setdefault("Origin", "https://kp.m-team.cc")
        session.headers.setdefault("Referer", "https://kp.m-team.cc/")
        session.headers.setdefault("version", str(site.get("api_version") or "1.1.4"))
        session.headers.setdefault("webVersion", str(site.get("web_version") or "1140"))
        session.headers.setdefault("ts", str(int(time.time())))
        session.headers.setdefault("visitorId", str(site.get("visitor_id") or ""))
        if method == "POST":
            files = {key: (None, value) for key, value in params.items()}
            params = {}
    try:
        if method == "POST":
            resp = session.post(check_url, data=params if files is None else None, files=files, timeout=timeout, allow_redirects=True)
        else:
            resp = session.get(check_url, params=params, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        return CheckResult("error", f"请求失败：{exc.__class__.__name__}: {exc}")

    text = resp.text or ""
    stats = extract_site_stats(text)
    final_url = resp.url
    if resp.status_code >= 400:
        return CheckResult("error", f"HTTP {resp.status_code}", resp.status_code, final_url, stats or None)

    failure_keywords = split_lines(site.get("failure_keywords") or DEFAULT_FAILURE_KEYWORDS)
    success_keywords = split_lines(site.get("success_keywords") or DEFAULT_SUCCESS_KEYWORDS)
    lowered_text = text.lower()
    lowered_url = final_url.lower()

    for keyword in failure_keywords:
        lowered = keyword.lower()
        if lowered and (lowered in lowered_text or lowered in lowered_url):
            return CheckResult("logged_out", f"命中失效关键词：{keyword}", resp.status_code, final_url, stats or None)

    if success_keywords:
        for keyword in success_keywords:
            if keyword and keyword.lower() in lowered_text:
                return CheckResult("ok", f"登录有效，命中关键词：{keyword}", resp.status_code, final_url, stats or None)
        return CheckResult("unknown", "未命中成功关键词，需要调整检测关键词", resp.status_code, final_url, stats or None)

    return CheckResult("ok", "HTTP 正常，未配置成功关键词", resp.status_code, final_url, stats or None)


def update_site_after_check(site: dict[str, Any], result: CheckResult) -> None:
    current = now_ts()
    site.setdefault("created_at", current)
    site["last_checked"] = current
    site["last_status"] = result.status
    site["last_message"] = result.message
    site["last_http_status"] = result.http_status
    site["last_final_url"] = result.final_url
    if result.stats:
        site["last_stats"] = result.stats
        site["last_stats_at"] = current
    if result.status == "ok":
        site["last_success"] = current


def site_due(site: dict[str, Any], current: int) -> bool:
    if not site.get("enabled", True):
        return False
    last_checked = as_int(site.get("last_checked"), 0)
    interval_seconds = max(as_int(site.get("interval_hours"), 600), 1) * 3600
    return current - last_checked >= interval_seconds


def days_since(ts: Any) -> float | None:
    value = as_int(ts, 0)
    if not value:
        return None
    return max((now_ts() - value) / 86400, 0)


def notify_stats_report_if_needed(data: dict[str, Any], site: dict[str, Any], result: CheckResult) -> None:
    if result.status != "ok" or not result.stats:
        return
    if not as_bool(site.get("stats_report_enabled"), True):
        return
    interval_seconds = max(as_int(site.get("stats_report_interval_days"), 25), 1) * 86400
    current = now_ts()
    last_report_at = as_int(site.get("last_stats_report_at"), 0)
    if last_report_at and current - last_report_at < interval_seconds:
        return

    settings = data.get("settings", {})
    title = f"[{APP_NAME}] {site.get('name')} 上传下载数据"
    body = build_stats_report_body(site, result.stats)
    send_notifications(settings, title, body)
    site["last_stats_report_at"] = current


def notify_manual_login_if_needed(data: dict[str, Any], site: dict[str, Any], result: CheckResult) -> None:
    if result.status != "ok":
        return
    if not as_bool(site.get("manual_login_reminder_enabled"), True):
        return
    reminder_days = max(as_int(site.get("manual_login_reminder_days"), 30), 1)
    last_manual_login_at = as_int(site.get("last_manual_login_at"), 0)
    if not last_manual_login_at:
        last_manual_login_at = as_int(site.get("created_at"), 0) or as_int(site.get("last_success"), 0)
    if not last_manual_login_at:
        return
    current = now_ts()
    if current - last_manual_login_at < reminder_days * 86400:
        return
    cooldown = max(as_int(data.get("settings", {}).get("notify_cooldown_hours"), 12), 1) * 3600
    last_at = as_int(site.get("last_manual_login_notify_at"), 0)
    if current - last_at < cooldown:
        return

    settings = data.get("settings", {})
    title = f"[{APP_NAME}] {site.get('name')} 需要手动网页登录"
    body = (
        f"站点：{site.get('name')}\n"
        f"距离上次记录的手动网页登录已经约 {days_since(last_manual_login_at) or 0:.1f} 天。\n"
        "请用浏览器打开站点并手动登录/刷新一次，完成后回到 PT Login Keeper 点“已手动登录”。\n"
        "说明：容器检测只能确认当前凭据是否可用，不能保证等同于站点要求的真实网页登录。"
    )
    send_notifications(settings, title, body)
    site["last_manual_login_notify_at"] = current


def notify_if_needed(data: dict[str, Any], site: dict[str, Any], result: CheckResult) -> None:
    notify_stats_report_if_needed(data, site, result)
    notify_manual_login_if_needed(data, site, result)

    settings = data.get("settings", {})
    cooldown = max(as_int(settings.get("notify_cooldown_hours"), 12), 1) * 3600
    warn_days = max(as_int(settings.get("warn_days"), 7), 0)
    expire_days = max(as_int(site.get("expire_days"), 30), 1)

    title = ""
    body = ""
    key = ""
    if result.status in {"logged_out", "missing_auth", "missing_cookie", "error", "unknown"}:
        title = f"[{APP_NAME}] {site.get('name')} 登录状态异常"
        body = f"站点：{site.get('name')}\n状态：{result.status}\n原因：{result.message}\n检测地址：{site.get('check_url') or site.get('url')}"
        key = f"bad:{result.status}:{result.message}"
    elif result.status == "ok":
        elapsed = days_since(site.get("last_success"))
        if elapsed is not None and expire_days - elapsed <= warn_days:
            left = max(expire_days - elapsed, 0)
            title = f"[{APP_NAME}] {site.get('name')} 接近保号期限"
            body = f"站点：{site.get('name')}\n距离 {expire_days} 天期限约剩 {left:.1f} 天。\n建议手动确认一次登录状态。"
            key = "warn-expire"

    if not title:
        return

    current = now_ts()
    last_key = str(site.get("last_notify_key") or "")
    last_at = as_int(site.get("last_notify_at"), 0)
    if key == last_key and current - last_at < cooldown:
        return

    send_notifications(settings, title, body)
    site["last_notify_key"] = key
    site["last_notify_at"] = current


def send_notifications(settings: dict[str, Any], title: str, body: str) -> None:
    webhook = str(settings.get("webhook_url") or os.getenv("NOTIFY_WEBHOOK_URL") or "").strip()
    wecom_robot = str(settings.get("wecom_robot_webhook") or os.getenv("WECOM_ROBOT_WEBHOOK") or "").strip()
    wecom_app_corpid = str(settings.get("wecom_app_corpid") or os.getenv("WECOM_APP_CORPID") or "").strip()
    wecom_app_agentid = str(settings.get("wecom_app_agentid") or os.getenv("WECOM_APP_AGENTID") or "").strip()
    wecom_app_secret = str(settings.get("wecom_app_secret") or os.getenv("WECOM_APP_SECRET") or "").strip()
    wecom_app_touser = str(settings.get("wecom_app_touser") or os.getenv("WECOM_APP_TOUSER") or "@all").strip() or "@all"
    sendkey = str(settings.get("serverchan_sendkey") or os.getenv("SERVERCHAN_SENDKEY") or "").strip()
    pushplus_token = str(settings.get("pushplus_token") or os.getenv("PUSHPLUS_TOKEN") or "").strip()

    if webhook:
        try:
            requests.post(webhook, json={"title": title, "text": body}, timeout=15)
        except Exception as exc:
            logging.warning("Webhook notify failed: %s", exc)

    if wecom_robot:
        try:
            resp = requests.post(
                wecom_robot,
                json={"msgtype": "text", "text": {"content": f"{title}\n\n{body}"}},
                timeout=15,
            )
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("errcode") not in (None, 0)):
                logging.warning("WeCom robot notify failed: status=%s body=%s", resp.status_code, resp.text[:300])
        except Exception as exc:
            logging.warning("WeCom robot notify failed: %s", exc)

    if wecom_app_corpid and wecom_app_agentid and wecom_app_secret:
        try:
            token_resp = requests.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": wecom_app_corpid, "corpsecret": wecom_app_secret},
                timeout=15,
            )
            token_payload = token_resp.json()
            access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else ""
            if not access_token:
                logging.warning("WeCom app token failed: status=%s body=%s", token_resp.status_code, token_resp.text[:300])
            else:
                try:
                    agentid: int | str = int(wecom_app_agentid)
                except ValueError:
                    agentid = wecom_app_agentid
                resp = requests.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                    params={"access_token": access_token},
                    json={
                        "touser": wecom_app_touser,
                        "msgtype": "text",
                        "agentid": agentid,
                        "text": {"content": f"{title}\n\n{body}"},
                        "safe": 0,
                    },
                    timeout=15,
                )
                try:
                    payload = resp.json()
                except Exception:
                    payload = {}
                if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("errcode") not in (None, 0)):
                    logging.warning("WeCom app notify failed: status=%s body=%s", resp.status_code, resp.text[:300])
        except Exception as exc:
            logging.warning("WeCom app notify failed: %s", exc)

    if sendkey:
        try:
            requests.post(f"https://sctapi.ftqq.com/{sendkey}.send", data={"title": title, "desp": body}, timeout=15)
        except Exception as exc:
            logging.warning("ServerChan notify failed: %s", exc)

    if pushplus_token:
        try:
            requests.post(
                "https://www.pushplus.plus/send",
                json={"token": pushplus_token, "title": title, "content": body.replace("\n", "<br>"), "template": "html"},
                timeout=15,
            )
        except Exception as exc:
            logging.warning("PushPlus notify failed: %s", exc)


def run_check_for_site(site_id: str) -> CheckResult | None:
    with STATE_LOCK:
        data = load_config()
        site = get_site(data, site_id)
        if site is None:
            return None
        result = check_site(site)
        update_site_after_check(site, result)
        notify_if_needed(data, site, result)
        save_config(data)
        return result


def run_check_ids(site_ids: list[str]) -> None:
    for site_id in dict.fromkeys(x for x in site_ids if x):
        try:
            result = run_check_for_site(site_id)
            if result:
                logging.info("Checked site_id=%s status=%s message=%s", site_id, result.status, result.message)
        except Exception:
            logging.exception("Check failed site_id=%s", site_id)


def trigger_check_ids(site_ids: list[str]) -> None:
    thread = threading.Thread(target=run_check_ids, args=(site_ids,), name="manual-check", daemon=True)
    thread.start()


def scheduler_loop() -> None:
    logging.info("Scheduler started")
    while True:
        current = now_ts()
        check_ids: list[str] = []
        with STATE_LOCK:
            data = load_config()
            sites = data.get("sites", [])
            if RUN_ALL_EVENT.is_set():
                RUN_ALL_EVENT.clear()
                check_ids.extend([site.get("id") for site in sites if site.get("enabled", True)])
            if RUN_SITE_EVENTS:
                check_ids.extend(list(RUN_SITE_EVENTS))
                RUN_SITE_EVENTS.clear()
            for site in sites:
                if site_due(site, current):
                    check_ids.append(site.get("id"))

        run_check_ids(check_ids)

        time.sleep(max(as_int(os.getenv("CHECK_INTERVAL_SECONDS"), 300), 30))


def render_page(message: str = "") -> str:
    data = load_config()
    settings = data.get("settings", {})
    rows = "\n".join(render_site_row(site) for site in data.get("sites", []))
    if not rows:
        rows = "<tr><td colspan='8' class='muted'>还没有站点。先添加一个 PT 站。</td></tr>"
    message_html = f"<div class='message'>{esc(message)}</div>" if message else ""
    return page_shell(
        f"""
  <h1>{APP_NAME}</h1>
  <div class="sub">PT 站 Cookie 登录保活与失效提醒</div>
  {message_html}
  <section class="panel">
    <div class="row">
      <a class="button primary" href="/site">添加站点</a>
      <form method="post" action="/check-all"><button class="secondary" type="submit">立即检测全部</button></form>
    </div>
  </section>
  <section class="panel">
    <table>
      <thead>
        <tr>
          <th>站点</th><th>状态</th><th>上次成功</th><th>上次检测</th><th>距保号</th><th>间隔</th><th>消息</th><th>操作</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </section>
  <section class="panel">
    <h2>通知设置</h2>
    <form method="post" action="/settings">
      <div class="grid">
        <div>
          <label>提前提醒天数</label>
          <input name="warn_days" value="{esc(settings.get("warn_days", 7))}">
        </div>
        <div>
          <label>同类通知冷却小时</label>
          <input name="notify_cooldown_hours" value="{esc(settings.get("notify_cooldown_hours", 12))}">
        </div>
      </div>
      <label>通用 Webhook URL</label>
      <input name="webhook_url" value="{esc(settings.get("webhook_url", ""))}" placeholder="可选，POST JSON: title/text">
      <label>企业微信机器人 / 微信转发 Webhook</label>
      <input name="wecom_robot_webhook" value="{esc(settings.get("wecom_robot_webhook", ""))}" placeholder="可选，例如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=... 或你的转发地址">
      <div class="hint">如果 MoviePilot 已经能推送企业微信，把 MoviePilot 里同一个企业微信机器人 Webhook 复制到这里即可共用。此字段会发送企业微信机器人兼容格式。</div>
      <h3>企业微信应用通知</h3>
      <div class="hint">如果 MoviePilot 使用的是企业微信应用通知，而不是机器人 Webhook，可以把 MoviePilot 的 `WECHAT_CORPID`、`WECHAT_APP_ID`、`WECHAT_APP_SECRET` 填到这里。</div>
      <div class="grid">
        <div>
          <label>企业 ID / CorpID</label>
          <input name="wecom_app_corpid" value="{esc(settings.get("wecom_app_corpid", ""))}" placeholder="WECHAT_CORPID">
        </div>
        <div>
          <label>应用 AgentId</label>
          <input name="wecom_app_agentid" value="{esc(settings.get("wecom_app_agentid", ""))}" placeholder="WECHAT_APP_ID">
        </div>
      </div>
      <label>应用 Secret</label>
      <input type="password" name="wecom_app_secret" value="{esc(settings.get("wecom_app_secret", ""))}" placeholder="WECHAT_APP_SECRET">
      <label>接收用户</label>
      <input name="wecom_app_touser" value="{esc(settings.get("wecom_app_touser", "@all"))}" placeholder="@all">
      <label>Server 酱 SendKey</label>
      <input name="serverchan_sendkey" value="{esc(settings.get("serverchan_sendkey", ""))}" placeholder="可选">
      <label>PushPlus Token</label>
      <input name="pushplus_token" value="{esc(settings.get("pushplus_token", ""))}" placeholder="可选">
      <div class="row submit-row"><button class="primary" type="submit">保存通知设置</button></div>
    </form>
    <form method="post" action="/test-notify" class="test-form">
      <button class="secondary" type="submit">发送测试通知</button>
    </form>
  </section>
""",
        "首页",
    )


def render_site_row(site: dict[str, Any]) -> str:
    status = str(site.get("last_status") or "never")
    status_class = {
        "ok": "ok",
        "never": "neutral",
        "unknown": "warn",
        "logged_out": "bad",
        "missing_auth": "bad",
        "missing_cookie": "bad",
        "error": "bad",
        "bad_config": "bad",
    }.get(status, "neutral")
    elapsed = days_since(site.get("last_success"))
    expire_days = max(as_int(site.get("expire_days"), 30), 1)
    if elapsed is None:
        left = "-"
    else:
        left = f"{max(expire_days - elapsed, 0):.1f} 天"
    stats = site.get("last_stats") if isinstance(site.get("last_stats"), dict) else {}
    stats_bits = []
    if stats.get("uploaded"):
        stats_bits.append(f"上传 {stats.get('uploaded')}")
    if stats.get("downloaded"):
        stats_bits.append(f"下载 {stats.get('downloaded')}")
    if stats.get("share_rate"):
        stats_bits.append(f"分享率 {stats.get('share_rate')}")
    stats_text = " / ".join(stats_bits)
    message_parts = [str(site.get("last_message", ""))]
    if stats_text:
        message_parts.append(f"数据：{stats_text}")
    if site.get("last_stats_at"):
        message_parts.append(f"数据时间：{format_time(site.get('last_stats_at'))}")
    message_text = "\n".join(part for part in message_parts if part)
    return f"""
      <tr>
        <td><b>{esc(site.get("name"))}</b><div class="muted">{esc(site.get("check_url") or site.get("url"))}</div></td>
        <td><span class="badge {status_class}">{esc(status)}</span></td>
        <td>{esc(format_time(site.get("last_success")))}</td>
        <td>{esc(format_time(site.get("last_checked")))}</td>
        <td>{esc(left)}</td>
        <td>{esc(site.get("interval_hours", 600))} 小时</td>
        <td class="message-cell">{esc(message_text)}</td>
        <td>
          <div class="actions">
            <form method="post" action="/check"><input type="hidden" name="id" value="{esc(site.get("id"))}"><button class="small" type="submit">检测</button></form>
            <form method="post" action="/mark-manual-login"><input type="hidden" name="id" value="{esc(site.get("id"))}"><button class="small" type="submit">已手动登录</button></form>
            <a class="small link-button" href="/site?id={esc(site.get("id"))}">编辑</a>
            <form method="post" action="/delete" onsubmit="return confirm('确认删除？')"><input type="hidden" name="id" value="{esc(site.get("id"))}"><button class="small danger" type="submit">删除</button></form>
          </div>
        </td>
      </tr>"""


def render_site_form(site: dict[str, Any] | None = None, message: str = "") -> str:
    site = site or {
        "enabled": True,
        "check_method": "GET",
        "interval_hours": 600,
        "expire_days": 30,
        "stats_report_enabled": True,
        "stats_report_interval_days": 25,
        "manual_login_reminder_enabled": True,
        "manual_login_reminder_days": 30,
        "success_keywords": DEFAULT_SUCCESS_KEYWORDS,
        "failure_keywords": DEFAULT_FAILURE_KEYWORDS,
    }
    checked = "checked" if site.get("enabled", True) else ""
    mteam_checked = "checked" if as_bool(site.get("mteam_sign"), False) else ""
    stats_report_checked = "checked" if as_bool(site.get("stats_report_enabled"), True) else ""
    manual_login_reminder_checked = "checked" if as_bool(site.get("manual_login_reminder_enabled"), True) else ""
    get_selected = "selected" if str(site.get("check_method") or "GET").upper() == "GET" else ""
    post_selected = "selected" if str(site.get("check_method") or "").upper() == "POST" else ""
    message_html = f"<div class='message'>{esc(message)}</div>" if message else ""
    return page_shell(
        f"""
  <h1>{'编辑站点' if site.get('id') else '添加站点'}</h1>
  <div class="sub">只保存 Cookie，不保存 PT 账号密码。</div>
  {message_html}
  <section class="panel">
    <form method="post" action="/site">
      <input type="hidden" name="id" value="{esc(site.get("id", ""))}">
      <label class="inline"><input type="checkbox" name="enabled" value="1" {checked}> 启用</label>
      <div class="grid">
        <div>
          <label>站点名称</label>
          <input name="name" required value="{esc(site.get("name", ""))}" placeholder="例如：某 PT">
        </div>
        <div>
          <label>站点首页</label>
          <input name="url" required value="{esc(site.get("url", ""))}" placeholder="https://example.com/">
        </div>
      </div>
      <label>检测地址</label>
      <input name="check_url" value="{esc(site.get("check_url", ""))}" placeholder="建议填用户中心/控制面板地址；留空则使用首页">
      <div class="grid">
        <div>
          <label>检测方法</label>
          <select name="check_method">
            <option value="GET" {get_selected}>GET</option>
            <option value="POST" {post_selected}>POST</option>
          </select>
        </div>
        <div>
          <label>M-Team API 签名</label>
          <input type="hidden" name="mteam_sign_present" value="1">
          <label class="inline option-line"><input type="checkbox" name="mteam_sign" value="1" {mteam_checked}> 启用 M-Team `_sgin` 签名</label>
        </div>
      </div>
      <label>请求头或 cURL</label>
      <textarea name="request_headers" placeholder="可直接粘贴 Copy as cURL，或粘贴 Request Headers；保存时会自动提取 Cookie / Authorization / User-Agent / did / visitorId">{esc(site.get("request_headers", ""))}</textarea>
      <label>Cookie</label>
      <textarea name="cookie" placeholder="可手动填写 Cookie；如果上面粘贴了 cURL/请求头，这里可留空自动提取">{esc(site.get("cookie", ""))}</textarea>
      <label>Authorization</label>
      <textarea name="authorization" placeholder="可选，例如 Bearer xxx；如果请求头里有 authorization，会自动提取">{esc(site.get("authorization", ""))}</textarea>
      <div class="grid">
        <div>
          <label>did</label>
          <input name="did" value="{esc(site.get("did", ""))}" placeholder="M-Team API 可选，从请求头自动提取">
        </div>
        <div>
          <label>visitorId</label>
          <input name="visitor_id" value="{esc(site.get("visitor_id", ""))}" placeholder="M-Team API 可选，从请求头自动提取">
        </div>
      </div>
      <div class="grid">
        <div>
          <label>API version</label>
          <input name="api_version" value="{esc(site.get("api_version", ""))}" placeholder="M-Team 默认 1.1.4">
        </div>
        <div>
          <label>Web version</label>
          <input name="web_version" value="{esc(site.get("web_version", ""))}" placeholder="M-Team 默认 1140">
        </div>
      </div>
      <div class="grid">
        <div>
          <label>User-Agent</label>
          <input name="user_agent" value="{esc(site.get("user_agent", ""))}" placeholder="可选，建议从请求头自动提取">
        </div>
        <div>
          <label>Referer</label>
          <input name="referer" value="{esc(site.get("referer", ""))}" placeholder="可选">
        </div>
      </div>
      <label>Accept-Language</label>
      <input name="accept_language" value="{esc(site.get("accept_language", ""))}" placeholder="可选，默认 zh-CN,zh;q=0.9,en;q=0.7">
      <div class="grid">
        <div>
          <label>检测间隔小时</label>
          <input name="interval_hours" value="{esc(site.get("interval_hours", 600))}">
          <div class="hint">25 天 = 600 小时。每到间隔会自动检测一次登录状态。</div>
        </div>
        <div>
          <label>保号天数</label>
          <input name="expire_days" value="{esc(site.get("expire_days", 30))}">
        </div>
      </div>
      <div class="grid">
        <div>
          <label>上传下载数据通知</label>
          <input type="hidden" name="stats_report_enabled_present" value="1">
          <label class="inline option-line"><input type="checkbox" name="stats_report_enabled" value="1" {stats_report_checked}> 检测成功后定期发送上传量、下载量、分享率</label>
        </div>
        <div>
          <label>数据通知间隔天数</label>
          <input name="stats_report_interval_days" value="{esc(site.get("stats_report_interval_days", 25))}">
        </div>
      </div>
      <div class="grid">
        <div>
          <label>手动网页登录提醒</label>
          <input type="hidden" name="manual_login_reminder_enabled_present" value="1">
          <label class="inline option-line"><input type="checkbox" name="manual_login_reminder_enabled" value="1" {manual_login_reminder_checked}> 定期提醒你用浏览器手动登录一次</label>
        </div>
        <div>
          <label>手动登录提醒天数</label>
          <input name="manual_login_reminder_days" value="{esc(site.get("manual_login_reminder_days", 30))}">
        </div>
      </div>
      <div class="hint">推荐：检测间隔 600 小时，数据通知间隔 25 天，手动网页登录提醒 30 天。完成手动网页登录后，在首页点该站点的“已手动登录”。</div>
      <label>成功关键词</label>
      <textarea name="success_keywords">{esc(site.get("success_keywords") or DEFAULT_SUCCESS_KEYWORDS)}</textarea>
      <div class="hint">检测页面包含任意成功关键词即认为仍处于登录状态。建议用“退出、用户中心、上传量、魔力”等登录后才出现的词。</div>
      <label>失效关键词</label>
      <textarea name="failure_keywords">{esc(site.get("failure_keywords") or DEFAULT_FAILURE_KEYWORDS)}</textarea>
      <div class="hint">检测页面包含任意失效关键词即认为 Cookie 失效。不同站点可按页面调整。</div>
      <div class="row submit-row">
        <button class="primary" type="submit">保存站点</button>
        <a class="button secondary" href="/">返回</a>
      </div>
    </form>
  </section>
""",
        "站点",
    )


def page_shell(content: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(APP_NAME)} - {esc(title)}</title>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f6f7f8; color:#202124; }}
    main {{ max-width:1120px; margin:0 auto; padding:28px 18px 48px; }}
    h1 {{ margin:0 0 6px; font-size:30px; }}
    h2 {{ margin:0 0 14px; font-size:18px; }}
    h3 {{ margin:18px 0 8px; font-size:16px; }}
    .sub,.muted,.hint {{ color:#6b7280; }}
    .hint {{ font-size:13px; line-height:1.5; margin-top:6px; }}
    .panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:18px; margin:16px 0; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
    .submit-row {{ margin-top:18px; }}
    .test-form {{ margin-top:10px; }}
    label {{ display:block; font-weight:650; margin:16px 0 8px; }}
    label.inline {{ display:flex; gap:8px; align-items:center; margin-top:0; }}
    .option-line {{ min-height:40px; }}
    input,textarea,select {{ width:100%; box-sizing:border-box; border:1px solid #d1d5db; border-radius:7px; padding:10px 12px; font-size:15px; background:#fff; }}
    textarea {{ min-height:120px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.5; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ text-align:left; border-bottom:1px solid #e5e7eb; padding:10px; vertical-align:top; }}
    th {{ color:#6b7280; font-size:13px; }}
    button,.button {{ display:inline-block; text-decoration:none; border:0; border-radius:7px; padding:10px 14px; font-size:14px; font-weight:650; cursor:pointer; }}
    .primary {{ background:#0969da; color:white; }}
    .secondary {{ background:#e5e7eb; color:#111827; }}
    .danger {{ background:#fee2e2; color:#991b1b; }}
    .small,.link-button {{ padding:6px 8px; font-size:13px; background:#e5e7eb; color:#111827; border-radius:6px; border:0; text-decoration:none; cursor:pointer; }}
    .actions {{ display:flex; gap:6px; flex-wrap:wrap; }}
    .badge {{ display:inline-block; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:700; }}
    .ok {{ background:#dcfce7; color:#166534; }}
    .warn {{ background:#fef3c7; color:#92400e; }}
    .bad {{ background:#fee2e2; color:#991b1b; }}
    .neutral {{ background:#e5e7eb; color:#374151; }}
    .message {{ background:#ecfdf3; border:1px solid #bbf7d0; color:#166534; padding:10px 12px; border-radius:7px; margin:14px 0; }}
    .message-cell {{ max-width:260px; word-break:break-word; white-space:pre-line; }}
    @media (max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} table {{ font-size:13px; }} }}
  </style>
</head>
<body><main>{content}</main></body>
</html>"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("web %s", fmt % args)

    def _authorized(self) -> bool:
        user = os.getenv("WEB_USER", "")
        password = os.getenv("WEB_PASSWORD", "")
        if not user and not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        return decoded == f"{user}:{password}"

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PT Login Keeper"')
        self.end_headers()
        return False

    def _send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _read_form(self) -> dict[str, list[str]]:
        length = as_int(self.headers.get("Content-Length"), 0)
        raw = self.rfile.read(length).decode("utf-8")
        return parse_qs(raw)

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_html("ok")
            return
        if parsed.path == "/site":
            query = parse_qs(parsed.query)
            site_id = form_value(query, "id")
            site = get_site(load_config(), site_id) if site_id else None
            self._send_html(render_site_form(site))
            return
        message = ""
        if "saved=1" in parsed.query:
            message = "已保存"
        if "checked=1" in parsed.query:
            message = "已请求检测"
        if "deleted=1" in parsed.query:
            message = "已删除"
        if "notify_test=1" in parsed.query:
            message = "已发送测试通知"
        if "manual_login_marked=1" in parsed.query:
            message = "已记录手动网页登录时间"
        self._send_html(render_page(message))

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        form = self._read_form()
        if self.path == "/settings":
            with STATE_LOCK:
                data = load_config()
                settings = data.setdefault("settings", {})
                settings["warn_days"] = max(as_int(form_value(form, "warn_days"), 7), 0)
                settings["notify_cooldown_hours"] = max(as_int(form_value(form, "notify_cooldown_hours"), 12), 1)
                settings["webhook_url"] = form_value(form, "webhook_url").strip()
                settings["wecom_robot_webhook"] = form_value(form, "wecom_robot_webhook").strip()
                settings["wecom_app_corpid"] = form_value(form, "wecom_app_corpid").strip()
                settings["wecom_app_agentid"] = form_value(form, "wecom_app_agentid").strip()
                settings["wecom_app_secret"] = form_value(form, "wecom_app_secret").strip()
                settings["wecom_app_touser"] = form_value(form, "wecom_app_touser").strip() or "@all"
                settings["serverchan_sendkey"] = form_value(form, "serverchan_sendkey").strip()
                settings["pushplus_token"] = form_value(form, "pushplus_token").strip()
                save_config(data)
            self._redirect("/?saved=1")
            return
        if self.path == "/test-notify":
            settings = load_config().get("settings", {})
            send_notifications(
                settings,
                f"[{APP_NAME}] 测试通知",
                "如果你收到这条消息，说明 PT Login Keeper 的微信通知配置正常。",
            )
            self._redirect("/?notify_test=1")
            return
        if self.path == "/site":
            with STATE_LOCK:
                data = load_config()
                site_id = form_value(form, "id")
                existing = get_site(data, site_id) if site_id else None
                site = normalize_site(form, existing)
                if not site["name"] or not site["url"]:
                    self._send_html(render_site_form(site, "保存失败：站点名称和首页不能为空"), status=400)
                    return
                sites = data.setdefault("sites", [])
                if existing:
                    for idx, old in enumerate(sites):
                        if old.get("id") == existing.get("id"):
                            sites[idx] = site
                            break
                else:
                    sites.append(site)
                save_config(data)
            self._redirect("/?saved=1")
            return
        if self.path == "/mark-manual-login":
            site_id = form_value(form, "id")
            with STATE_LOCK:
                data = load_config()
                site = get_site(data, site_id)
                if site is not None:
                    current = now_ts()
                    site["last_manual_login_at"] = current
                    site["last_manual_login_notify_at"] = 0
                    save_config(data)
            self._redirect("/?manual_login_marked=1")
            return
        if self.path == "/delete":
            site_id = form_value(form, "id")
            with STATE_LOCK:
                data = load_config()
                data["sites"] = [site for site in data.get("sites", []) if site.get("id") != site_id]
                save_config(data)
            self._redirect("/?deleted=1")
            return
        if self.path == "/check":
            site_id = form_value(form, "id")
            if site_id:
                trigger_check_ids([site_id])
            self._redirect("/?checked=1")
            return
        if self.path == "/check-all":
            with STATE_LOCK:
                data = load_config()
                site_ids = [site.get("id") for site in data.get("sites", []) if site.get("enabled", True)]
            trigger_check_ids(site_ids)
            self._redirect("/?checked=1")
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_dir()
    thread = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
    thread.start()
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = as_int(os.getenv("APP_PORT"), 9199)
    logging.info("%s listening on %s:%s", APP_NAME, host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
