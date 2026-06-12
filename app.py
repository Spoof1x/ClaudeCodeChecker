#!/usr/bin/env python3
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import random
import secrets
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


BASE = "https://claude.ai/api"
HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(HERE, "sessions")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_PROXY = os.environ.get("CLAUDE_PROXY", "").strip()


def _load_proxy_file():
    path = os.path.join(SESSIONS_DIR, "proxy.txt")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError:
            return ""
    return ""


if not _PROXY:
    _PROXY = _load_proxy_file()


def _opener(proxy=None):
    proxy = proxy or _PROXY
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


def _c(text, color):
    if not sys.stdout.isatty():
        return str(text)
    return f"{color}{text}{C.RESET}"


def _cookies_from_jar(data):
    cookies = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "name" in item and "value" in item:
                cookies[str(item["name"])] = str(item["value"])
    elif isinstance(data, dict):
        if "name" in data and "value" in data:
            cookies[str(data["name"])] = str(data["value"])
        else:
            for key, value in data.items():
                if isinstance(value, str):
                    cookies[str(key)] = value
    return cookies


def _build_session(text, proxy=None):
    text = text.strip()
    if not text:
        return None

    parsed = None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if parsed is not None:
        cookies = _cookies_from_jar(parsed)
        if "sessionKey" in cookies:
            header = "; ".join(f"{name}={value}" for name, value in cookies.items())
            return {
                "key": cookies["sessionKey"],
                "cookie": header,
                "org": cookies.get("lastActiveOrg"),
                "proxy": proxy,
                "cookies": cookies,
            }

    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return {"key": line, "cookie": f"sessionKey={line}", "org": None, "proxy": proxy, "cookies": {}}

    return None


def _open_json(req, proxy=None, timeout=30):
    proxy = proxy or _PROXY
    if proxy:
        return _curl_json(req, proxy, timeout)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, enc, status = exc.read(), exc.headers.get("Content-Encoding"), exc.code
    except urllib.error.URLError as exc:
        return None, f"Ошибка соединения: {exc.reason}"
    if enc == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return status, _try_json(raw.decode("utf-8", errors="replace"))


def _curl_json(req, proxy, timeout):
    cmd = ["curl", "-x", proxy, "-s", "--compressed", "--max-time", str(timeout),
           "-X", req.get_method(), "-w", "\n__CCSTATUS__%{http_code}", req.full_url]
    hdrs = {}
    hdrs.update(req.headers)
    hdrs.update(getattr(req, "unredirected_hdrs", {}))
    for k, v in hdrs.items():
        cmd += ["-H", f"{k}: {v}"]
    data = req.data
    if data is not None:
        cmd += ["--data-binary", "@-"]
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 6)
    except subprocess.TimeoutExpired:
        return None, "Ошибка соединения: timed out"
    except Exception as exc:
        return None, f"Ошибка соединения: {exc}"
    out = r.stdout.decode("utf-8", errors="replace")
    idx = out.rfind("__CCSTATUS__")
    status = None
    if idx >= 0:
        try:
            status = int(out[idx + len("__CCSTATUS__"):].strip()) or None
        except ValueError:
            status = None
    if r.returncode != 0 or status is None:
        reason = CURL_ERR.get(r.returncode, f"curl {r.returncode}") if r.returncode else "нет ответа"
        return None, f"Ошибка соединения: {reason}"
    return status, _try_json(out[:idx].rstrip("\n"))


def api_request(method, path, session, payload=None, extra_headers=None):
    url = path if path.startswith("http") else f"{BASE}{path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
        "anthropic-client-platform": "web_claude_ai",
    }
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v is not None})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    req.add_unredirected_header("Cookie", session["cookie"])
    return _open_json(req, session.get("proxy"), timeout=10)


def api_get(path, session):
    return api_request("GET", path, session)


CLIENT_VERSION = "1.0.0"


def mutation_headers(session):
    h = {"anthropic-client-version": CLIENT_VERSION}
    cookies = session.get("cookies") or {}
    dev = cookies.get("anthropic-device-id")
    anon = cookies.get("ajs_anonymous_id")
    if dev:
        h["anthropic-device-id"] = dev
    if anon:
        h["anthropic-anonymous-id"] = anon
    return h


CC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CC_SCOPE = "user:inference"
CC_REDIRECT = "http://localhost:45289/callback"
CC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _code_from_url(u):
    try:
        return (urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("code") or [None])[0]
    except Exception:
        return None


def _oauth_exchange(payload, proxy=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(CC_TOKEN_URL, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "claude-cli/2.1.170 (external, cli)",
    })
    return _open_json(req, proxy)


def _extract_oauth_code(data):
    if isinstance(data, dict):
        if data.get("code"):
            return data["code"]
        for k in ("redirect_uri", "redirect_url", "location", "url"):
            v = data.get(k)
            if isinstance(v, str) and "code=" in v:
                return _code_from_url(v)
    if isinstance(data, str) and "code=" in data:
        return _code_from_url(data)
    return None


def parse_authorize_url(url):
    url = (url or "").strip()
    if not url:
        return {}
    qs = url.split("?", 1)[1] if "?" in url else url
    qs = qs.split("#", 1)[0]
    try:
        return {k: v[0] for k, v in urllib.parse.parse_qs(qs).items() if v}
    except Exception:
        return {}


def _snip(data):
    return (json.dumps(data, ensure_ascii=False)[:200]
            if isinstance(data, (dict, list)) else str(data)[:200])


def _authorize_post(session, org, body):
    st, data = api_request("POST", f"https://claude.ai/v1/oauth/{org}/authorize",
                           session, payload=body, extra_headers=mutation_headers(session))
    return st, data, _extract_oauth_code(data)


def claude_code_authorize(session, org, q):
    if not org:
        return {"ok": False, "error": "Не удалось определить организацию"}
    if not all(q.get(k) for k in ("client_id", "code_challenge", "state", "redirect_uri")):
        return {"ok": False, "error": "В ссылке нет нужных параметров (client_id/code_challenge/state/redirect_uri)"}
    body = {
        "response_type": q.get("response_type") or "code",
        "client_id": q["client_id"], "organization_uuid": org,
        "redirect_uri": q["redirect_uri"], "scope": q.get("scope") or CC_SCOPE,
        "state": q["state"], "code_challenge": q["code_challenge"],
        "code_challenge_method": q.get("code_challenge_method") or "S256",
    }
    st, data, code = _authorize_post(session, org, body)
    if not code:
        return {"ok": False, "error": f"Авторизация HTTP {st}: код не получен. {_snip(data)}".strip()}
    return {"ok": True, "code": code, "state": q["state"]}


def claude_code_token(session, org):
    if not org:
        return {"ok": False, "error": "Не удалось определить организацию"}
    verifier, challenge = _pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode()
    body = {
        "response_type": "code", "client_id": CC_CLIENT_ID,
        "organization_uuid": org, "redirect_uri": CC_REDIRECT,
        "scope": CC_SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    st, data, code = _authorize_post(session, org, body)
    if not code:
        return {"ok": False,
                "error": f"Авторизация HTTP {st}: код не получен (нужен Arkose/браузер). {_snip(data)}".strip()}
    ex = {
        "grant_type": "authorization_code", "code": code, "state": state,
        "client_id": CC_CLIENT_ID, "redirect_uri": CC_REDIRECT, "code_verifier": verifier,
    }
    st2, data2 = _oauth_exchange(ex, session.get("proxy"))
    for delay in (4, 8):
        if st2 != 429:
            break
        time.sleep(delay)
        st2, data2 = _oauth_exchange(ex, session.get("proxy"))
    if isinstance(data2, dict) and data2.get("access_token"):
        return {"ok": True, "token": data2["access_token"],
                "refresh": data2.get("refresh_token"), "expires_in": data2.get("expires_in")}
    if st2 == 429:
        return {"ok": False, "code_ok": True,
                "error": "Обмен: слишком много запросов (429). Подождите пару минут или смените аккаунт."}
    return {"ok": False, "code_ok": True,
            "error": f"Код получен, но обмен не прошёл: HTTP {st2} {str(data2)[:200]}"}


def _api_err(st, data):
    if isinstance(data, dict):
        for k in ("message", "error", "detail"):
            v = data.get(k)
            if isinstance(v, str):
                return v
        return json.dumps(data, ensure_ascii=False)[:200]
    return f"HTTP {st}: {str(data)[:200]}"


STRIPE_PK = ("pk_live_51MExQ9BjIQrRQnuxA9s9ahUkfIUHPoc3NFNidarWIUhEpwuc1bdj"
             "SJU9medEpVjoP4kTUrV2G8QWdxi9GjRJMUri005KO5xdyD")


def _stripe_pi(client_secret, proxy=None):
    pi = client_secret.split("_secret_")[0]
    q = urllib.parse.urlencode({"client_secret": client_secret, "key": STRIPE_PK})
    req = urllib.request.Request(
        f"https://api.stripe.com/v1/payment_intents/{pi}?{q}",
        headers={"Accept": "application/json", "Origin": "https://js.stripe.com",
                 "Referer": "https://js.stripe.com/", "User-Agent": USER_AGENT})
    return _open_json(req, proxy, timeout=20)[1]


def _confirm_pi(cs, proxy):
    status, msg = None, None
    for _ in range(5):
        pi = _stripe_pi(cs, proxy)
        if isinstance(pi, dict):
            status = pi.get("status")
            msg = (pi.get("last_payment_error") or {}).get("message")
            if status in ("succeeded", "requires_payment_method", "requires_action", "canceled"):
                break
        time.sleep(2)
    if status == "succeeded":
        return {"ok": True, "status": status}
    if status == "requires_action":
        return {"ok": False, "status": status, "error": "нужно 3DS-подтверждение"}
    if status == "requires_payment_method":
        return {"ok": False, "status": status, "error": msg or "карта отклонена"}
    return {"ok": False, "status": status or "pending", "error": msg or "статус не подтверждён"}


def _finish_upgrade(st, data, session):
    if not (st and st < 400 and isinstance(data, dict) and data.get("clientSecret")):
        return {"ok": False, "error": _api_err(st, data)}
    return _confirm_pi(data["clientSecret"], session.get("proxy"))


def claude_upgrade_max(session, org, tier):
    if not org:
        return {"ok": False, "error": "Не удалось определить организацию"}
    st, data = api_request("PUT", f"/organizations/{org}/upgrade_to_max",
                           session, payload={"max_tier": tier},
                           extra_headers=mutation_headers(session))
    return _finish_upgrade(st, data, session)


def claude_upgrade_pro(session, org):
    if not org:
        return {"ok": False, "error": "Не удалось определить организацию"}
    st, data = api_request("PUT", f"/organizations/{org}/upgrade_to_pro",
                           session, payload={}, extra_headers=mutation_headers(session))
    if st in (404, 405):
        return {"ok": False, "error": "Апгрейд до Pro пока недоступен (нет рабочего эндпоинта)"}
    return _finish_upgrade(st, data, session)


TOPUP_BUNDLES = {
    50: {"amount": 5000, "bundle_id": "bundle_50", "expected": 4500},
    250: {"amount": 25000, "bundle_id": "bundle_250", "expected": 20000},
    1000: {"amount": 100000, "bundle_id": "bundle_1000", "expected": 70000},
}


def claude_buy_credits(session, org, denom):
    if not org:
        return {"ok": False, "error": "Не удалось определить организацию"}
    try:
        b = TOPUP_BUNDLES.get(int(denom))
    except (TypeError, ValueError):
        b = None
    if not b:
        return {"ok": False, "error": f"Нет данных бандла для {denom} (нужен curl этого номинала)"}
    payload = {"amount": b["amount"], "bundle_id": b["bundle_id"],
               "expected_price_minor_units": b["expected"],
               "redirect_url": "https://claude.ai/", "payment_method_type": "card"}
    st, data = api_request("POST", f"/organizations/{org}/contracts/prepaid/credits",
                           session, payload=payload, extra_headers=mutation_headers(session))
    if not (st and st < 400 and isinstance(data, dict)):
        return {"ok": False, "error": _api_err(st, data)}
    pid = data.get("purchase_id")
    TERMINAL = ("failed", "succeeded", "complete", "completed", "paid")
    status = data.get("payment_status")
    if pid and status not in TERMINAL:
        for _ in range(4):
            time.sleep(2)
            s2, d2 = api_request("GET", f"/organizations/{org}/prepaid/commits/{pid}", session)
            if isinstance(d2, dict):
                status = d2.get("status") or d2.get("payment_status") or status
                if status in TERMINAL:
                    break
    ok = status in ("succeeded", "complete", "completed", "paid")
    res = {"ok": ok, "purchase_id": pid, "status": status}
    if not ok:
        res["error"] = (f"платёж не прошёл: {status}" if status
                        else "платёж не подтверждён (таймаут)")
    return res


def _try_json(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def ensure_ok(status, data, what):
    if status in (401, 403):
        print(_c(f"✗ {what}: доступ запрещён ({status}). Кука истекла/неверна.", C.RED))
        return False
    if status is None:
        print(_c(f"✗ {what}: {data}", C.RED))
        return False
    if status >= 400:
        print(_c(f"✗ {what}: HTTP {status}", C.RED))
        if isinstance(data, (dict, list)):
            print(_c(json.dumps(data, ensure_ascii=False)[:300], C.DIM))
        return False
    return True


PLAN_LABELS = (
    ("claude_max", "Max"),
    ("claude_pro", "Pro"),
    ("claude_team", "Team"),
    ("raven", "Enterprise"),
)


def plan_from_caps(caps):
    caps = caps or []
    for key, label in PLAN_LABELS:
        if key in caps:
            return label
    if "chat" in caps:
        return "Free"
    return None


def fetch_organizations(session):
    status, data = api_get("/organizations", session)
    if not ensure_ok(status, data, "Организации"):
        return None
    return data if isinstance(data, list) else None


def pick_chat_org(orgs, session):
    if not orgs:
        return None
    pool = [o for o in orgs if "chat" in (o.get("capabilities") or [])] or orgs
    hint = session.get("org")
    if hint:
        for org in pool:
            if org.get("uuid") == hint:
                return org
    return pool[0]


def fetch_usage(session, org_uuid):
    status, data = api_get(f"/organizations/{org_uuid}/usage", session)
    if status and status < 400 and isinstance(data, dict):
        return data
    return None


DATA_FILE = os.path.join(HERE, "data.json")
VAULT_INDEX = os.path.join(HERE, "sessions.json")
PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

STATIC = {
    "/": ("index.html", "text/html"),
}


def read_file(name):
    try:
        with open(os.path.join(HERE, name), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        print(_c(f"⚠ Не удалось сохранить {os.path.basename(path)}: {exc}", C.YELLOW))
        return False


def _read_json(path, empty):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return type(empty)()
    return d if isinstance(d, type(empty)) else type(empty)()


def _read_data():
    return _read_json(DATA_FILE, {})


def _write_data(d):
    _write_json(DATA_FILE, d)


def read_history():
    return _read_data().get("history") or {}


def write_history(hist):
    d = _read_data()
    d["history"] = hist
    _write_data(d)


_DATA_LOCK = threading.Lock()
_CANCEL_LOCK = threading.Lock()
_CANCELLED = set()


def upsert_history(data):
    uuid = (data.get("account") or {}).get("uuid")
    if not uuid:
        return
    with _DATA_LOCK:
        hist = read_history()
        hist[uuid] = {"ts": time.time(), "data": data}
        write_history(hist)


def read_vault():
    return _read_json(VAULT_INDEX, [])


def write_vault(items):
    _write_json(VAULT_INDEX, items)


def _vault_rows(items):
    return [{k: v for k, v in it.items() if k != "jar"} for it in items]


def _gen_session_id(existing):
    while True:
        sid = ("".join(random.choice(string.ascii_letters) for _ in range(4))
               + "".join(random.choice(string.digits) for _ in range(4)))
        if sid not in existing:
            return sid


def _decode_routing_hint(rh):
    if not rh:
        return {}
    token = rh.split("sk-ant-rh-")[-1] if "sk-ant-rh-" in rh else rh
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except Exception:
        return {}


def cookie_quick_info(jar):
    cookies = _cookies_from_jar(jar)
    key = cookies.get("sessionKey")
    if not key:
        return None
    rh = _decode_routing_hint(cookies.get("routingHint", ""))
    expires = None
    if isinstance(jar, list):
        for it in jar:
            if isinstance(it, dict) and it.get("name") == "sessionKey":
                expires = it.get("expirationDate")
                break
    return {
        "name": rh.get("name"),
        "account_uuid": rh.get("sub"),
        "org_uuid": cookies.get("lastActiveOrg"),
        "phone_verified": rh.get("phone_verified"),
        "locale": rh.get("locale"),
        "key_tail": key[-6:],
        "expires": expires,
    }


def _clean_name(fname):
    if not fname:
        return None
    base = os.path.splitext(os.path.basename(str(fname)))[0].strip()
    return base or None


def _jar_ident(d):
    uid = d.get("account_uuid")
    if uid:
        return ("uuid", uid)
    kt = d.get("key_tail")
    return ("tail", kt) if kt else None


def add_cookie_jar(jar, fallback_name=None):
    info = cookie_quick_info(jar)
    if not info:
        return None
    items = read_vault()
    ident = _jar_ident(info)
    if ident:
        for it in items:
            if _jar_ident(it) == ident:
                it["jar"] = jar
                it["key_tail"] = info.get("key_tail")
                it["expires"] = info.get("expires")
                if info.get("org_uuid"):
                    it["org_uuid"] = info["org_uuid"]
                it["status"] = "unchecked"
                it["checked_at"] = None
                write_vault(items)
                return "updated"
    sid = _gen_session_id({it["id"] for it in items})
    name = (_clean_name(fallback_name) or info.get("name")
            or (info.get("account_uuid") or "")[:8] or info.get("key_tail"))
    entry = {"id": sid, "jar": jar, "status": "unchecked",
             "added_at": time.time(), "checked_at": None,
             "email": None, "plan": None, **info, "name": name}
    items.append(entry)
    write_vault(items)
    return "added"


def read_proxies():
    return _read_data().get("proxies") or []


def write_proxies(items):
    d = _read_data()
    d["proxies"] = items
    _write_data(d)


def parse_proxy(line):
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    scheme = "http"
    if "://" in line:
        scheme, line = line.split("://", 1)
        scheme = scheme.strip().lower()
    user = pwd = None
    if "@" in line:
        cred, hostport = line.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
        else:
            user = cred
        parts = hostport.split(":")
    else:
        parts = line.split(":")
        if len(parts) == 4:
            parts, user, pwd = parts[:2], parts[2], parts[3]
    if len(parts) < 2:
        return None
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port.isdigit():
        return None
    if scheme not in PROXY_SCHEMES:
        scheme = "http"
    auth = ""
    if user is not None:
        auth = user + ((":" + pwd) if pwd is not None else "") + "@"
    return {"url": f"{scheme}://{auth}{host}:{port}", "scheme": scheme,
            "host": host, "port": int(port), "user": user}


def _pwd_of(d):
    rest = (d.get("url") or "").split("://", 1)[-1]
    if "@" not in rest:
        return None
    cred = rest.rsplit("@", 1)[0]
    return cred.split(":", 1)[1] if ":" in cred else None


def _pkey(d, pwd):
    return (d.get("scheme"), (d.get("host") or "").strip().rstrip(".").lower(),
            d.get("port"), d.get("user") or None, pwd)


CURL_ERR = {
    5: "PROXY DNS FAIL", 6: "DNS FAIL", 7: "CONNECTION REFUSED",
    28: "TIMEOUT", 35: "SSL ERROR", 52: "EMPTY REPLY", 56: "RECV ERROR",
    97: "SOCKS FAIL",
}
HTTP_TEXT = {200: "OK", 403: "Forbidden", 407: "Proxy Auth Required",
             401: "Unauthorized", 429: "Too Many Requests", 502: "Bad Gateway",
             503: "Service Unavailable"}


def check_proxy(url, timeout=12):
    try:
        r = subprocess.run(
            ["curl", "-x", url, "-s", "-o", "/dev/null",
             "-w", "%{http_code} %{time_total}", "--max-time", str(timeout),
             "https://claude.ai/"],
            capture_output=True, text=True, timeout=timeout + 6,
        )
        parts = r.stdout.strip().split()
        code = int(parts[0]) if parts and parts[0].isdigit() else 0
        latency = int(float(parts[1]) * 1000) if len(parts) > 1 else None
        if code:
            result = f"HTTP {code}" + (" " + HTTP_TEXT[code] if code in HTTP_TEXT else "")
            return True, code, latency, result
        return False, 0, None, CURL_ERR.get(r.returncode, f"FAILED (curl {r.returncode})")
    except subprocess.TimeoutExpired:
        return False, 0, None, "TIMEOUT"
    except Exception:
        return False, 0, None, "ERROR"


def clamp_threads(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(100, n))


def _mask_proxy(url):
    if not url:
        return None
    u = str(url)
    if "://" in u:
        u = u.split("://", 1)[1]
    if "@" in u:
        u = u.split("@", 1)[1]
    return u or None


def to_netscape(jar):
    lines = ["# Netscape HTTP Cookie File", ""]
    if isinstance(jar, list):
        for c in jar:
            if not isinstance(c, dict) or c.get("name") is None:
                continue
            domain = str(c.get("domain") or ".claude.ai")
            inc_sub = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if c.get("secure") else "FALSE"
            try:
                expiry = int(c.get("expirationDate") or 0)
            except (TypeError, ValueError):
                expiry = 0
            lines.append("\t".join([domain, inc_sub, str(c.get("path") or "/"),
                                    secure, str(expiry), str(c.get("name")), str(c.get("value", ""))]))
    return "\n".join(lines) + "\n"


def _safe_name(s, fallback):
    base = "".join(ch for ch in str(s or "") if ch.isalnum() or ch in "-_ .").strip()
    return base or fallback


def _uniq(used, name):
    out, root, dot = name, name, ""
    if "." in name:
        root, dot = name.rsplit(".", 1)
        dot = "." + dot
    i = 2
    while out in used:
        out = f"{root} ({i}){dot}"
        i += 1
    used.add(out)
    return out


def check_proxies(subset, threads=20):
    def one(p):
        valid, code, lat, result = check_proxy(p["url"])
        p["status"] = "valid" if valid else "invalid"
        p["code"], p["latency"], p["checked_at"], p["result"] = code, lat, time.time(), result
    if not subset:
        return
    with ThreadPoolExecutor(max_workers=clamp_threads(threads)) as ex:
        list(ex.map(one, subset))


def _parallel(tasks):
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, min(10, len(tasks)))) as ex:
        futures = {ex.submit(fn): key for key, fn in tasks.items()}
        for fut, key in futures.items():
            try:
                out[key] = fut.result()
            except Exception:
                out[key] = None
    return out


def fetch_all_sessions(session):
    out, offset, limit = [], 0, 50
    for _ in range(40):
        st, d = api_get(
            f"/auth/sessions/list-active?limit={limit}&offset={offset}", session
        )
        if not (st and st < 400 and isinstance(d, dict)):
            break
        out.extend(d.get("data") or [])
        pg = d.get("pagination") or {}
        if not pg.get("has_more"):
            break
        offset += pg.get("limit") or limit
    return out


def fetch_all_tokens(session, ou):
    out, seen, offset, limit = [], set(), 0, 1000
    for _ in range(25):
        st, d = api_get(
            f"/oauth/organizations/{ou}/oauth_tokens?limit={limit}&offset={offset}", session
        )
        if not (st and st < 400):
            break
        batch = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else None)
        if not batch:
            break
        new = [t for t in batch if t.get("id") not in seen]
        if not new:
            break
        for t in new:
            seen.add(t.get("id"))
        out.extend(new)
        if len(batch) < limit:
            break
        offset += limit
    return out


def _boot_err(st, boot):
    if st and st >= 400:
        txt = HTTP_TEXT.get(st)
        return f"HTTP {st}" + (f" {txt}" if txt else "")
    if not st:
        s = str(boot).lower()
        if "tim" in s:
            return "TIMEOUT"
        if "refused" in s or "отказ" in s:
            return "отказ соединения"
        return "нет соединения"
    return "нет данных"


def _build_report(session, level, should_stop=None):
    if should_stop and should_stop():
        return None
    st, boot = api_get("/bootstrap", session)
    ok_boot = bool(st and st < 400 and isinstance(boot, dict))
    check_err = None if ok_boot else _boot_err(st, boot)
    boot = boot if ok_boot else {}
    account = boot.get("account") or {}
    orgs = [m.get("organization") or {} for m in (account.get("memberships") or []) if isinstance(m, dict)]
    org = pick_chat_org(orgs, session) or (orgs[0] if orgs else {})
    ou = org.get("uuid")
    others = [o for o in orgs if o.get("uuid") != ou]

    def get(path):
        s, data = api_get(path, session)
        return data if (s and s < 400) else None

    tasks = {}
    if ou and level != "mini":
        tasks.update({
            "usage": partial(fetch_usage, session, ou),
            "subscription": partial(get, f"/organizations/{ou}/subscription_details"),
            "balance": partial(get, f"/stripe/{ou}/balance"),
            "overage": partial(get, f"/organizations/{ou}/overage_spend_limit"),
            "payment_method": partial(get, f"/organizations/{ou}/payment_method"),
        })
        if level == "full":
            tasks.update({
                "sessions": partial(fetch_all_sessions, session),
                "tokens": partial(fetch_all_tokens, session, ou),
                "invoices": partial(get, f"/stripe/{ou}/invoices"),
                "credits": partial(get, f"/organizations/{ou}/prepaid/credits"),
            })
    if should_stop and should_stop():
        return None
    r = _parallel(tasks) if tasks else {}

    return {
        "account": {
            "full_name": account.get("full_name"),
            "display_name": account.get("display_name"),
            "email": account.get("email_address") or account.get("email"),
            "uuid": account.get("uuid"),
            "phone_verified": bool(account.get("verified_phone_number_last4")),
            "created_at": account.get("created_at"),
        },
        "org": {
            "uuid": ou,
            "name": org.get("name"),
            "plan": plan_from_caps(org.get("capabilities")),
            "billing_type": org.get("billing_type"),
            "tier": org.get("raw_rate_limit_tier") or org.get("rate_limit_tier"),
            "capabilities": org.get("capabilities") or [],
            "created_at": org.get("created_at"),
        },
        "others": [
            {"name": o.get("name"), "capabilities": o.get("capabilities") or [],
             "disabled": o.get("api_disabled_reason")}
            for o in others
        ],
        "usage": r.get("usage"),
        "subscription": r.get("subscription"),
        "balance": r.get("balance"),
        "credits": r.get("credits"),
        "overage": r.get("overage"),
        "invoices": r.get("invoices"),
        "sessions": r.get("sessions"),
        "tokens": r.get("tokens"),
        "payment_method": r.get("payment_method"),
        "mini": level == "mini",
        "check_err": check_err,
        "boot_status": st,
    }


def mini_report(session, should_stop=None):
    return _build_report(session, "mini", should_stop)


def quick_report(session, should_stop=None):
    return _build_report(session, "quick", should_stop)


def full_report(session):
    return _build_report(session, "full")


def _session_from_vault(item, proxy):
    jar = item.get("jar")
    if not jar:
        return None
    return _build_session(json.dumps(jar), proxy=proxy)


def read_groups():
    return _read_data().get("groups") or []


def write_groups(groups):
    d = _read_data()
    d["groups"] = groups
    _write_data(d)


def _update_group(gid, mutate):
    with _DATA_LOCK:
        groups = read_groups()
        for g in groups:
            if g.get("id") == gid:
                mutate(g)
                break
        write_groups(groups)


def _update_vault_status(vid, ok, info, proxy):
    info = info or {}
    with _DATA_LOCK:
        items = read_vault()
        for it in items:
            if it.get("id") == vid:
                it["status"] = "valid" if ok else "invalid"
                it["checked_at"] = time.time()
                it["proxy"] = _mask_proxy(proxy)
                if ok:
                    it["mini"] = bool(info.get("mini"))
                    for k in ("uuid", "email", "plan", "tier", "balance", "bal_cur", "used",
                              "limit", "currency", "auto_renew", "has_payment", "renew"):
                        if info.get(k) is not None:
                            it[k] = info[k]
                    if it["mini"]:
                        for k in ("balance", "bal_cur", "used", "limit", "currency",
                                  "auto_renew", "has_payment", "renew"):
                            it[k] = None
                break
        write_vault(items)


def _lim_obj(x):
    if isinstance(x, dict) and x.get("utilization") is not None:
        return {"u": x.get("utilization"), "r": x.get("resets_at")}
    return None


def _extract_info(data):
    acc = data.get("account") or {}
    org = data.get("org") or {}
    ov = data.get("overage") or {}
    bal = data.get("balance") or {}
    u = data.get("usage") or {}
    sub = data.get("subscription") or {}
    pm = data.get("payment_method")
    status = str(sub.get("status") or "").lower()
    cancelled = bool(sub.get("cancel_at_period_end") or sub.get("canceled_at")
                     or sub.get("cancelled_at") or sub.get("ended_at"))
    return {
        "uuid": acc.get("uuid"), "email": acc.get("email"),
        "plan": org.get("plan"), "tier": org.get("tier"),
        "used": ov.get("used_credits"), "limit": ov.get("monthly_credit_limit"),
        "currency": ov.get("currency") or "USD",
        "balance": bal.get("balance"), "bal_cur": bal.get("currency"),
        "auto_renew": bool(sub) and status in ("active", "trialing") and not cancelled,
        "has_payment": bool(pm) or bool(sub.get("payment_method")),
        "renew": sub.get("next_charge_at") or sub.get("next_charge_date"),
        "lim5h": _lim_obj(u.get("five_hour")),
        "limweek": _lim_obj(u.get("seven_day")),
        "limsonnet": _lim_obj(u.get("seven_day_sonnet")),
    }


def _inc_proxy_uses(urls):
    urls = [u for u in (urls or []) if u]
    if not urls:
        return
    with _DATA_LOCK:
        items = read_proxies()
        for p in items:
            n = urls.count(p.get("url"))
            if n:
                p["uses"] = (p.get("uses") or 0) + n
        write_proxies(items)


def pick_least_used_proxy():
    proxies = [p for p in read_proxies() if p.get("status") == "valid" and p.get("enabled")]
    if not proxies:
        return None
    mn = min((p.get("uses") or 0) for p in proxies)
    cand = [p for p in proxies if (p.get("uses") or 0) == mn]
    return random.choice(cand)["url"]


def _result_skeleton(item, will_check, proxy):
    return {
        "vault_id": item.get("id"),
        "name": item.get("name") or item.get("key_tail"),
        "key_tail": item.get("key_tail"),
        "status": "pending" if will_check else "skipped",
        "proxy": _mask_proxy(proxy),
        "proxy_url": proxy,
        "uuid": None, "email": None, "plan": None, "tier": None,
        "used": None, "limit": None, "currency": "USD", "checked_at": None,
        "balance": None, "bal_cur": None,
        "auto_renew": None, "has_payment": None, "renew": None,
        "lim5h": None, "limweek": None, "limsonnet": None,
        "mini": False, "err": None,
    }


def start_group(vault_ids, threads, proxy_mode, fast=False):
    vault = read_vault()
    by_id = {it.get("id"): it for it in vault}
    seen = set()
    vault_ids = [i for i in vault_ids if not (i in seen or seen.add(i))]
    selected = [by_id[i] for i in vault_ids if i in by_id]
    selected = [it for it in selected if it.get("jar")]
    if not selected:
        return None
    proxies = [p for p in read_proxies() if p.get("status") == "valid" and p.get("enabled")]
    if proxy_mode != "none" and not proxies:
        return None

    assignments = []
    if proxy_mode == "one2one":
        n = min(len(selected), len(proxies))
        for i, item in enumerate(selected):
            assignments.append((item, proxies[i]["url"], True) if i < n else (item, None, False))
    elif proxy_mode == "none":
        assignments = [(item, None, True) for item in selected]
    else:
        assignments = [(item, (random.choice(proxies)["url"] if proxies else None), True)
                       for item in selected]

    gid = _uuid.uuid4().hex[:10]
    group = {
        "id": gid, "created_at": time.time(), "status": "running",
        "proxy_mode": proxy_mode,
        "total": sum(1 for _, _, w in assignments if w),
        "results": [_result_skeleton(item, will, px) for item, px, will in assignments],
    }
    with _DATA_LOCK:
        groups = read_groups()
        groups.append(group)
        write_groups(groups)

    to_check = [(item, px) for item, px, will in assignments if will]
    _inc_proxy_uses([px for _, px, w in assignments if w and px])
    threading.Thread(target=_run_group, args=(gid, to_check, clamp_threads(threads), bool(fast)),
                     daemon=True).start()
    return gid


def _group_cancelled(gid):
    with _CANCEL_LOCK:
        return gid in _CANCELLED


def _run_group(gid, to_check, threads, fast=False):
    def one(pair):
        item, proxy = pair

        def skip(g):
            for r in g["results"]:
                if r["vault_id"] == item.get("id") and r["status"] == "pending":
                    r["status"] = "skipped"
                    break

        if _group_cancelled(gid):
            _update_group(gid, skip)
            return
        session = _session_from_vault(item, proxy)
        sc = lambda: _group_cancelled(gid)
        data = None
        if session:
            try:
                data = mini_report(session, sc) if fast else quick_report(session, sc)
            except Exception:
                data = None
        if _group_cancelled(gid):
            _update_group(gid, skip)
            return
        acc = (data or {}).get("account") or {}
        ok = bool(data and acc.get("uuid"))
        info = {}
        transport = False
        if ok:
            data["ok"] = True
            data["proxy"] = proxy
            upsert_history(data)
            info = _extract_info(data)
            info["mini"] = bool(data.get("mini"))
            if info["mini"]:
                info["auto_renew"] = None
                info["has_payment"] = None
        else:
            if not session:
                info["err"] = "нет cookie"
            elif data is None:
                info["err"] = "ошибка проверки"
                transport = True
            else:
                info["err"] = data.get("check_err") or "нет аккаунта"
                transport = data.get("boot_status") is None
        row_status = "error" if transport else ("valid" if ok else "invalid")

        def mut(g):
            for r in g["results"]:
                if r["vault_id"] == item.get("id"):
                    r["status"] = row_status
                    r["checked_at"] = time.time()
                    r.update(info)
                    break

        _update_group(gid, mut)
        if not transport:
            _update_vault_status(item.get("id"), ok, info, proxy)

    if to_check:
        with ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(one, to_check))

    def _finalize(g):
        unfinished = any(r.get("status") in ("skipped", "pending") for r in (g.get("results") or []))
        g["status"] = "stopped" if unfinished else "done"
    _update_group(gid, _finalize)
    with _CANCEL_LOCK:
        _CANCELLED.discard(gid)


def _session_from_uuid(uuid):
    if not uuid:
        return None
    item = next((it for it in read_vault() if it.get("uuid") == uuid), None)
    if not item:
        return None
    entry = read_history().get(uuid) or {}
    proxy = (entry.get("data") or {}).get("proxy")
    return _session_from_vault(item, proxy)


def action_session(body):
    return _session_from_uuid(body.get("uuid"))


def _strict_session(uuid):
    return _session_from_uuid(uuid)


def org_for(session):
    orgs = fetch_organizations(session) or []
    return (pick_chat_org(orgs, session) or {}).get("uuid")


def _org_uuid_for(uuid):
    return ((read_history().get(uuid) or {}).get("data") or {}).get("org", {}).get("uuid")


def _resolve_currency(uuid):
    d = (read_history().get(uuid) or {}).get("data") or {}
    ov = d.get("overage") or {}
    cr = d.get("credits") or {}
    sub = d.get("subscription") or {}
    return (ov.get("currency") or cr.get("currency") or sub.get("currency") or "USD").upper()


def _has_payment_for(uuid):
    d = (read_history().get(uuid) or {}).get("data") or {}
    return bool(d.get("payment_method")) or bool((d.get("subscription") or {}).get("payment_method"))


def _update_group_rows_by_uuid(uuid, mutate):
    if not uuid:
        return
    with _DATA_LOCK:
        groups = read_groups()
        changed = False
        for g in groups:
            for r in (g.get("results") or []):
                if r.get("uuid") == uuid:
                    mutate(r)
                    changed = True
        if changed:
            write_groups(groups)


def _mark_invalid_by_uuid(uuid):
    if not uuid:
        return
    now = time.time()
    with _DATA_LOCK:
        items = read_vault()
        changed = False
        for it in items:
            if it.get("uuid") == uuid:
                it["status"] = "invalid"
                it["checked_at"] = now
                changed = True
        if changed:
            write_vault(items)

    def _mut(r):
        r["status"] = "invalid"
        r["checked_at"] = now
    _update_group_rows_by_uuid(uuid, _mut)


def _err(st, data):
    if st in (401, 403):
        return "Куки протухли или нет доступа"
    if st is None:
        return str(data)
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False)[:300]
    return str(data)[:300]


def revoke_token(session, token_id):
    ou = org_for(session)
    if not (ou and token_id):
        return False
    st, _ = api_request(
        "POST", f"/oauth/organizations/{ou}/oauth_tokens/{token_id}/revoke",
        session, payload={},
    )
    return bool(st and st < 400)


def logout_session(session, created_at, slug):
    if not (created_at and slug):
        return False
    st, _ = api_request(
        "POST", "/auth/logout/session", session,
        payload={"created_at": created_at, "application_slug": slug},
    )
    return bool(st and st < 400)


def logout_all_sessions(session):
    count = 0
    for s in fetch_all_sessions(session):
        if s.get("is_current"):
            continue
        if logout_session(session, s.get("created_at"), s.get("application_slug")):
            count += 1
    return count


def revoke_all_tokens(session):
    ou = org_for(session)
    if not ou:
        return 0
    st, data = api_get(f"/oauth/organizations/{ou}/oauth_tokens", session)
    if not (st and st < 400 and isinstance(data, list)):
        return 0
    count = 0
    for t in data:
        if t.get("is_revoked"):
            continue
        if revoke_token(session, t.get("id")):
            count += 1
    return count


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if getattr(self, "_sent", False):
            return
        self._sent = True
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass

    def _send_bytes(self, code, data, ctype, filename=None):
        if getattr(self, "_sent", False):
            return
        self._sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        self._sent = False
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            content = read_file(name)
            if content is None:
                self._send(404, f"{name} не найден", "text/plain")
            else:
                self._send(200, content, ctype)
        elif path == "/api/account":
            uuid = (parse_qs(urlparse(self.path).query).get("uuid") or [""])[0]
            entry = read_history().get(uuid)
            if entry:
                self._send(200, {"ok": True, "ts": entry.get("ts"),
                                 "data": entry.get("data")})
            else:
                self._send(200, {"ok": False})
        elif path == "/api/account/full":
            uuid = (parse_qs(urlparse(self.path).query).get("uuid") or [""])[0]
            item = next((it for it in read_vault() if it.get("uuid") == uuid), None)
            if item:
                entry = read_history().get(uuid) or {}
                proxy = (entry.get("data") or {}).get("proxy")
                session = _session_from_vault(item, proxy)
                if session:
                    try:
                        data = full_report(session)
                    except Exception:
                        data = None
                    if data and (data.get("account") or {}).get("uuid"):
                        data["proxy"] = proxy
                        finfo = _extract_info(data)
                        finfo["mini"] = False
                        upsert_history(data)
                        _update_vault_status(item.get("id"), True, finfo, proxy)
                        now = time.time()

                        def _gmut(r):
                            r["status"] = "valid"
                            r["checked_at"] = now
                            r.update(finfo)
                        _update_group_rows_by_uuid(uuid, _gmut)
                        self._send(200, {"ok": True, "ts": now, "data": data})
                        return
            entry = read_history().get(uuid)
            if entry:
                self._send(200, {"ok": True, "ts": entry.get("ts"),
                                 "data": entry.get("data")})
            else:
                self._send(200, {"ok": False})
        elif path == "/api/proxies":
            self._send(200, {"ok": True, "items": read_proxies()})
        elif path == "/api/groups":
            self._send(200, {"ok": True, "items": read_groups()})
        elif path == "/api/vault":
            out = []
            for it in read_vault():
                row = {k: v for k, v in it.items() if k != "jar"}
                if not it.get("jar"):
                    row["status"] = "nd"
                    row["missing"] = True
                out.append(row)
            self._send(200, {"ok": True, "items": out})
        elif path == "/favicon.ico":
            self._send(204, "", "text/plain")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        self._sent = False
        try:
            self.path = self.path.split("?", 1)[0]
            if self.path == "/api/groups/start":
                body = self._body()
                gid = start_group(body.get("vault_ids") or [],
                                  clamp_threads(body.get("threads")),
                                  body.get("proxy_mode") or "random",
                                  bool(body.get("fast")))
                self._send(200, {"ok": bool(gid), "group_id": gid})
            elif self.path == "/api/groups/stop":
                gid = self._body().get("id")
                running = any(g.get("id") == gid and g.get("status") == "running"
                              for g in read_groups())
                if running:
                    with _CANCEL_LOCK:
                        _CANCELLED.add(gid)
                self._send(200, {"ok": running})
            elif self.path == "/api/groups/delete":
                gid = self._body().get("id")
                with _DATA_LOCK:
                    write_groups([g for g in read_groups() if g.get("id") != gid])
                self._send(200, {"ok": True})
            elif self.path == "/api/export":
                body = self._body()
                ids = body.get("vault_ids") or []
                fmt = "json" if body.get("format") == "json" else "netscape"
                vault = {it.get("id"): it for it in read_vault()}
                used, buf = set(), io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for vid in ids:
                        it = vault.get(vid)
                        if not it:
                            continue
                        jar = it.get("jar")
                        if not jar:
                            continue
                        base = _safe_name(it.get("name"), str(vid))
                        if fmt == "json":
                            z.writestr(_uniq(used, base + ".json"),
                                       json.dumps(jar, ensure_ascii=False, indent=2))
                        else:
                            z.writestr(_uniq(used, base + ".txt"), to_netscape(jar))
                self._send_bytes(200, buf.getvalue(), "application/zip", f"cookies_{fmt}.zip")
            elif self.path == "/api/results/delete":
                body = self._body()
                gid = body.get("group_id")
                vids = set(body.get("vault_ids") or [])
                removed = []

                def _mut(g):
                    keep = []
                    for r in g["results"]:
                        if r.get("vault_id") in vids:
                            if r.get("uuid"):
                                removed.append(r["uuid"])
                        else:
                            keep.append(r)
                    g["results"] = keep
                    g["total"] = sum(1 for r in keep if r.get("status") != "skipped")

                _update_group(gid, _mut)
                if removed:
                    with _DATA_LOCK:
                        hist = read_history()
                        for u in removed:
                            hist.pop(u, None)
                        write_history(hist)
                self._send(200, {"ok": True, "removed": len(vids)})
            elif self.path == "/api/account/recheck":
                uuid = self._body().get("uuid")
                purl = pick_least_used_proxy()
                if not purl:
                    self._send(200, {"ok": False, "error": "Нет валидных прокси."})
                    return
                _inc_proxy_uses([purl])
                masked = _mask_proxy(purl)
                with _DATA_LOCK:
                    hist = read_history()
                    entry = hist.get(uuid)
                    if entry:
                        entry.setdefault("data", {})["proxy"] = purl
                        write_history(hist)
                with _DATA_LOCK:
                    items = read_vault()
                    for it in items:
                        if it.get("uuid") == uuid:
                            it["proxy"] = masked
                    write_vault(items)
                self._send(200, {"ok": True, "proxy": purl})
            elif self.path == "/api/overage/set":
                body = self._body()
                uuid = body.get("uuid")
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                if not ou:
                    self._send(200, {"ok": False, "error": "Не удалось определить организацию"})
                    return
                if bool(body.get("enabled")):
                    try:
                        minor = int(round(float(body.get("limit_major")) * 100))
                    except (TypeError, ValueError):
                        self._send(200, {"ok": False, "error": "Некорректный лимит"})
                        return
                    if minor <= 0:
                        self._send(200, {"ok": False, "error": "Лимит должен быть > 0"})
                        return
                    payload = {"is_enabled": True, "monthly_credit_limit": minor,
                               "currency": _resolve_currency(uuid)}
                else:
                    payload = {"is_enabled": False}
                st, data = api_request("PUT", f"/organizations/{ou}/overage_spend_limit",
                                            session, payload=payload,
                                            extra_headers=mutation_headers(session))
                ok = bool(st and st < 400 and isinstance(data, dict))
                if ok:
                    with _DATA_LOCK:
                        hist = read_history()
                        e = hist.get(uuid)
                        if e:
                            e.setdefault("data", {})["overage"] = data
                            write_history(hist)

                    def _mut(r):
                        r["used"] = data.get("used_credits")
                        r["limit"] = data.get("monthly_credit_limit")
                        if data.get("currency"):
                            r["currency"] = data.get("currency")
                    _update_group_rows_by_uuid(uuid, _mut)
                elif st == 401:
                    _mark_invalid_by_uuid(uuid)
                self._send(200, {"ok": ok, "overage": data if ok else None,
                                 "invalidated": bool(not ok and st == 401),
                                 "error": None if ok else _err(st, data)})
            elif self.path == "/api/autoreload/set":
                body = self._body()
                uuid = body.get("uuid")
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                if not ou:
                    self._send(200, {"ok": False, "error": "Не удалось определить организацию"})
                    return
                if bool(body.get("enabled")):
                    try:
                        th = int(round(float(body.get("threshold_major")) * 100))
                        rt = int(round(float(body.get("reload_to_major")) * 100))
                    except (TypeError, ValueError):
                        self._send(200, {"ok": False, "error": "Некорректные суммы"})
                        return
                    if not (th > 0 and rt > th):
                        self._send(200, {"ok": False, "error": "«Пополнять до» должно быть больше порога"})
                        return
                    payload = {"enabled": True, "threshold_in_minor_units": th,
                               "reload_to_in_minor_units": rt, "currency": _resolve_currency(uuid)}
                else:
                    payload = {"enabled": False}
                st, data = api_request("PUT", f"/organizations/{ou}/contracts/auto_reload_settings",
                                            session, payload=payload,
                                            extra_headers=mutation_headers(session))
                ok = bool(st and st < 400 and isinstance(data, dict))
                if ok:
                    with _DATA_LOCK:
                        hist = read_history()
                        e = hist.get(uuid)
                        if e:
                            cr = e.setdefault("data", {}).setdefault("credits", {}) or {}
                            cr["auto_reload_settings"] = data if data.get("enabled") else None
                            e["data"]["credits"] = cr
                            write_history(hist)
                elif st == 401:
                    _mark_invalid_by_uuid(uuid)
                self._send(200, {"ok": ok, "auto_reload": data if ok else None,
                                 "invalidated": bool(not ok and st == 401),
                                 "error": None if ok else _err(st, data)})
            elif self.path == "/api/token/create":
                body = self._body()
                uuid = body.get("uuid")
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                self._send(200, claude_code_token(session, ou))
            elif self.path == "/api/account/upgrade":
                body = self._body()
                uuid = body.get("uuid")
                tier = body.get("tier")
                if tier not in ("5x", "20x", "pro"):
                    self._send(200, {"ok": False, "error": "Неверный тариф"})
                    return
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                if not _has_payment_for(uuid):
                    self._send(200, {"ok": False, "skipped": True, "error": "Нет привязанной карты"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                if tier == "pro":
                    self._send(200, claude_upgrade_pro(session, ou))
                else:
                    self._send(200, claude_upgrade_max(session, ou, tier))
            elif self.path == "/api/account/topup":
                body = self._body()
                uuid = body.get("uuid")
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                if not _has_payment_for(uuid):
                    self._send(200, {"ok": False, "skipped": True, "error": "Нет привязанной карты"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                self._send(200, claude_buy_credits(session, ou, body.get("amount")))
            elif self.path == "/api/oauth/authorize":
                body = self._body()
                uuid = body.get("uuid")
                session = _strict_session(uuid)
                if not session:
                    self._send(200, {"ok": False, "error": "Сессия для аккаунта не найдена"})
                    return
                ou = _org_uuid_for(uuid) or org_for(session)
                q = parse_authorize_url(body.get("url"))
                self._send(200, claude_code_authorize(session, ou, q))
            elif self.path == "/api/token/revoke":
                body = self._body()
                s = action_session(body)
                self._send(200, {"ok": bool(s) and revoke_token(s, body.get("id"))})
            elif self.path == "/api/session/logout":
                body = self._body()
                s = action_session(body)
                self._send(200, {"ok": bool(s) and logout_session(s, body.get("created_at"), body.get("application_slug"))})
            elif self.path == "/api/sessions/logout-all":
                s = action_session(self._body())
                self._send(200, {"ok": bool(s), "count": logout_all_sessions(s) if s else 0})
            elif self.path == "/api/tokens/revoke-all":
                s = action_session(self._body())
                self._send(200, {"ok": bool(s), "count": revoke_all_tokens(s) if s else 0})
            elif self.path == "/api/history/delete":
                uuid = self._body().get("uuid")
                with _DATA_LOCK:
                    hist = read_history()
                    if uuid in hist:
                        del hist[uuid]
                        write_history(hist)
                self._send(200, {"ok": True})
            elif self.path == "/api/vault/add":
                body = self._body()
                entries = body.get("files")
                if entries is None and body.get("file") is not None:
                    entries = [body.get("file")]
                added = updated = 0
                with _DATA_LOCK:
                    for ent in (entries or []):
                        if isinstance(ent, dict) and ("jar" in ent or "data" in ent):
                            jar = ent.get("jar") if ent.get("jar") is not None else ent.get("data")
                            fname = ent.get("name")
                        else:
                            jar, fname = ent, None
                        res = add_cookie_jar(jar, fname)
                        if res == "added":
                            added += 1
                        elif res == "updated":
                            updated += 1
                    items = _vault_rows(read_vault())
                self._send(200, {"ok": True, "added": added, "updated": updated, "items": items})
            elif self.path == "/api/vault/delete":
                sid = str(self._body().get("id") or "")
                with _DATA_LOCK:
                    items = read_vault()
                    keep = [it for it in items if it.get("id") != sid]
                    if len(keep) != len(items):
                        write_vault(keep)
                self._send(200, {"ok": True, "items": _vault_rows(keep)})
            elif self.path == "/api/proxies/add":
                body = self._body()
                with _DATA_LOCK:
                    items = read_proxies()
                    idx = {}
                    for i, p in enumerate(items):
                        idx.setdefault(_pkey(p, _pwd_of(p)), i)
                    added = updated = dupes = invalid = 0
                    for line in (body.get("text") or "").splitlines():
                        s = line.strip()
                        pp = parse_proxy(line)
                        if not pp:
                            if s and not s.startswith("#"):
                                invalid += 1
                            continue
                        k = _pkey(pp, _pwd_of(pp))
                        j = idx.get(k)
                        if j is not None:
                            ex = items[j]
                            if ex["url"].lower() == pp["url"].lower():
                                dupes += 1
                                continue
                            ex["raw"] = s
                            ex["url"], ex["scheme"] = pp["url"], pp["scheme"]
                            ex["host"], ex["port"], ex["user"] = pp["host"], pp["port"], pp["user"]
                            updated += 1
                        else:
                            items.append({"id": _uuid.uuid4().hex[:12], "raw": s, **pp,
                                          "tags": [pp["scheme"]], "enabled": True, "status": "unchecked",
                                          "code": None, "latency": None, "checked_at": None, "result": None})
                            idx[k] = len(items) - 1
                            added += 1
                    write_proxies(items)
                self._send(200, {"ok": True, "added": added, "updated": updated,
                                 "dupes": dupes, "invalid": invalid, "items": items})
            elif self.path == "/api/proxies/check":
                body = self._body()
                with _DATA_LOCK:
                    items = read_proxies()
                    if body.get("all"):
                        subset = [p for p in items if p.get("enabled")]
                    else:
                        ids = set(body.get("ids") or [])
                        subset = [p for p in items if p.get("id") in ids]
                check_proxies(subset, clamp_threads(body.get("threads")))
                with _DATA_LOCK:
                    items = read_proxies()
                    upd = {p.get("id"): p for p in subset}
                    for p in items:
                        u = upd.get(p.get("id"))
                        if u:
                            for k in ("status", "code", "latency", "checked_at", "result"):
                                p[k] = u[k]
                    write_proxies(items)
                self._send(200, {"ok": True, "checked": len(subset), "items": items})
            elif self.path == "/api/proxies/delete":
                ids = set(self._body().get("ids") or [])
                with _DATA_LOCK:
                    items = [p for p in read_proxies() if p.get("id") not in ids]
                    write_proxies(items)
                self._send(200, {"ok": True, "items": items})
            elif self.path == "/api/proxies/toggle":
                body = self._body()
                ids, en = set(body.get("ids") or []), bool(body.get("enabled"))
                with _DATA_LOCK:
                    items = read_proxies()
                    for p in items:
                        if p.get("id") in ids:
                            p["enabled"] = en
                    write_proxies(items)
                self._send(200, {"ok": True, "items": items})
            elif self.path == "/api/proxies/scheme":
                body = self._body()
                pid = body.get("id")
                scheme = (body.get("scheme") or "").strip().lower()
                if scheme not in PROXY_SCHEMES:
                    self._send(200, {"ok": False, "error": "неизвестная схема"})
                else:
                    with _DATA_LOCK:
                        items = read_proxies()
                        for p in items:
                            if p.get("id") == pid:
                                user, pwd = p.get("user"), _pwd_of(p)
                                auth = (user + ((":" + pwd) if pwd is not None else "") + "@") if user else ""
                                p["scheme"] = scheme
                                p["url"] = f"{scheme}://{auth}{p.get('host')}:{p.get('port')}"
                                p["raw"] = p["url"]
                                p["tags"] = [scheme]
                                p["status"] = "unchecked"
                                p["code"] = p["latency"] = p["checked_at"] = p["result"] = None
                                break
                        write_proxies(items)
                    self._send(200, {"ok": True, "items": items})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)})


def run():
    parser = argparse.ArgumentParser(description="ClaudeChecker — веб-контролёр аккаунтов claude.ai.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--open", action="store_true", help="открыть браузер")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print(_c("⚠ host не localhost — управление аккаунтом будет доступно по сети!", C.YELLOW))

    url = f"http://{args.host}:{args.port}"
    print(_c(f"ClaudeChecker запущен: {url}", C.GREEN))
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    run()
