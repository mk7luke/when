"""When: five-field cron in plain English, plus the next run times."""

from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
PUBLIC_HOST = os.environ.get("WHEN_PUBLIC_HOST", "https://when.hermz.xyz").rstrip("/")
BIND_HOST = os.environ.get("WHEN_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("WHEN_PORT", "8881"))

RATE_LIMIT = 60
RATE_WINDOW = 3600
_hits: dict[str, list[float]] = {}
_page_cache: dict[str, tuple[float, str]] = {}

ZONES = [
    "UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
    "Europe/Paris",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
]

PRESETS = [
    ("Every 5 minutes", "*/5 * * * *"),
    ("Hourly", "0 * * * *"),
    ("Daily at 09:00", "0 9 * * *"),
    ("Weekdays at 09:00", "0 9 * * 1-5"),
    ("Sunday at 00:00", "0 0 * * 0"),
    ("First of the month", "0 0 1 * *"),
]

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

DOW = {
    "0": "Sunday",
    "7": "Sunday",
    "1": "Monday",
    "2": "Tuesday",
    "3": "Wednesday",
    "4": "Thursday",
    "5": "Friday",
    "6": "Saturday",
    "sun": "Sunday",
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
}
MONTHS = {
    "1": "January",
    "2": "February",
    "3": "March",
    "4": "April",
    "5": "May",
    "6": "June",
    "7": "July",
    "8": "August",
    "9": "September",
    "10": "October",
    "11": "November",
    "12": "December",
    "jan": "January",
    "feb": "February",
    "mar": "March",
    "apr": "April",
    "may": "May",
    "jun": "June",
    "jul": "July",
    "aug": "August",
    "sep": "September",
    "oct": "October",
    "nov": "November",
    "dec": "December",
}

_NUM = re.compile(r"\d{1,2}")
_STEP = re.compile(r"^\*/(\d{1,2})$")
_RANGE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_LIST = re.compile(r"^(\d{1,2})(,(\d{1,2}))+$")


class ExplainError(ValueError):
    pass


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "local"


def _rate_ok(ip: str) -> bool:
    now = time.time()
    window = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(window) >= RATE_LIMIT:
        _hits[ip] = window
        return False
    window.append(now)
    _hits[ip] = window
    return True


def _join_and(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _clock(hour: str, minute: str) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def normalize(expr: str) -> tuple[str, str | None]:
    raw = " ".join((expr or "").strip().split())
    if not raw:
        raise ExplainError("Need an expression.")
    if len(raw) > 80:
        raise ExplainError("That expression is too long.")
    if re.search(r"[^\w\s@*,/\-]", raw):
        raise ExplainError("Use a plain cron expression. No quotes or extra symbols.")
    low = raw.lower()
    if low == "@reboot":
        raise ExplainError("@reboot is not a clock time. It runs when the machine starts.")
    if low.startswith("@") and low not in MACROS:
        raise ExplainError("Unknown @ name. Try @hourly, @daily, @weekly, @monthly, or @yearly.")
    if low in MACROS:
        return MACROS[low], low
    parts = low.split(" ")
    if len(parts) == 6:
        raise ExplainError("That looks like six fields. This tool starts at minutes, not seconds.")
    if len(parts) != 5:
        raise ExplainError("Need five fields: minute hour day month weekday.")
    if any(part in {"l", "w", "?"} or "#" in part or re.search(r"\d+w$", part) for part in parts):
        raise ExplainError("Quartz extras (L, W, #) are not supported. Five-field cron only.")
    return low, None


def _dow_name(token: str) -> str | None:
    return DOW.get(token.lower())


def _mon_name(token: str) -> str | None:
    return MONTHS.get(token.lower())


def dow_phrase(dow: str) -> str | None:
    key = dow.lower()
    if key in {"1-5", "mon-fri"}:
        return "weekdays"
    if key in {"0,6", "6,0", "6-7", "sat,sun", "sun,sat", "0-6", "0-7"}:
        if key in {"0-6", "0-7"}:
            return "every day of the week"
        return "weekends"
    named = _dow_name(key)
    if named:
        return named
    m = _RANGE.fullmatch(key)
    if m:
        a, b = _dow_name(m.group(1)), _dow_name(m.group(2))
        if a and b:
            return f"{a} through {b}"
        return None
    if _LIST.fullmatch(key):
        names = [_dow_name(p) for p in key.split(",")]
        if all(names):
            return _join_and(names)  # type: ignore[arg-type]
    if re.fullmatch(r"[a-z]{3}(,[a-z]{3})+", key):
        names = [_dow_name(p) for p in key.split(",")]
        if all(names):
            return _join_and(names)  # type: ignore[arg-type]
    return None


def month_phrase(month: str) -> str | None:
    key = month.lower()
    named = _mon_name(key)
    if named:
        return named
    m = _RANGE.fullmatch(key)
    if m:
        a, b = _mon_name(m.group(1)), _mon_name(m.group(2))
        if a and b:
            return f"{a} through {b}"
    if re.fullmatch(r"[a-z]{3}-[a-z]{3}", key):
        a, b = key.split("-")
        if _mon_name(a) and _mon_name(b):
            return f"{_mon_name(a)} through {_mon_name(b)}"
    return None


def _is_num(field: str) -> bool:
    return bool(_NUM.fullmatch(field))


def english(five: str) -> tuple[str, str | None]:
    minute, hour, dom, month, dow = five.split(" ")
    warning = None
    if dom != "*" and dow != "*":
        warning = (
            "Day of month and weekday are both set. "
            "Standard cron fires when either one matches, not only when both match."
        )
        return _fallback(minute, hour, dom, month, dow), warning

    sentence = _pretty(minute, hour, dom, month, dow)
    if sentence is None:
        return _fallback(minute, hour, dom, month, dow), warning
    return sentence, warning


def _pretty(minute: str, hour: str, dom: str, month: str, dow: str) -> str | None:
    has_day = not (dom == "*" and month == "*" and dow == "*")
    clock = _clock_bit(minute, hour, has_day)
    if clock is None:
        return None
    if not has_day:
        return clock if clock.endswith(".") else clock + "."

    if dow != "*" and dom == "*" and month == "*":
        phrase = dow_phrase(dow)
        if not phrase:
            return None
        if phrase in {"weekdays", "weekends", "every day of the week"}:
            return f"{clock} on {phrase}."
        if " through " in phrase or " and " in phrase:
            return f"{clock} on {phrase}."
        return f"{clock} on {phrase}."

    if dow == "*" and month == "*" and _is_num(dom) and _is_num(minute) and _is_num(hour):
        return f"At {_clock(hour, minute)} on day {int(dom)} of every month."

    if dow == "*" and dom == "*" and month != "*":
        mon = month_phrase(month)
        if not mon or not _is_num(minute) or not _is_num(hour):
            return None
        return f"At {_clock(hour, minute)} in {mon}."

    if dow == "*" and _is_num(dom) and month != "*" and _is_num(minute) and _is_num(hour):
        mon = month_phrase(month)
        if not mon:
            return None
        return f"At {_clock(hour, minute)} on {mon} {int(dom)}."

    return None


def _clock_bit(minute: str, hour: str, has_day: bool) -> str | None:
    step = _STEP.fullmatch(minute)
    if step and hour == "*" and not has_day:
        n = int(step.group(1))
        if n == 1:
            return "Every minute."
        return f"Every {n} minutes."
    if minute == "0" and hour == "*" and not has_day:
        return "Every hour, on the hour."
    if _is_num(minute) and hour == "*" and not has_day:
        return f"Every hour at minute {int(minute)}."
    hour_step = _STEP.fullmatch(hour)
    if minute == "0" and hour_step and not has_day:
        n = int(hour_step.group(1))
        if n == 1:
            return "Every hour, on the hour."
        return f"Every {n} hours, on the hour."
    if _is_num(minute) and _is_num(hour):
        stamp = _clock(hour, minute)
        return f"At {stamp}" if has_day else f"Every day at {stamp}."
    if minute == "0" and _RANGE.fullmatch(hour) and not has_day:
        a, b = hour.split("-")
        return f"Every hour from {int(a):02d}:00 through {int(b):02d}:00."
    if step and _RANGE.fullmatch(hour) and not has_day:
        n = int(step.group(1))
        a, b = hour.split("-")
        return f"Every {n} minutes, from {int(a):02d}:00 through {int(b):02d}:00."
    if _is_num(minute) and _LIST.fullmatch(hour) and not has_day:
        stamps = [_clock(h, minute) for h in hour.split(",")]
        return f"Every day at {_join_and(stamps)}."
    if _is_num(minute) and _is_num(hour):
        return f"At {_clock(hour, minute)}"
    return None


def _field_note(raw: str) -> str:
    if raw == "*":
        return "any"
    step = _STEP.fullmatch(raw)
    if step:
        return f"every {int(step.group(1))}"
    if _RANGE.fullmatch(raw):
        a, b = raw.split("-")
        return f"{a} through {b}"
    if "," in raw:
        return raw.replace(",", ", ")
    return raw


def _fallback(minute: str, hour: str, dom: str, month: str, dow: str) -> str:
    bits = [
        f"minute is {_field_note(minute)}",
        f"hour is {_field_note(hour)}",
        f"day of month is {_field_note(dom)}",
        f"month is {_field_note(month)}",
        f"weekday is {_field_note(dow)}",
    ]
    return "Fires when " + ", ".join(bits) + "."


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ExplainError("Unknown timezone. Use an IANA name like America/Los_Angeles.") from e


def next_runs(five: str, tz_name: str, count: int) -> list[dict[str, str]]:
    tz = _zone(tz_name)
    start = datetime.now(tz)
    try:
        itr = croniter(five, start)
    except (ValueError, KeyError) as e:
        raise ExplainError("That expression does not parse.") from e
    runs: list[dict[str, str]] = []
    seen: set[str] = set()
    for _ in range(count):
        try:
            dt = itr.get_next(datetime)
        except (ValueError, KeyError) as e:
            raise ExplainError("That expression does not parse.") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        local = dt.astimezone(tz)
        utc = dt.astimezone(ZoneInfo("UTC"))
        key = utc.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            break
        seen.add(key)
        runs.append(
            {
                "local": local.strftime("%a %d %b %Y, %H:%M %Z"),
                "utc": utc.strftime("%Y-%m-%d %H:%M UTC"),
            }
        )
    if not runs:
        raise ExplainError("No upcoming runs for that expression.")
    return runs


def explain(expr: str, tz_name: str, count: int = 8) -> dict[str, Any]:
    five, alias = normalize(expr)
    if not croniter.is_valid(five):
        raise ExplainError("That expression does not parse.")
    text, warning = english(five)
    minute, hour, dom, month, dow = five.split(" ")
    labels = ("Minute", "Hour", "Day", "Month", "Weekday")
    raws = (minute, hour, dom, month, dow)
    return {
        "ok": True,
        "expr": five,
        "alias": alias,
        "english": text,
        "warning": warning,
        "timezone": tz_name,
        "fields": [
            {"name": labels[i], "raw": raws[i], "note": _field_note(raws[i])} for i in range(5)
        ],
        "next": next_runs(five, tz_name, count),
    }


def resolve_tz(tz: str | None, tz_custom: str | None) -> str:
    custom = (tz_custom or "").strip()
    picked = (tz or "").strip()
    name = custom or picked or "UTC"
    if len(name) > 64 or "/" not in name and name != "UTC":
        if name != "UTC":
            raise ExplainError("Unknown timezone. Use an IANA name like America/Los_Angeles.")
    _zone(name)
    return name


def _asset_version(*paths: Path) -> str:
    m = 0.0
    for p in paths:
        try:
            m = max(m, p.stat().st_mtime)
        except OSError:
            pass
    return str(int(m))


def _shell() -> str:
    html_path = STATIC / "index.html"
    css_path = STATIC / "style.css"
    js_path = STATIC / "home.js"
    stamp = max(
        html_path.stat().st_mtime,
        css_path.stat().st_mtime if css_path.exists() else 0.0,
        js_path.stat().st_mtime if js_path.exists() else 0.0,
    )
    cached = _page_cache.get("shell")
    if cached and cached[0] == stamp:
        return cached[1]
    page = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    page = re.sub(
        r"""[ \t]*<link[^>]+href=["']/static/style\.css[^"']*["'][^>]*>\s*""",
        "",
        page,
        flags=re.I,
    )
    injection = f'<style id="when-css">\n{css}\n</style>\n'
    page = re.sub(r"</head>", injection + "</head>", page, count=1, flags=re.I)
    v = _asset_version(js_path, css_path)
    page = re.sub(
        r"""src=(["'])/static/home\.js(?:\?[^"']*)?\1""",
        rf"src=\1/static/home.js?v={v}\1",
        page,
    )
    _page_cache["shell"] = (stamp, page)
    return page


def _tz_options(selected: str, custom_used: bool) -> str:
    chunks = []
    for zone in ZONES:
        sel = "" if custom_used or zone != selected else " selected"
        chunks.append(f'<option value="{html.escape(zone)}"{sel}>{html.escape(zone)}</option>')
    return "".join(chunks)


def _presets(tz_name: str, custom_used: bool) -> str:
    bits = []
    for label, expr in PRESETS:
        q = f"/?e={quote(expr)}&tz={quote(tz_name if not custom_used else 'UTC')}"
        if custom_used:
            q += f"&tz_custom={quote(tz_name)}"
        bits.append(f'<a class="chip" href="{html.escape(q)}">{html.escape(label)}</a>')
    return "".join(bits)


def _result_html(data: dict[str, Any]) -> str:
    alias = ""
    if data.get("alias"):
        alias = f'<p class="alias">{html.escape(data["alias"])} expands to {html.escape(data["expr"])}</p>'
    warning = ""
    if data.get("warning"):
        warning = f'<p class="status warn">{html.escape(data["warning"])}</p>'
    times = []
    for row in data["next"]:
        times.append(
            "<div class=\"time\">"
            f"<strong>{html.escape(row['local'])}</strong>"
            f"<span>{html.escape(row['utc'])}</span>"
            "</div>"
        )
    fields = []
    for field in data["fields"]:
        fields.append(
            "<div class=\"field-chip\">"
            f"<span>{html.escape(field['name'])}</span>"
            f"<strong>{html.escape(field['raw'])}</strong>"
            f"<em>{html.escape(field['note'])}</em>"
            "</div>"
        )
    zone = html.escape(data["timezone"])
    return (
        '<section class="result" id="result">'
        f"{alias}"
        f'<p class="english" id="english">{html.escape(data["english"])}</p>'
        f'<p class="hint">Times in {zone}. 24-hour clock.</p>'
        f"{warning}"
        f'<div class="times">{"".join(times)}</div>'
        f'<div class="fields">{"".join(fields)}</div>'
        '<div class="row-actions">'
        '<button type="button" class="btn" id="copy-expr">Copy expression</button>'
        '<button type="button" class="btn" id="copy-link">Copy link</button>'
        "</div>"
        "</section>"
    )


def render_page(
    expr: str = "",
    tz_name: str = "UTC",
    custom: str = "",
    custom_used: bool = False,
    status: str = "",
    status_class: str = "",
    result: str = "",
) -> str:
    page = _shell()
    page = page.replace("<!--EXPR-->", html.escape(expr, quote=True))
    page = page.replace("<!--TZ_OPTIONS-->", _tz_options(tz_name, custom_used))
    page = page.replace("<!--TZ_CUSTOM-->", html.escape(custom, quote=True))
    page = page.replace("<!--PRESETS-->", _presets(tz_name, custom_used))
    page = page.replace("<!--STATUS-->", html.escape(status))
    page = page.replace("<!--STATUS_CLASS-->", status_class)
    page = page.replace("<!--RESULT-->", result)
    return page


app = FastAPI(title="When", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    if request.url.path in {"/", "/index.html"}:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "when"}


@app.get("/robots.txt")
def robots() -> Response:
    return Response((STATIC / "robots.txt").read_text(encoding="utf-8"), media_type="text/plain")


def _run_explain(expr: str, tz: str, tz_custom: str, count: int) -> dict[str, Any]:
    if count < 1 or count > 8:
        raise ExplainError("Ask for 1 to 8 runs.")
    zone = resolve_tz(tz, tz_custom)
    return explain(expr, zone, count)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    e: str = "",
    tz: str = "UTC",
    tz_custom: str = "",
) -> HTMLResponse:
    expr = e
    custom = tz_custom.strip()
    status = ""
    status_class = ""
    result = ""
    tz_name = "UTC"
    custom_used = False
    if expr.strip():
        if not _rate_ok(_client_ip(request)):
            status = "Too many explains from this network. Try again in a bit."
            status_class = " bad"
        else:
            try:
                tz_name = resolve_tz(tz, custom)
                custom_used = bool(custom)
                data = explain(expr, tz_name, 8)
                result = _result_html(data)
                expr = data["expr"] if not data.get("alias") else expr.strip()
            except ExplainError as err:
                status = str(err)
                status_class = " bad"
                try:
                    tz_name = resolve_tz(tz, custom)
                    custom_used = bool(custom)
                except ExplainError:
                    tz_name = "UTC"
                    custom_used = False
    else:
        try:
            tz_name = resolve_tz(tz, custom) if (tz or custom) else "UTC"
            custom_used = bool(custom)
        except ExplainError as err:
            status = str(err)
            status_class = " bad"
            tz_name = "UTC"
    page = render_page(
        expr=expr.strip(),
        tz_name=tz_name,
        custom=custom if custom_used else "",
        custom_used=custom_used,
        status=status,
        status_class=status_class,
        result=result,
    )
    return HTMLResponse(page)


@app.get("/api/explain")
def api_explain(
    request: Request,
    e: str = Query(""),
    tz: str = Query("UTC"),
    tz_custom: str = Query(""),
    n: int = Query(8),
) -> JSONResponse:
    if not _rate_ok(_client_ip(request)):
        raise HTTPException(429, "Too many explains from this network. Try again in a bit.")
    try:
        data = _run_explain(e, tz, tz_custom, n)
    except ExplainError as err:
        raise HTTPException(400, str(err)) from err
    return JSONResponse(data)


def _self_check() -> None:
    samples = {
        "*/5 * * * *": "Every 5 minutes.",
        "0 * * * *": "Every hour, on the hour.",
        "30 * * * *": "Every hour at minute 30.",
        "0 9 * * *": "Every day at 09:00.",
        "30 14 * * *": "Every day at 14:30.",
        "0 9 * * 1-5": "At 09:00 on weekdays.",
        "0 0 * * 0": "At 00:00 on Sunday.",
        "0 9 * * 1,3,5": "At 09:00 on Monday, Wednesday, and Friday.",
        "0 0 1 * *": "At 00:00 on day 1 of every month.",
        "0 0 1 1 *": "At 00:00 on January 1.",
        "0 */2 * * *": "Every 2 hours, on the hour.",
        "0 9-17 * * *": "Every hour from 09:00 through 17:00.",
        "0 9,17 * * *": "Every day at 09:00 and 17:00.",
    }
    for expr, want in samples.items():
        got = explain(expr, "UTC", 2)
        if got["english"] != want:
            raise SystemExit(f"{expr!r}\n  got {got['english']!r}\n  want {want!r}")
    alias = explain("@daily", "America/Los_Angeles", 2)
    if alias["alias"] != "@daily" or alias["expr"] != "0 0 * * *":
        raise SystemExit(f"alias failed: {alias}")
    if "Los_Angeles" not in alias["next"][0]["local"] and "PST" not in alias["next"][0]["local"] and "PDT" not in alias["next"][0]["local"]:
        raise SystemExit(f"tz format unexpected: {alias['next'][0]}")
    both = explain("0 9 1 * 1", "UTC", 3)
    if not both["warning"] or "either" not in both["warning"]:
        raise SystemExit(f"warning missing: {both}")
    try:
        explain("@reboot", "UTC", 1)
        raise SystemExit("reboot should fail")
    except ExplainError:
        pass
    try:
        explain("0 9 * * * *", "UTC", 1)
        raise SystemExit("six fields should fail")
    except ExplainError:
        pass
    print("self-check ok", len(samples))


if __name__ == "__main__":
    _self_check()
