#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
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
    interval_hours = max(as_int(form_value(form, "interval_hours"), 168), 1)
    expire_days = max(as_int(form_value(form, "expire_days"), 30), 1)
    return {
        **existing,
        "id": site_id,
        "enabled": "enabled" in form,
        "name": form_value(form, "name").strip(),
        "url": form_value(form, "url").strip(),
        "check_url": form_value(form, "check_url").strip(),
        "cookie": form_value(form, "cookie").strip(),
        "success_keywords": form_value(form, "success_keywords").strip(),
        "failure_keywords": form_value(form, "failure_keywords").strip(),
        "interval_hours": interval_hours,
        "expire_days": expire_days,
    }


def form_value(form: dict[str, list[str]], name: str, default: str = "") -> str:
    values = form.get(name)
    if not values:
        return default
    return values[0]


def check_site(site: dict[str, Any]) -> CheckResult:
    cookie = str(site.get("cookie") or "").strip()
    if not cookie:
        return CheckResult("missing_cookie", "未填写 Cookie")

    check_url = str(site.get("check_url") or site.get("url") or "").strip()
    if not check_url:
        return CheckResult("bad_config", "未填写检测地址")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.getenv(
                "USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cookie": cookie,
        }
    )

    timeout = as_int(os.getenv("REQUEST_TIMEOUT"), 30)
    try:
        resp = session.get(check_url, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        return CheckResult("error", f"请求失败：{exc.__class__.__name__}: {exc}")

    text = resp.text or ""
    final_url = resp.url
    if resp.status_code >= 400:
        return CheckResult("error", f"HTTP {resp.status_code}", resp.status_code, final_url)

    failure_keywords = split_lines(site.get("failure_keywords") or DEFAULT_FAILURE_KEYWORDS)
    success_keywords = split_lines(site.get("success_keywords") or DEFAULT_SUCCESS_KEYWORDS)
    lowered_text = text.lower()
    lowered_url = final_url.lower()

    for keyword in failure_keywords:
        lowered = keyword.lower()
        if lowered and (lowered in lowered_text or lowered in lowered_url):
            return CheckResult("logged_out", f"命中失效关键词：{keyword}", resp.status_code, final_url)

    if success_keywords:
        for keyword in success_keywords:
            if keyword and keyword.lower() in lowered_text:
                return CheckResult("ok", f"登录有效，命中关键词：{keyword}", resp.status_code, final_url)
        return CheckResult("unknown", "未命中成功关键词，需要调整检测关键词", resp.status_code, final_url)

    return CheckResult("ok", "HTTP 正常，未配置成功关键词", resp.status_code, final_url)


def update_site_after_check(site: dict[str, Any], result: CheckResult) -> None:
    current = now_ts()
    site["last_checked"] = current
    site["last_status"] = result.status
    site["last_message"] = result.message
    site["last_http_status"] = result.http_status
    site["last_final_url"] = result.final_url
    if result.status == "ok":
        site["last_success"] = current


def site_due(site: dict[str, Any], current: int) -> bool:
    if not site.get("enabled", True):
        return False
    last_checked = as_int(site.get("last_checked"), 0)
    interval_seconds = max(as_int(site.get("interval_hours"), 168), 1) * 3600
    return current - last_checked >= interval_seconds


def days_since(ts: Any) -> float | None:
    value = as_int(ts, 0)
    if not value:
        return None
    return max((now_ts() - value) / 86400, 0)


def notify_if_needed(data: dict[str, Any], site: dict[str, Any], result: CheckResult) -> None:
    settings = data.get("settings", {})
    cooldown = max(as_int(settings.get("notify_cooldown_hours"), 12), 1) * 3600
    warn_days = max(as_int(settings.get("warn_days"), 7), 0)
    expire_days = max(as_int(site.get("expire_days"), 30), 1)

    title = ""
    body = ""
    key = ""
    if result.status in {"logged_out", "missing_cookie", "error", "unknown"}:
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
    sendkey = str(settings.get("serverchan_sendkey") or os.getenv("SERVERCHAN_SENDKEY") or "").strip()
    pushplus_token = str(settings.get("pushplus_token") or os.getenv("PUSHPLUS_TOKEN") or "").strip()

    if webhook:
        try:
            requests.post(webhook, json={"title": title, "text": body}, timeout=15)
        except Exception as exc:
            logging.warning("Webhook notify failed: %s", exc)

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

        for site_id in dict.fromkeys(x for x in check_ids if x):
            try:
                result = run_check_for_site(site_id)
                if result:
                    logging.info("Checked site_id=%s status=%s message=%s", site_id, result.status, result.message)
            except Exception:
                logging.exception("Check failed site_id=%s", site_id)

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
      <label>Server 酱 SendKey</label>
      <input name="serverchan_sendkey" value="{esc(settings.get("serverchan_sendkey", ""))}" placeholder="可选">
      <label>PushPlus Token</label>
      <input name="pushplus_token" value="{esc(settings.get("pushplus_token", ""))}" placeholder="可选">
      <div class="row submit-row"><button class="primary" type="submit">保存通知设置</button></div>
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
    return f"""
      <tr>
        <td><b>{esc(site.get("name"))}</b><div class="muted">{esc(site.get("check_url") or site.get("url"))}</div></td>
        <td><span class="badge {status_class}">{esc(status)}</span></td>
        <td>{esc(format_time(site.get("last_success")))}</td>
        <td>{esc(format_time(site.get("last_checked")))}</td>
        <td>{esc(left)}</td>
        <td>{esc(site.get("interval_hours", 168))} 小时</td>
        <td class="message-cell">{esc(site.get("last_message", ""))}</td>
        <td>
          <div class="actions">
            <form method="post" action="/check"><input type="hidden" name="id" value="{esc(site.get("id"))}"><button class="small" type="submit">检测</button></form>
            <a class="small link-button" href="/site?id={esc(site.get("id"))}">编辑</a>
            <form method="post" action="/delete" onsubmit="return confirm('确认删除？')"><input type="hidden" name="id" value="{esc(site.get("id"))}"><button class="small danger" type="submit">删除</button></form>
          </div>
        </td>
      </tr>"""


def render_site_form(site: dict[str, Any] | None = None, message: str = "") -> str:
    site = site or {
        "enabled": True,
        "interval_hours": 168,
        "expire_days": 30,
        "success_keywords": DEFAULT_SUCCESS_KEYWORDS,
        "failure_keywords": DEFAULT_FAILURE_KEYWORDS,
    }
    checked = "checked" if site.get("enabled", True) else ""
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
      <label>Cookie</label>
      <textarea name="cookie" required placeholder="从浏览器开发者工具复制该 PT 站 Cookie 请求头">{esc(site.get("cookie", ""))}</textarea>
      <div class="grid">
        <div>
          <label>检测间隔小时</label>
          <input name="interval_hours" value="{esc(site.get("interval_hours", 168))}">
        </div>
        <div>
          <label>保号天数</label>
          <input name="expire_days" value="{esc(site.get("expire_days", 30))}">
        </div>
      </div>
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
    .sub,.muted,.hint {{ color:#6b7280; }}
    .hint {{ font-size:13px; line-height:1.5; margin-top:6px; }}
    .panel {{ background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:18px; margin:16px 0; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
    .submit-row {{ margin-top:18px; }}
    label {{ display:block; font-weight:650; margin:16px 0 8px; }}
    label.inline {{ display:flex; gap:8px; align-items:center; margin-top:0; }}
    input,textarea {{ width:100%; box-sizing:border-box; border:1px solid #d1d5db; border-radius:7px; padding:10px 12px; font-size:15px; background:#fff; }}
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
    .message-cell {{ max-width:260px; word-break:break-word; }}
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
                settings["serverchan_sendkey"] = form_value(form, "serverchan_sendkey").strip()
                settings["pushplus_token"] = form_value(form, "pushplus_token").strip()
                save_config(data)
            self._redirect("/?saved=1")
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
                RUN_SITE_EVENTS.add(site_id)
            self._redirect("/?checked=1")
            return
        if self.path == "/check-all":
            RUN_ALL_EVENT.set()
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
