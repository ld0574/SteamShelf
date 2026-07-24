from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


LOG = logging.getLogger("steam_to_notion")
NOTION_VERSION = "2022-06-28"
STEAM_LOGO_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/8/83/Steam_icon_logo.svg"
)


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Account:
    steam_id: str
    profile_id: str
    account_name: str


@dataclass(frozen=True)
class FieldDef:
    notion_name: str
    record_key: str
    notion_type: str
    auto_create: bool = True


@dataclass
class Config:
    steam_api_key: str
    notion_api_key: str
    notion_database_id: str
    accounts: list[Account]
    include_played_free_games: bool = True
    enable_item_update: bool = True
    enable_filter: bool = False
    fetch_web_library: bool = False
    fetch_store_metadata: bool = True
    schema: str = "cn"
    insert_fields: set[str] | None = None
    update_fields: set[str] | None = None
    sync_main_games_only: bool = True
    skip_playtime_web: bool = True
    use_account_field: bool = False
    top_tags: int = 5
    verify_cover_urls: bool = False
    store_failure_limit: int = 3
    refresh_store_metadata: bool = False
    abandoned_days: int = 180
    http_timeout: int = 15
    http_retries: int = 2
    request_delay: float = 0.4
    export_csv_path: str | None = None
    export_xlsx_path: str | None = None
    dry_run: bool = False
    mark_uploaded: bool = True
    set_page_cover: bool = True
    set_page_icon: bool = True

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        steam_api_key = env_any("STEAM_API_KEY", required=True)
        notion_api_key = env_any("NOTION_API_KEY", default="")
        notion_database_id = env_any("NOTION_DATABASE_ID", default="")
        accounts = parse_accounts()

        use_account_default = len(accounts) > 1
        use_account_field = env_bool("USE_ACCOUNT_FIELD", use_account_default)

        cfg = cls(
            steam_api_key=steam_api_key,
            notion_api_key=notion_api_key,
            notion_database_id=notion_database_id,
            accounts=accounts,
            include_played_free_games=env_bool(
                "INCLUDE_PLAYED_FREE_GAMES",
                env_bool("include_played_free_games", True),
            ),
            enable_item_update=env_bool(
                "ENABLE_ITEM_UPDATE", env_bool("enable_item_update", True)
            ),
            enable_filter=env_bool("ENABLE_FILTER", env_bool("enable_filter", False)),
            fetch_web_library=env_bool(
                "FETCH_WEB_LIBRARY", env_bool("FETCH_STEAM_WEB_LIBRARY", False)
            ),
            fetch_store_metadata=env_bool("FETCH_STORE_METADATA", True),
            schema=env_any("NOTION_SCHEMA", "FIELD_PRESET", default="cn").lower(),
            insert_fields=env_field_set("INSERT_FIELDS"),
            update_fields=env_field_set("UPDATE_FIELDS"),
            sync_main_games_only=env_bool("SYNC_MAIN_GAMES_ONLY", True),
            skip_playtime_web=env_bool("SKIP_PLAYTIME_WEB", True),
            use_account_field=use_account_field,
            top_tags=max(0, env_int("TOP_TAGS", 5)),
            verify_cover_urls=env_bool("VERIFY_COVER_URLS", False),
            store_failure_limit=max(1, env_int("STORE_FAILURE_LIMIT", 3)),
            refresh_store_metadata=env_bool("REFRESH_STORE_METADATA", False),
            abandoned_days=max(31, env_int("ABANDONED_DAYS", 180)),
            http_timeout=max(5, env_int("HTTP_TIMEOUT", 15)),
            http_retries=max(1, env_int("HTTP_RETRIES", 2)),
            request_delay=max(0.0, env_float("REQUEST_DELAY", 0.4)),
            export_csv_path=args.export_csv or env_any("EXPORT_CSV_PATH", default=""),
            export_xlsx_path=args.export_xlsx or env_any("EXPORT_XLSX_PATH", default=""),
            dry_run=args.dry_run or env_bool("DRY_RUN", False),
            mark_uploaded=env_bool("MARK_UPLOADED", True),
            set_page_cover=env_bool("SET_PAGE_COVER", True),
            set_page_icon=env_bool("SET_PAGE_ICON", True),
        )

        if args.schema:
            cfg.schema = args.schema
        if cfg.schema == "english":
            cfg.schema = "legacy"
        if cfg.schema not in {"cn", "legacy"}:
            raise SyncError("NOTION_SCHEMA 仅支持 cn 或 legacy")
        if not cfg.dry_run and (not cfg.notion_api_key or not cfg.notion_database_id):
            raise SyncError("同步到 Notion 需要设置 NOTION_API_KEY 和 NOTION_DATABASE_ID")
        return cfg


@dataclass
class NotionSyncContext:
    db_props: dict[str, str]
    title_prop: str
    field_defs: list[FieldDef]
    existing_pages: dict[str, str]
    key_property: str | None
    store_metadata_cache: dict[str, dict[str, Any]]


FIELD_DEFS_CN: list[FieldDef] = [
    FieldDef("游戏名称", "游戏名称", "title"),
    FieldDef("游戏封面", "封面", "files"),
    FieldDef("游戏ID", "游戏ID", "number"),
    FieldDef("商店链接", "商店链接", "url"),
    FieldDef("游玩时间", "游玩时间", "rich_text"),
    FieldDef("总时长/h", "总时长小时", "number"),
    FieldDef("完成度", "完成度", "number"),
    FieldDef("成就总数", "成就总数", "number"),
    FieldDef("已解锁成就", "已解锁成就", "number"),
    FieldDef("首个成就解锁于", "首个成就时间", "date"),
    FieldDef("最后游玩日期", "最后游玩", "date"),
    FieldDef("游玩状态", "游玩状态", "status"),
    FieldDef("开发商", "开发商", "rich_text"),
    FieldDef("发行商", "发行商", "rich_text"),
    FieldDef("发售日期", "发售日期", "date"),
    FieldDef("商店标签", "商店标签", "multi_select"),
    FieldDef("内容类型", "内容类型", "select"),
    FieldDef("数据源", "数据源", "select"),
    FieldDef("抓取来源", "抓取来源", "select"),
    FieldDef("所属账户", "所属账户", "select"),
    FieldDef("游戏简介", "游戏简介", "rich_text"),
    FieldDef("评分", "评分", "number"),
]

FIELD_DEFS_LEGACY: list[FieldDef] = [
    FieldDef("name", "游戏名称", "title"),
    FieldDef("appid", "游戏ID", "number"),
    FieldDef("playtime", "总时长小时", "number"),
    FieldDef("last play", "最后游玩", "date"),
    FieldDef("store url", "商店链接", "url"),
    FieldDef("completion", "完成度", "number"),
    FieldDef("total achievements", "成就总数", "number"),
    FieldDef("achieved achievements", "已解锁成就", "number"),
    FieldDef("info", "游戏简介", "rich_text"),
    FieldDef("tags", "商店标签", "multi_select"),
    FieldDef("type", "内容类型", "select"),
    FieldDef("source", "抓取来源", "select"),
    FieldDef("data source", "数据源", "select"),
    FieldDef("account", "所属账户", "select"),
]

WEB_PLAYTIME_FIELDS = {
    "游玩时间",
    "最后游玩日期",
    "总时长/h",
    "playtime",
    "last play",
}

SELECT_OPTIONS: dict[str, list[dict[str, str]]] = {
    "内容类型": [
        {"name": "游戏本体", "color": "green"},
        {"name": "DLC", "color": "blue"},
        {"name": "试玩版", "color": "yellow"},
        {"name": "原声音轨", "color": "purple"},
        {"name": "其它", "color": "gray"},
    ],
    "数据源": [{"name": "Steam", "color": "blue"}],
    "抓取来源": [
        {"name": "API", "color": "green"},
        {"name": "网页", "color": "orange"},
        {"name": "两者都有", "color": "blue"},
        {"name": "未知", "color": "gray"},
    ],
}

STATUS_OPTIONS = [
    {"name": "未开始", "color": "gray", "group": "To-do"},
    {"name": "游玩中", "color": "blue", "group": "In progress"},
    {"name": "暂搁置", "color": "yellow", "group": "In progress"},
    {"name": "已弃坑", "color": "red", "group": "Complete"},
    {"name": "已通关", "color": "green", "group": "Complete"},
    {"name": "全成就", "color": "purple", "group": "Complete"},
]


def env_any(*names: str, default: str | None = None, required: bool = False) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    if required:
        joined = " / ".join(names)
        raise SyncError(f"缺少环境变量：{joined}")
    return default or ""


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_field_set(name: str) -> set[str] | None:
    value = env_any(name, default="")
    if not value:
        return None
    fields = [item.strip() for item in re.split(r"[,，;；|]", value) if item.strip()]
    return set(fields) or None


def redact_secrets(text: str) -> str:
    return re.sub(
        r"([?&](?:key|access_token|auth|token|secret)=)[^&\s)]+",
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )


def normalize_steam_identity(value: str) -> str:
    text = value.strip().rstrip("/")
    match = re.search(r"steamcommunity\.com/(?:profiles|id)/([^/?#]+)", text)
    if match:
        return match.group(1)
    return text


def parse_accounts() -> list[Account]:
    raw_accounts = env_any("STEAM_ACCOUNTS", default="")
    accounts: list[Account] = []

    if raw_accounts:
        try:
            parsed = json.loads(raw_accounts)
        except json.JSONDecodeError as exc:
            raise SyncError("STEAM_ACCOUNTS 必须是 JSON 数组") from exc
        if not isinstance(parsed, list):
            raise SyncError("STEAM_ACCOUNTS 必须是 JSON 数组")
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise SyncError(f"STEAM_ACCOUNTS 第 {idx} 项必须是对象")
            steam_id = normalize_steam_identity(
                str(item.get("steam_id") or item.get("STEAM_USER_ID") or "")
            )
            profile_id = normalize_steam_identity(str(
                item.get("profile_id") or item.get("profile") or steam_id
            ))
            account_name = str(
                item.get("account")
                or item.get("account_name")
                or item.get("name")
                or profile_id
            ).strip()
            if not steam_id:
                raise SyncError(f"STEAM_ACCOUNTS 第 {idx} 项缺少 steam_id")
            accounts.append(Account(steam_id, profile_id or steam_id, account_name))
    else:
        steam_id = normalize_steam_identity(env_any("STEAM_USER_ID", required=True))
        profile_id = normalize_steam_identity(env_any("STEAM_PROFILE_ID", default=steam_id))
        account_name = env_any("STEAM_ACCOUNT_NAME", default=profile_id)
        accounts.append(Account(steam_id, profile_id, account_name))

    return accounts


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def text_or_none(value: Any) -> str | None:
    if is_blank(value):
        return None
    return str(value).strip()


def number_or_none(value: Any) -> float | None:
    if is_blank(value):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def int_key(value: Any) -> str | None:
    number = number_or_none(value)
    if number is None:
        return None
    return str(int(number))


def chunks(text: str, size: int = 2000) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def parse_date(value: Any) -> str | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if is_blank(value):
        return None

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    if text in {"此版本无成就", "未知", "无"}:
        return None

    chinese_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if chinese_match:
        year, month, day = map(int, chinese_match.groups())
        return date(year, month, day).isoformat()

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S",
        "%b %d, %Y",
        "%d %b, %Y",
        "%B %d, %Y",
        "%d %B, %Y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def timestamp_to_date(value: Any) -> str | None:
    number = number_or_none(value)
    if number is None or number <= 0:
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc).date().isoformat()


def is_steam_id64(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"\d{17}", value.strip()))


def steam_profile_path(profile_id: str) -> str:
    profile = profile_id.strip()
    if is_steam_id64(profile):
        return f"profiles/{profile}"
    return f"id/{quote(profile)}"


def format_playtime(minutes: Any) -> str:
    mins = number_or_none(minutes)
    if mins is None or mins <= 0:
        return "0"
    if mins < 60:
        return f"{int(round(mins))} 分钟"
    return f"{round(mins / 60, 2)} 小时"


class HttpClient:
    def __init__(self, timeout: int = 25, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/115.0.0.0 Safari/537.36"
                )
            }
        )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(1, self.retries + 1):
            response: requests.Response | None = None
            try:
                response = self.session.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(2 * attempt, 10)
                    LOG.warning(
                        "请求 %s %s 返回 %s，%.1f 秒后重试",
                        method.upper(),
                        url,
                        response.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                if not response.ok:
                    detail = redact_secrets(response.text[:500])
                    raise SyncError(
                        f"请求失败：{method.upper()} {url} -> "
                        f"HTTP {response.status_code}: {detail}"
                    )
                return response
            except requests.RequestException as exc:
                if attempt >= self.retries:
                    detail = (
                        response.text[:500] if response is not None else str(exc)
                    )
                    raise SyncError(
                        f"请求失败：{method.upper()} {url} -> "
                        f"{redact_secrets(detail)}"
                    ) from None
                time.sleep(min(2 * attempt, 10))
        raise SyncError(f"请求失败：{method.upper()} {url}")


class SteamClient:
    def __init__(
        self,
        http: HttpClient,
        config: Config,
        store_metadata_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.http = http
        self.config = config
        self.store_metadata_cache = store_metadata_cache or {}
        self._steam_id64_cache: dict[str, str] = {}
        self._store_network_failures = 0
        self._store_metadata_disabled = False
        self._store_cache_hits = 0
        self._store_cache_misses = 0
        self._store_fetches = 0

    def resolve_steam_id64(self, account: Account) -> str:
        raw_id = account.steam_id.strip()
        if is_steam_id64(raw_id):
            return raw_id
        if raw_id in self._steam_id64_cache:
            return self._steam_id64_cache[raw_id]

        candidates = unique_keys([raw_id, account.profile_id.strip()])
        for candidate in candidates:
            if is_steam_id64(candidate):
                self._steam_id64_cache[raw_id] = candidate
                return candidate
            try:
                response = self.http.request(
                    "GET",
                    "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
                    params={
                        "key": self.config.steam_api_key,
                        "vanityurl": candidate,
                    },
                )
            except SyncError as exc:
                LOG.warning("解析 Steam 自定义 ID 失败（%s）：%s", candidate, exc)
                continue
            payload = response.json().get("response", {})
            if payload.get("success") == 1 and payload.get("steamid"):
                steam_id64 = str(payload["steamid"])
                self._steam_id64_cache[raw_id] = steam_id64
                LOG.info("已将 Steam 自定义 ID %s 解析为 SteamID64", candidate)
                return steam_id64

        raise SyncError(
            "无法把 STEAM_USER_ID 解析为 17 位 SteamID64。"
            "请填 SteamID64，或确认自定义短链接名是否正确。"
        )

    def get_owned_games(self, account: Account) -> pd.DataFrame:
        steam_id64 = self.resolve_steam_id64(account)
        params = {
            "key": self.config.steam_api_key,
            "steamid": steam_id64,
            "include_appinfo": 1,
        }
        if self.config.include_played_free_games:
            params["include_played_free_games"] = 1
        response = self.http.request(
            "GET",
            "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
            params=params,
        )
        games = response.json().get("response", {}).get("games", [])
        df = pd.DataFrame(games)
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "appid",
                    "name",
                    "playtime_forever",
                    "rtime_last_played",
                    "img_icon_url",
                ]
            )
        keep = [
            "appid",
            "name",
            "playtime_forever",
            "rtime_last_played",
            "img_icon_url",
        ]
        for column in keep:
            if column not in df.columns:
                df[column] = None
        return df[keep]

    def get_all_games_web(self, account: Account) -> pd.DataFrame:
        if not self.config.fetch_web_library:
            return pd.DataFrame(columns=["appid", "name_web", "hours_total_web"])
        path = steam_profile_path(account.profile_id)
        url = f"https://steamcommunity.com/{path}/games?xml=1"
        try:
            response = self.http.request("GET", url, timeout=min(self.http.timeout, 10))
            root = ET.fromstring(response.content)
        except Exception as exc:
            LOG.warning(
                "无法抓取 Steam XML 游戏库（%s），已仅使用 Steam API 数据：%s",
                account.account_name,
                exc,
            )
            return pd.DataFrame(columns=["appid", "name_web", "hours_total_web"])

        rows: list[dict[str, Any]] = []
        for game_node in root.findall(".//game"):
            appid = game_node.findtext("appID")
            if not appid:
                continue
            hours_text = game_node.findtext("hoursOnRecord")
            hours = number_or_none(hours_text)
            rows.append(
                {
                    "appid": int(appid),
                    "name_web": game_node.findtext("name"),
                    "hours_total_web": hours,
                }
            )
        return pd.DataFrame(rows, columns=["appid", "name_web", "hours_total_web"])

    def merge_games(self, api_games: pd.DataFrame, web_games: pd.DataFrame) -> pd.DataFrame:
        api = api_games.copy()
        web = web_games.copy()
        api["from_api"] = True
        web["from_web"] = True

        merged = pd.merge(api, web, how="outer", on="appid")
        if merged.empty:
            return merged

        merged["from_api"] = merged["from_api"].eq(True)
        merged["from_web"] = merged["from_web"].eq(True)
        merged["name"] = merged["name"].combine_first(merged["name_web"])

        missing_api_minutes = merged["playtime_forever"].isna() & merged[
            "hours_total_web"
        ].notna()
        merged.loc[missing_api_minutes, "playtime_forever"] = (
            merged.loc[missing_api_minutes, "hours_total_web"].astype(float) * 60
        ).round()

        def source(row: pd.Series) -> str:
            if row["from_api"] and row["from_web"]:
                return "两者都有"
            if row["from_api"]:
                return "API"
            if row["from_web"]:
                return "网页"
            return "未知"

        merged["抓取来源"] = merged.apply(source, axis=1)
        merged = merged.drop_duplicates(subset=["appid"], keep="first")
        return merged.sort_values("name", na_position="last").reset_index(drop=True)

    def get_achievements(self, account: Account, appid: int) -> dict[str, Any]:
        steam_id64 = self.resolve_steam_id64(account)
        params = {
            "key": self.config.steam_api_key,
            "steamid": steam_id64,
            "appid": appid,
        }
        try:
            response = self.http.request(
                "GET",
                "https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/",
                params=params,
            )
            data = response.json().get("playerstats", {})
        except SyncError:
            return {"total": 0, "achieved": 0, "first_unlock": None}

        achievements = data.get("achievements") or []
        if not isinstance(achievements, list):
            return {"total": 0, "achieved": 0, "first_unlock": None}

        unlocked = [
            item
            for item in achievements
            if item.get("achieved") in {1, True} and number_or_none(item.get("unlocktime"))
        ]
        first_unlock = None
        if unlocked:
            first_unlock_time = min(int(item["unlocktime"]) for item in unlocked)
            first_unlock = timestamp_to_date(first_unlock_time)
        return {"total": len(achievements), "achieved": len(unlocked), "first_unlock": first_unlock}

    def get_store_info(
        self, appid: int, account: Account, api_name: str | None = None
    ) -> dict[str, Any]:
        cached = self.lookup_store_metadata(appid, account, api_name)
        if cached and not self.config.refresh_store_metadata:
            missing_fields = missing_store_metadata_fields(cached)
            if not missing_fields or not self.config.fetch_store_metadata:
                self._store_cache_hits += 1
                return cached
            LOG.debug(
                "AppID %s Notion 元数据缺失：%s",
                appid,
                ", ".join(missing_fields),
            )
            self._store_cache_misses += 1
        elif self.store_metadata_cache and not cached:
            self._store_cache_misses += 1

        if not self.config.fetch_store_metadata or self._store_metadata_disabled:
            return cached or self.empty_store_info()

        data, region, network_failed = self.fetch_appdetails(appid, "cn", "zh-cn")
        if data is None:
            data, region, us_network_failed = self.fetch_appdetails(appid, "us", "en")
            network_failed = network_failed or us_network_failed

        if data is None:
            if network_failed:
                self._store_network_failures += 1
                if self._store_network_failures >= self.config.store_failure_limit:
                    self._store_metadata_disabled = True
                    LOG.warning(
                        "Steam 商店接口连续失败 %s 次，后续本次运行将跳过商店元数据",
                        self._store_network_failures,
                    )
            return cached or self.empty_store_info(region=region or "未知")

        html_info = self.fetch_store_html_info(appid)

        content_type = {
            "game": "游戏本体",
            "dlc": "DLC",
            "music": "原声音轨",
            "demo": "试玩版",
        }.get(str(data.get("type", "")).lower(), "其它")
        tags = html_info.get("tags") or [
            str(genre.get("description", "")).strip()
            for genre in data.get("genres") or []
            if str(genre.get("description", "")).strip()
        ]
        cover_url = (
            data.get("header_image")
            or data.get("capsule_image")
            or data.get("capsule_imagev5")
            or self.get_cover_url(appid)
        )

        self._store_fetches += 1
        return {
            "store_name": data.get("name"),
            "cn_title": html_info.get("cn_title") or data.get("name"),
            "description": html_info.get("description")
            or data.get("short_description")
            or "",
            "developers": ", ".join(data.get("developers") or []),
            "publishers": ", ".join(data.get("publishers") or []),
            "release_date": parse_date((data.get("release_date") or {}).get("date")),
            "tags": tags,
            "content_type": content_type,
            "region": region,
            "cover_url": cover_url,
        }

    def lookup_store_metadata(
        self, appid: int, account: Account, api_name: str | None
    ) -> dict[str, Any] | None:
        if not self.store_metadata_cache:
            return None
        for key in metadata_lookup_keys(
            appid=appid,
            title=api_name,
            account_name=account.account_name,
            use_account_field=self.config.use_account_field,
        ):
            cached = self.store_metadata_cache.get(key)
            if cached:
                return cached
        return None

    def empty_store_info(self, region: str = "未知") -> dict[str, Any]:
        return {
            "store_name": None,
            "cn_title": None,
            "description": "",
            "developers": "",
            "publishers": "",
            "release_date": None,
            "tags": [],
            "content_type": "游戏本体",
            "region": region,
        }

    def fetch_appdetails(
        self, appid: int, country: str, language: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        try:
            response = self.http.request(
                "GET",
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "cc": country, "l": language},
                timeout=min(self.http.timeout, 10),
            )
            payload = response.json().get(str(appid), {})
        except SyncError as exc:
            LOG.warning("AppID %s 商店 API 请求失败：%s", appid, exc)
            return None, country.upper(), True
        if payload.get("success") and payload.get("data"):
            return payload["data"], country.upper(), False
        return None, country.upper(), False

    def fetch_store_html_info(self, appid: int) -> dict[str, Any]:
        url = f"https://store.steampowered.com/app/{appid}/?l=schinese"
        headers = {
            "Cookie": (
                "birthtime=568022401; lastagecheckage=1-January-1990; "
                "wants_mature_content=1"
            )
        }
        try:
            response = self.http.request(
                "GET",
                url,
                headers=headers,
                timeout=min(self.http.timeout, 10),
            )
        except SyncError:
            return {"cn_title": None, "description": "", "tags": []}

        soup = BeautifulSoup(response.text, "html.parser")
        title_node = soup.select_one(".apphub_AppName")
        description_node = soup.select_one(".game_description_snippet")
        tag_nodes = soup.select(".glance_tags.popular_tags a.app_tag") or soup.select(
            "a.app_tag"
        )
        tags = [
            tag.get_text(strip=True)
            for tag in tag_nodes[: self.config.top_tags]
            if tag.get_text(strip=True)
        ]
        return {
            "cn_title": title_node.get_text(strip=True) if title_node else None,
            "description": (
                description_node.get_text(strip=True) if description_node else ""
            ),
            "tags": tags,
        }

    def get_cover_url(self, appid: int) -> str:
        base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}"
        candidates = [
            f"{base}/capsule_616x353.jpg",
            f"{base}/header.jpg",
        ]
        if not self.config.verify_cover_urls:
            return candidates[0]
        for url in candidates:
            try:
                response = self.http.session.head(url, timeout=5, allow_redirects=True)
                if response.status_code == 200:
                    return url
            except requests.RequestException:
                continue
        return candidates[-1]

    def build_dataframe(self) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        for account in self.config.accounts:
            LOG.info("开始抓取账号 %s", account.account_name)
            api_games = self.get_owned_games(account)
            web_games = self.get_all_games_web(account)
            games = self.merge_games(api_games, web_games)
            LOG.info(
                "账号 %s 合并后共 %s 个游戏", account.account_name, len(games)
            )

            for index, game in games.iterrows():
                appid = int(game["appid"])
                name = text_or_none(game.get("name")) or f"App {appid}"
                LOG.info("[%s/%s] 处理 %s", index + 1, len(games), name)
                records.append(self.build_record(account, game))
                if self.config.request_delay:
                    time.sleep(self.config.request_delay)

        if self.store_metadata_cache:
            LOG.info(
                "商店元数据缓存：命中 %s，缺失/不完整 %s，实际抓取 %s",
                self._store_cache_hits,
                self._store_cache_misses,
                self._store_fetches,
            )
        return pd.DataFrame(records)

    def build_record(self, account: Account, game: pd.Series) -> dict[str, Any]:
        appid = int(game["appid"])
        api_name = text_or_none(game.get("name"))
        store = self.get_store_info(appid, account, api_name)
        achievements = self.get_achievements(account, appid)

        minutes = number_or_none(game.get("playtime_forever"))
        hours = round(minutes / 60, 2) if minutes is not None else None
        title = store.get("cn_title") or store.get("store_name") or api_name or f"App {appid}"

        total_achievements = achievements["total"]
        achieved_achievements = achievements["achieved"]
        completion = (
            round(achieved_achievements / total_achievements, 4)
            if total_achievements
            else None
        )
        content_type = store.get("content_type") or "其它"

        record = {
            "游戏名称": title,
            "游戏ID": appid,
            "商店链接": f"https://store.steampowered.com/app/{appid}",
            "游玩时间": format_playtime(minutes),
            "总时长小时": hours,
            "最后游玩": timestamp_to_date(game.get("rtime_last_played")),
            "成就总数": total_achievements,
            "已解锁成就": achieved_achievements,
            "完成度": completion,
            "首个成就时间": achievements["first_unlock"],
            "开发商": store.get("developers"),
            "发行商": store.get("publishers"),
            "发售日期": store.get("release_date"),
            "商店标签": store.get("tags") or [],
            "数据源": "Steam",
            "抓取来源": game.get("抓取来源"),
            "内容类型": content_type,
            "封面": store.get("cover_url") or self.get_cover_url(appid),
            "所属账户": account.account_name,
            "游戏简介": store.get("description") or "",
        }
        record["游玩状态"] = get_game_status(
            record,
            abandoned_days=self.config.abandoned_days,
        )
        return record


class NotionClient:
    def __init__(self, http: HttpClient, config: Config) -> None:
        self.http = http
        self.config = config

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.notion_api_key}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        }

    def retrieve_database(self) -> dict[str, Any]:
        url = f"https://api.notion.com/v1/databases/{self.config.notion_database_id}"
        return self.http.request("GET", url, headers=self.headers).json()

    def update_database_schema(self, properties: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.notion.com/v1/databases/{self.config.notion_database_id}"
        return self.http.request(
            "PATCH",
            url,
            headers=self.headers,
            json={"properties": properties},
        ).json()

    def ensure_database_schema(
        self,
        db_props: dict[str, str],
        title_prop: str,
        field_defs: list[FieldDef],
    ) -> dict[str, str]:
        missing_schemas: dict[str, Any] = {}

        for field in field_defs:
            if not field.auto_create:
                continue
            if field.notion_type == "title":
                continue
            if field.notion_name in db_props:
                continue
            schema = notion_property_schema(field.notion_name, field.notion_type)
            if schema:
                missing_schemas[field.notion_name] = schema

        if (
            self.config.mark_uploaded
            and "已上传" not in db_props
            and self.config.schema == "cn"
        ):
            missing_schemas["已上传"] = {"checkbox": {}}

        if not missing_schemas:
            return db_props

        LOG.info("Notion 缺少字段，自动创建：%s", ", ".join(missing_schemas))
        new_props = dict(db_props)
        for name, schema in missing_schemas.items():
            try:
                database = self.update_database_schema({name: schema})
            except SyncError:
                if name == "游玩状态" and "status" in schema:
                    LOG.warning("创建 status 字段失败，改用 select 字段：%s", name)
                    database = self.update_database_schema(
                        {name: status_as_select_schema()}
                    )
                else:
                    raise
            new_props = {
                prop_name: meta.get("type", "")
                for prop_name, meta in database.get("properties", {}).items()
            }
        if title_prop not in new_props:
            new_props[title_prop] = "title"
        return new_props

    def query_database(self, start_cursor: str | None = None) -> dict[str, Any]:
        url = (
            f"https://api.notion.com/v1/databases/"
            f"{self.config.notion_database_id}/query"
        )
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self.http.request("POST", url, headers=self.headers, json=body).json()

    def prepare_sync_context(self) -> NotionSyncContext:
        database = self.retrieve_database()
        raw_props = database.get("properties", {})
        db_props = {name: meta.get("type", "") for name, meta in raw_props.items()}
        title_prop = find_title_property(db_props)
        if not title_prop:
            raise SyncError("Notion 数据库中没有 title 属性")

        field_defs = FIELD_DEFS_CN if self.config.schema == "cn" else FIELD_DEFS_LEGACY
        db_props = self.ensure_database_schema(db_props, title_prop, field_defs)
        existing_pages, key_property, store_metadata_cache = self.fetch_existing_pages(
            db_props,
            title_prop,
            field_defs,
        )
        LOG.info("Notion 已有记录 %s 条", len(existing_pages))
        if store_metadata_cache:
            LOG.info("已从 Notion 读取商店元数据缓存 %s 条", len(store_metadata_cache))
        return NotionSyncContext(
            db_props=db_props,
            title_prop=title_prop,
            field_defs=field_defs,
            existing_pages=existing_pages,
            key_property=key_property,
            store_metadata_cache=store_metadata_cache,
        )

    def fetch_existing_pages(
        self,
        db_props: dict[str, str],
        title_prop: str,
        field_defs: list[FieldDef],
    ) -> tuple[dict[str, str], str | None, dict[str, dict[str, Any]]]:
        key_property = choose_key_property(self.config.schema, db_props)
        account_property = choose_account_property(db_props) if self.config.use_account_field else None
        existing: dict[str, str] = {}
        store_metadata_cache: dict[str, dict[str, Any]] = {}
        cursor = None

        while True:
            payload = self.query_database(cursor)
            for page in payload.get("results", []):
                props = page.get("properties", {})
                keys = page_keys_from_props(
                    props,
                    title_prop=title_prop,
                    key_property=key_property,
                    account_property=account_property,
                    use_account_field=self.config.use_account_field,
                )
                for key in keys:
                    existing[key] = page["id"]
                cached_store = cached_store_info_from_props(
                    props,
                    title_prop=title_prop,
                    field_defs=field_defs,
                )
                if cached_store:
                    for key in keys:
                        store_metadata_cache.setdefault(key, cached_store)
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")
        return existing, key_property, store_metadata_cache

    def create_page(self, properties: dict[str, Any], record: dict[str, Any]) -> None:
        body: dict[str, Any] = {
            "parent": {"database_id": self.config.notion_database_id},
            "properties": properties,
        }
        self.add_page_media(body, record)
        self.http.request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=self.headers,
            json=body,
        )

    def update_page(
        self, page_id: str, properties: dict[str, Any], record: dict[str, Any]
    ) -> None:
        body: dict[str, Any] = {"properties": properties}
        self.add_page_media(body, record)
        self.http.request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=self.headers,
            json=body,
        )

    def add_page_media(self, body: dict[str, Any], record: dict[str, Any]) -> None:
        cover = text_or_none(record.get("封面"))
        if self.config.set_page_cover and cover:
            body["cover"] = {"type": "external", "external": {"url": cover}}
        if self.config.set_page_icon:
            body["icon"] = {"type": "external", "external": {"url": STEAM_LOGO_URL}}

    def sync_dataframe(
        self, df: pd.DataFrame, context: NotionSyncContext | None = None
    ) -> None:
        context = context or self.prepare_sync_context()
        db_props = context.db_props
        title_prop = context.title_prop
        field_defs = context.field_defs
        existing_pages = context.existing_pages
        key_property = context.key_property

        created = 0
        updated = 0
        skipped = 0
        failed = 0

        for index, record in enumerate(df.to_dict("records"), start=1):
            display_name = text_or_none(record.get("游戏名称")) or "未命名游戏"

            skip_reason = skip_record_reason(record, self.config)
            if skip_reason:
                skipped += 1
                LOG.info(
                    "[%s/%s] 跳过 %s：%s",
                    index,
                    len(df),
                    display_name,
                    skip_reason,
                )
                continue

            keys = record_keys(
                record,
                key_property=key_property,
                use_account_field=self.config.use_account_field,
            )
            page_id = next(
                (existing_pages[key] for key in keys if key in existing_pages),
                None,
            )
            if page_id and not self.config.enable_item_update:
                skipped += 1
                LOG.info("[%s/%s] 已存在，跳过 %s", index, len(df), display_name)
                continue

            properties = build_notion_properties(
                record=record,
                db_props=db_props,
                title_prop=title_prop,
                field_defs=field_defs,
                config=self.config,
                is_update=bool(page_id),
                key_property=key_property,
            )
            if not properties:
                skipped += 1
                LOG.warning("[%s/%s] 没有可写入字段，跳过 %s", index, len(df), display_name)
                continue

            try:
                if page_id:
                    self.update_page(page_id, properties, record)
                    updated += 1
                    LOG.info("[%s/%s] 更新 %s", index, len(df), display_name)
                else:
                    self.create_page(properties, record)
                    created += 1
                    LOG.info("[%s/%s] 新增 %s", index, len(df), display_name)
            except SyncError as exc:
                failed += 1
                LOG.error("[%s/%s] 写入失败 %s：%s", index, len(df), display_name, exc)

            if self.config.request_delay:
                time.sleep(self.config.request_delay)

        LOG.info(
            "同步完成：新增 %s，更新 %s，跳过 %s，失败 %s",
            created,
            updated,
            skipped,
            failed,
        )
        if failed:
            raise SyncError(f"有 {failed} 条记录写入失败")


def get_game_status(record: dict[str, Any], abandoned_days: int = 180) -> str | None:
    content_type = text_or_none(record.get("内容类型")) or ""
    if content_type != "游戏本体":
        return None

    hours = number_or_none(record.get("总时长小时")) or 0
    last_played = parse_date(record.get("最后游玩"))
    first_achievement = parse_date(record.get("首个成就时间"))
    achievements = number_or_none(record.get("已解锁成就")) or 0
    total_achievements = number_or_none(record.get("成就总数"))

    if hours == 0 and achievements == 0 and not first_achievement:
        return "未开始"

    if not total_achievements or total_achievements <= 0:
        return status_from_last_played(
            last_played,
            hours,
            abandoned_days=abandoned_days,
        )

    if achievements >= total_achievements:
        return "全成就"

    return status_from_last_played(
        last_played,
        hours,
        abandoned_days=abandoned_days,
    )


def status_from_last_played(
    last_played: str | None,
    hours: float,
    abandoned_days: int = 180,
) -> str:
    if last_played:
        days_since = (date.today() - datetime.fromisoformat(last_played).date()).days
        if days_since <= 30:
            return "游玩中"
        if days_since <= abandoned_days:
            return "暂搁置"
        return "已弃坑"
    if hours > 0:
        return "暂搁置"
    return "未开始"


def recently_played(last_played: str | None, days: int) -> bool:
    if not last_played:
        return False
    return (date.today() - datetime.fromisoformat(last_played).date()).days <= days


def skip_record_reason(record: dict[str, Any], config: Config) -> str | None:
    if not text_or_none(record.get("游戏名称")):
        return "缺少游戏名称"
    content_type = text_or_none(record.get("内容类型")) or "其它"
    skipped_content_types = {"DLC", "试玩版", "原声音轨"}
    if config.sync_main_games_only and content_type in skipped_content_types:
        return f"内容类型为 {content_type}，SYNC_MAIN_GAMES_ONLY=true"
    if not config.enable_filter:
        return None
    playtime = number_or_none(record.get("总时长小时")) or 0
    achievements = number_or_none(record.get("成就总数")) or 0
    last_played = parse_date(record.get("最后游玩"))
    if playtime < 0.1 and achievements < 1:
        return "ENABLE_FILTER=true，游玩时长低于 0.1 小时且无成就"
    if achievements < 1 and playtime < 6 and not recently_played(last_played, 365 * 6):
        return "ENABLE_FILTER=true，游玩时长低于 6 小时、无成就且长期未玩"
    return None


def find_title_property(db_props: dict[str, str]) -> str | None:
    for name, prop_type in db_props.items():
        if prop_type == "title":
            return name
    return None


def choose_key_property(schema: str, db_props: dict[str, str]) -> str | None:
    candidates = (
        ["游戏ID", "appid", "AppID", "应用ID"]
        if schema == "cn"
        else ["appid", "游戏ID", "AppID"]
    )
    for candidate in candidates:
        if candidate in db_props:
            return candidate
    return None


def choose_account_property(db_props: dict[str, str]) -> str | None:
    for candidate in ("所属账户", "account", "账户"):
        if candidate in db_props:
            return candidate
    return None


def notion_property_schema(name: str, notion_type: str) -> dict[str, Any] | None:
    if notion_type == "title":
        return None
    if notion_type == "rich_text":
        return {"rich_text": {}}
    if notion_type == "number":
        return {"number": {"format": "number"}}
    if notion_type == "date":
        return {"date": {}}
    if notion_type == "url":
        return {"url": {}}
    if notion_type == "files":
        return {"files": {}}
    if notion_type == "checkbox":
        return {"checkbox": {}}
    if notion_type == "multi_select":
        return {"multi_select": {"options": []}}
    if notion_type == "select":
        return {"select": {"options": SELECT_OPTIONS.get(name, [])}}
    if notion_type == "status":
        return {"status": {"options": STATUS_OPTIONS}}
    return None


def status_as_select_schema() -> dict[str, Any]:
    return {
        "select": {
            "options": [
                {
                    "name": option["name"],
                    "color": option["color"],
                }
                for option in STATUS_OPTIONS
            ]
        }
    }


def extract_prop_value(prop: dict[str, Any]) -> Any:
    prop_type = prop.get("type")
    value = prop.get(prop_type)
    if prop_type in {"title", "rich_text"}:
        return "".join(item.get("plain_text", "") for item in value or [])
    if prop_type in {"select", "status"}:
        return (value or {}).get("name")
    if prop_type == "multi_select":
        return [item.get("name") for item in value or [] if item.get("name")]
    if prop_type == "files":
        files = value or []
        for item in files:
            file_type = item.get("type")
            url = (item.get(file_type) or {}).get("url")
            if url:
                return url
        return None
    if prop_type == "number":
        return value
    if prop_type == "date":
        return (value or {}).get("start")
    if prop_type == "url":
        return value
    return None


def cached_store_info_from_props(
    props: dict[str, Any],
    title_prop: str,
    field_defs: list[FieldDef],
) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    wanted = {
        "游戏名称",
        "封面",
        "开发商",
        "发行商",
        "发售日期",
        "商店标签",
        "内容类型",
        "游戏简介",
    }
    for field in field_defs:
        if field.record_key not in wanted:
            continue
        prop_name = field.notion_name
        if field.notion_type == "title" and prop_name not in props:
            prop_name = title_prop
        if prop_name in props:
            values[field.record_key] = extract_prop_value(props[prop_name])

    store = {
        "store_name": text_or_none(values.get("游戏名称")),
        "cn_title": text_or_none(values.get("游戏名称")),
        "description": text_or_none(values.get("游戏简介")) or "",
        "developers": text_or_none(values.get("开发商")) or "",
        "publishers": text_or_none(values.get("发行商")) or "",
        "release_date": parse_date(values.get("发售日期")),
        "tags": normalize_tags(values.get("商店标签")),
        "content_type": text_or_none(values.get("内容类型")) or "游戏本体",
        "region": "Notion",
        "cover_url": text_or_none(values.get("封面")),
    }
    if cached_store_info_has_any(store):
        return store
    return None


def cached_store_info_has_any(store: dict[str, Any]) -> bool:
    return any(
        [
            text_or_none(store.get("store_name")),
            text_or_none(store.get("cn_title")),
            text_or_none(store.get("description")),
            text_or_none(store.get("developers")),
            text_or_none(store.get("publishers")),
            parse_date(store.get("release_date")),
            normalize_tags(store.get("tags")),
            text_or_none(store.get("cover_url")),
        ]
    )


def missing_store_metadata_fields(store: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not text_or_none(store.get("cover_url")):
        missing.append("封面")
    if not normalize_tags(store.get("tags")):
        missing.append("商店标签")
    if not (
        text_or_none(store.get("developers"))
        or text_or_none(store.get("publishers"))
    ):
        missing.append("开发商/发行商")
    if not text_or_none(store.get("description")):
        missing.append("游戏简介")
    if not text_or_none(store.get("content_type")):
        missing.append("内容类型")
    return missing


def unique_keys(keys: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def page_keys_from_props(
    props: dict[str, Any],
    title_prop: str,
    key_property: str | None,
    account_property: str | None,
    use_account_field: bool,
) -> list[str]:
    key_value = None
    if key_property and key_property in props:
        key_value = int_key(extract_prop_value(props[key_property]))
    title_value = None
    if title_prop in props:
        title_value = text_or_none(extract_prop_value(props[title_prop]))
    account = None
    if use_account_field and account_property and account_property in props:
        account = text_or_none(extract_prop_value(props[account_property])) or "未知"

    keys: list[str | None] = []
    for base in unique_keys([key_value, title_value]):
        if use_account_field:
            if account:
                keys.append(f"{base}_{account}")
            keys.append(f"{base}_未知")
        keys.append(base)
    return unique_keys(keys)


def metadata_lookup_keys(
    appid: int,
    title: str | None,
    account_name: str,
    use_account_field: bool,
) -> list[str]:
    record = {
        "游戏ID": appid,
        "游戏名称": title,
        "所属账户": account_name,
    }
    return record_keys(
        record,
        key_property="游戏ID",
        use_account_field=use_account_field,
    )


def record_keys(
    record: dict[str, Any],
    key_property: str | None,
    use_account_field: bool,
) -> list[str]:
    key_value = int_key(record.get("游戏ID")) if key_property else None
    title_value = text_or_none(record.get("游戏名称"))
    account = text_or_none(record.get("所属账户")) or "未知"

    keys: list[str | None] = []
    for base in unique_keys([key_value, title_value]):
        if use_account_field:
            keys.append(f"{base}_{account}")
            keys.append(f"{base}_未知")
        keys.append(base)
    return unique_keys(keys)


def build_notion_properties(
    record: dict[str, Any],
    db_props: dict[str, str],
    title_prop: str,
    field_defs: list[FieldDef],
    config: Config,
    is_update: bool,
    key_property: str | None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    missing: list[str] = []

    for field in field_defs:
        target_name = field.notion_name
        if target_name not in db_props and field.notion_type == "title":
            target_name = title_prop
        if target_name not in db_props:
            missing.append(field.notion_name)
            continue

        value = record.get(field.record_key)
        prop = serialize_notion_value(value, field.notion_type, db_props[target_name])
        if prop is not None:
            properties[target_name] = prop

    if config.mark_uploaded and "已上传" in db_props and db_props["已上传"] == "checkbox":
        properties["已上传"] = {"checkbox": True}

    allowed_fields = config.update_fields if is_update else config.insert_fields
    if allowed_fields:
        mandatory = {title_prop}
        if key_property:
            mandatory.add(key_property)
        account_property = choose_account_property(db_props)
        if config.use_account_field and account_property:
            mandatory.add(account_property)
        allowed = set(allowed_fields)
        if not is_update and config.update_fields:
            allowed.update(config.update_fields)
        allowed.update(mandatory)
        properties = {name: value for name, value in properties.items() if name in allowed}

    source = text_or_none(record.get("抓取来源"))
    if config.skip_playtime_web and source == "网页":
        properties = {
            name: value
            for name, value in properties.items()
            if name not in WEB_PLAYTIME_FIELDS
        }

    if missing:
        LOG.debug("Notion 数据库缺少字段，已自动跳过：%s", ", ".join(sorted(set(missing))))
    return properties


def serialize_notion_value(
    value: Any, preferred_type: str, actual_type: str
) -> dict[str, Any] | None:
    target_type = actual_type or preferred_type

    if target_type == "title":
        text = text_or_none(value)
        if text is None:
            return None
        return {"title": [{"type": "text", "text": {"content": text[:2000]}}]}

    if target_type == "rich_text":
        text = text_or_none(value)
        if text is None:
            return None
        return {
            "rich_text": [
                {"type": "text", "text": {"content": chunk}}
                for chunk in chunks(text)
            ]
        }

    if target_type == "number":
        number = number_or_none(value)
        if number is None:
            return None
        return {"number": number}

    if target_type == "date":
        iso_date = parse_date(value)
        if iso_date is None:
            return None
        return {"date": {"start": iso_date}}

    if target_type == "url":
        url = text_or_none(value)
        if not url or not url.startswith(("http://", "https://")):
            return None
        return {"url": url}

    if target_type == "files":
        url = text_or_none(value)
        if not url or not url.startswith(("http://", "https://")):
            return None
        return {
            "files": [
                {
                    "type": "external",
                    "name": "Steam Header",
                    "external": {"url": url},
                }
            ]
        }

    if target_type == "multi_select":
        tags = normalize_tags(value)
        if not tags:
            return None
        return {"multi_select": [{"name": tag[:100]} for tag in tags[:100]]}

    if target_type in {"select", "status"}:
        text = text_or_none(value)
        if text is None:
            return None
        return {target_type: {"name": text[:100]}}

    if target_type == "checkbox":
        return {"checkbox": bool(value)}

    return None


def normalize_tags(value: Any) -> list[str]:
    if is_blank(value):
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,，、|/；;]", str(value))
    seen: set[str] = set()
    tags: list[str] = []
    for item in items:
        tag = str(item).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def export_dataframe(df: pd.DataFrame, config: Config) -> None:
    if config.export_csv_path:
        df.to_csv(config.export_csv_path, index=False, encoding="utf-8-sig")
        LOG.info("已导出 CSV：%s", config.export_csv_path)
    if config.export_xlsx_path:
        df.to_excel(config.export_xlsx_path, index=False)
        LOG.info("已导出 Excel：%s", config.export_xlsx_path)


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Steam library data, build a dataframe, and upsert it to Notion."
    )
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和导出，不写入 Notion")
    parser.add_argument("--schema", choices=["cn", "legacy"], help="Notion 字段预设")
    parser.add_argument("--export-csv", help="导出 CSV 的路径")
    parser.add_argument("--export-xlsx", help="导出 Excel 的路径")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.debug)
    load_dotenv()
    config = Config.from_env(args)
    LOG.info(
        "SteamShelf 启动：账号 %s 个，dry_run=%s，抓商店=%s，强制刷新商店=%s，抓网页库=%s",
        len(config.accounts),
        config.dry_run,
        config.fetch_store_metadata,
        config.refresh_store_metadata,
        config.fetch_web_library,
    )

    http = HttpClient(timeout=config.http_timeout, retries=config.http_retries)
    notion = None
    notion_context = None
    if not config.dry_run:
        notion = NotionClient(http, config)
        notion_context = notion.prepare_sync_context()

    steam = SteamClient(
        http,
        config,
        store_metadata_cache=(
            notion_context.store_metadata_cache if notion_context else None
        ),
    )
    df = steam.build_dataframe()
    LOG.info("DataFrame 构建完成：%s 行，%s 列", len(df), len(df.columns))
    export_dataframe(df, config)

    if config.dry_run:
        LOG.info("DRY_RUN=true，已跳过 Notion 写入")
        return

    if notion is None:
        notion = NotionClient(http, config)
    notion.sync_dataframe(df, notion_context)


if __name__ == "__main__":
    run()
