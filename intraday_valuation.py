#!/usr/bin/env python3
"""显示自选基金的盘中估算涨跌。数据来自第三方估值，不是官方净值。"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import time
from datetime import datetime
from importlib.util import find_spec
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"
EASTMONEY_ENDPOINTS = (
    "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast",
    "https://fundcomapi.eastmoney.com/mm/newCore/FundValuationLast",
)
DAYFUND_ENDPOINT = "https://fund.dayfund.com.cn/ajs/ajaxdata.shtml"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_ssl_context() -> ssl.SSLContext:
    """Use trusted CA bundles from Python, macOS, or an explicit environment path."""
    candidates = []
    configured_bundle = os.environ.get("SSL_CERT_FILE")
    if configured_bundle:
        candidates.append(Path(configured_bundle))
    if find_spec("certifi") is not None:
        import certifi

        candidates.append(Path(certifi.where()))
    candidates.extend(
        Path(path)
        for path in (
            "/etc/ssl/cert.pem",
            "/opt/homebrew/etc/openssl@3/cert.pem",
        )
    )
    default_bundle = ssl.get_default_verify_paths().cafile
    if default_bundle:
        candidates.append(Path(default_bundle))
    for bundle in candidates:
        if bundle.is_file():
            return ssl.create_default_context(cafile=str(bundle))
    return ssl.create_default_context()


SSL_CONTEXT = make_ssl_context()


def load_funds(path: Path) -> list[dict[str, str]]:
    """Read either ["000001", ...] or {"funds": [{"code": "000001"}, ...]} ."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到自选列表：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"自选列表不是有效 JSON：{exc}") from exc

    items = config.get("funds") if isinstance(config, dict) else config
    if not isinstance(items, list) or not items:
        raise ValueError("自选列表应是非空数组，或包含 funds 数组的对象")

    funds = []
    seen = set()
    for item in items:
        code = item.get("code", "") if isinstance(item, dict) else item
        code = str(code).strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"基金代码应为 6 位数字：{code!r}")
        if code not in seen:
            funds.append({"code": code})
            seen.add(code)
    return funds


def as_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def has_valuation(item: dict | None) -> bool:
    return bool(item) and (
        as_number(item.get("GSZ")) is not None
        or as_number(item.get("GSZZL")) is not None
    )


def fetch_eastmoney(codes: list[str]) -> dict[str, dict]:
    params = urlencode({"FCODES": ",".join(codes), "FIELDS": FIELDS})
    errors = []
    for endpoint in EASTMONEY_ENDPOINTS:
        request = Request(
            f"{endpoint}?{params}",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FundWatch/1.0)",
                "Referer": "https://h5.1234567.com.cn/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        try:
            with urlopen(request, timeout=12, context=SSL_CONTEXT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            items = (payload.get("data", payload.get("Datas", [])) or []) if isinstance(payload, dict) else []
            if not isinstance(items, list):
                raise ValueError("接口返回格式异常（data/Datas 不是数组）")
            return {
                str(item.get("FCODE", "")).zfill(6): item
                for item in items
                if isinstance(item, dict) and item.get("FCODE")
            }
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("天天基金接口请求失败：\n  " + "\n  ".join(errors))


def fetch_dayfund(code: str, today: str) -> tuple[dict | None, str | None]:
    """Fetch Dayfund's model estimate; reject values whose estimate date is stale."""
    params = urlencode({"showtype": "getfundvalue", "fundcode": code})
    request = Request(
        f"{DAYFUND_ENDPOINT}?{params}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FundWatch/1.0)",
            "Referer": f"https://fund.dayfund.com.cn/fundpre/{code}.html",
            "Accept": "text/plain,*/*",
        },
    )
    with urlopen(request, timeout=12, context=SSL_CONTEXT) as response:
        fields = response.read().decode("utf-8", "replace").strip().split("|")
    if len(fields) < 11:
        raise ValueError("基金速查网返回格式异常")

    estimate_date = fields[9].strip()
    update_time = fields[10].strip()
    if estimate_date != today:
        return None, estimate_date or "未知日期"

    change = fields[5].strip().removesuffix("%").strip()
    estimate_nav = fields[7].strip()
    if as_number(change) is None and as_number(estimate_nav) is None:
        return None, None

    return {
        "FCODE": code,
        "GSZZL": change,
        "GSZ": estimate_nav,
        "GZTIME": f"{estimate_date} {update_time}".strip(),
        "_source": "基金速查网",
    }, None


def fetch_valuations(codes: list[str]) -> dict[str, dict]:
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    try:
        primary = fetch_eastmoney(codes)
        primary_error = None
    except RuntimeError as exc:
        primary = {}
        primary_error = str(exc)

    results = {}
    for code in codes:
        item = primary.get(code, {})
        if has_valuation(item):
            item["_source"] = "天天基金"
            results[code] = item
            continue

        # Use an independent model-based estimate only when the primary has no value.
        try:
            backup, stale_date = fetch_dayfund(code, today)
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
            backup, stale_date = None, None
            backup_error = str(exc)
        else:
            backup_error = None

        if backup:
            merged = {**item, **backup}
            if item.get("SHORTNAME"):
                merged["SHORTNAME"] = item["SHORTNAME"]
            merged["_source"] = "基金速查网估算"
            results[code] = merged
        elif item:
            item["_source"] = "天天基金（无估值）"
            item["_stale_date"] = stale_date
            results[code] = item
        else:
            results[code] = {
                "FCODE": code,
                "_source": "无可用估值",
                "_stale_date": stale_date,
            }

        if backup_error:
            results[code]["_backup_error"] = backup_error
        if primary_error:
            results[code]["_primary_error"] = primary_error
    return results


def format_number(value: object, suffix: str = "", signed: bool = False) -> str:
    number = as_number(value)
    if number is None:
        return "—"
    return f"{number:+.2f}{suffix}" if signed else f"{number:.4f}{suffix}"


def show(funds: list[dict[str, str]]) -> None:
    data = fetch_valuations([fund["code"] for fund in funds])
    now = datetime.now().astimezone()
    print(f"\n盘中估算 · {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'代码':<8} {'基金名称':<24} {'估算涨跌':>10} {'估算净值':>10}  {'数据源':<16} 更新时间")
    print("-" * 100)
    for fund in funds:
        code = fund["code"]
        item = data.get(code, {})
        name = str(item.get("SHORTNAME") or code)[:22]
        change = format_number(item.get("GSZZL"), "%", signed=True)
        estimate = format_number(item.get("GSZ"))
        updated = item.get("GZTIME") or "—"
        source = item.get("_source", "无可用估值")
        if change == "—" and estimate == "—":
            change = "暂无估值"
            if item.get("_stale_date"):
                updated = f"备用源数据日期 {item['_stale_date']}"
            else:
                updated = "—"
        print(f"{code:<8} {name:<24} {change:>10} {estimate:>10}  {source:<16} {updated}")
    print("\n注：所有盘中涨跌均为第三方估算，不是官方净值；备用估值仅接受当日数据，并按估值源和更新时间标注。")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看自选基金盘中估算涨跌")
    parser.add_argument("-c", "--config", type=Path, default=Path(__file__).with_name("funds.json"),
                        help="自选列表 JSON 文件（默认与脚本同目录的 funds.json）")
    parser.add_argument("-w", "--watch", action="store_true", help="持续刷新")
    parser.add_argument("-i", "--interval", type=int, default=60, help="刷新间隔秒数，默认 60")
    args = parser.parse_args()
    if args.interval < 15:
        parser.error("刷新间隔不能小于 15 秒")
    try:
        funds = load_funds(args.config)
        while True:
            try:
                show(funds)
            except (RuntimeError, OSError) as exc:
                print(f"获取失败：{exc}", file=sys.stderr)
            if not args.watch:
                return 0
            time.sleep(args.interval)
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
