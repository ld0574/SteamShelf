import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import Mock, patch

import pandas as pd

from sync import (
    Account, Config, FIELD_DEFS_CN, FIELD_DEFS_LEGACY, HttpClient,
    NotionSyncContext, SteamClient, SyncError, achievement_cache_from_context,
    achievement_cache_key, refresh_due,
)


class SteamCacheTests(unittest.TestCase):
    def setUp(self):
        self.account = Account("76561190000000000", "profile", "main")
        self.config = Config("unused", "unused", "database", [self.account])
        self.http = HttpClient()
        self.http.request = Mock()
        self.client = SteamClient(self.http, self.config)
        self.appid = next(appid for appid in range(100, 200) if not refresh_due(appid, 30))
        self.played = date.today() - timedelta(days=10)
        self.game = pd.Series({
            "appid": self.appid, "name": "Game", "playtime_forever": 120,
            "rtime_last_played": int(datetime.combine(self.played, time(), timezone.utc).timestamp()),
        })
        self.stats = {"total": 10, "achieved": 5, "first_unlock": "2020-01-01"}
        self.cached = {
            "hours": 2, "last_played": self.played.isoformat(), "achievements": self.stats,
        }
        self.client.achievement_cache[str(self.appid)] = self.cached

    def test_unchanged_game_reuses_stats_without_request(self):
        self.assertEqual(self.client.achievements_for_game(self.account, self.game), self.stats)
        self.http.request.assert_not_called()

    def test_changed_playtime_or_last_played_refreshes(self):
        for changes in ({"playtime_forever": 121}, {"rtime_last_played": 0}):
            with self.subTest(changes=changes), patch.object(self.client, "get_achievements", return_value=self.stats) as fetch:
                game = self.game.copy()
                for key, value in changes.items():
                    game[key] = value
                self.client.achievements_for_game(self.account, game)
                fetch.assert_called_once_with(self.account, self.appid)

    def test_recent_play_refreshes_even_if_signature_matches(self):
        self.game["rtime_last_played"] = int(datetime.now(timezone.utc).timestamp())
        self.cached["last_played"] = date.today().isoformat()
        with patch.object(self.client, "get_achievements", return_value=self.stats) as fetch:
            self.client.achievements_for_game(self.account, self.game)
        fetch.assert_called_once()

    def test_manual_and_rotating_refresh(self):
        with patch.object(self.client, "get_achievements", return_value=self.stats) as fetch:
            self.config.refresh_achievements = True
            self.client.achievements_for_game(self.account, self.game)
            self.config.refresh_achievements = False
            with patch("sync.refresh_due", return_value=True):
                self.client.achievements_for_game(self.account, self.game)
        self.assertEqual(fetch.call_count, 2)

    def test_failure_preserves_cached_values_and_omits_unknown_values(self):
        self.config.refresh_achievements = True
        with patch.object(self.client, "get_achievements", side_effect=SyncError("offline")):
            self.assertEqual(self.client.achievements_for_game(self.account, self.game), self.stats)
            self.client.achievement_cache.clear()
            result = self.client.achievements_for_game(self.account, self.game)
        self.assertEqual(result, {"total": None, "achieved": None, "first_unlock": None})

    def test_other_account_never_reuses_stats(self):
        self.config.use_account_field = True
        self.client.achievement_cache = {f"{self.appid}_other": self.cached}
        with patch.object(self.client, "get_achievements", return_value=self.stats) as fetch:
            self.client.achievements_for_game(self.account, self.game)
        fetch.assert_called_once()

    def test_no_stats_response_is_valid_zero_but_private_profile_is_unknown(self):
        self.http.request.return_value.json.return_value = {
            "playerstats": {"success": False, "error": "Requested app has no stats"},
        }
        self.assertEqual(self.client.get_achievements(self.account, self.appid)["total"], 0)
        self.http.request.return_value.json.return_value = {
            "playerstats": {"success": False, "error": "Profile is not public"},
        }
        with self.assertRaises(SyncError):
            self.client.get_achievements(self.account, self.appid)

    def test_unchanged_library_does_not_sleep_but_status_is_recalculated(self):
        # The state must cross the 30-day boundary even when achievements are cached.
        played = date.today() - timedelta(days=31)
        self.game["rtime_last_played"] = int(datetime.combine(played, time(), timezone.utc).timestamp())
        self.cached["last_played"] = played.isoformat()
        store = self.client.empty_store_info()
        with patch.object(self.client, "get_owned_games", return_value=pd.DataFrame([self.game])), \
                patch.object(self.client, "get_all_games_web", return_value=pd.DataFrame()), \
                patch.object(self.client, "merge_games", return_value=pd.DataFrame([self.game])), \
                patch.object(self.client, "get_store_info", return_value=store), \
                patch("sync.time.sleep") as sleep:
            frame = self.client.build_dataframe()
        self.assertEqual(frame.iloc[0]["游玩状态"], "暂搁置")
        self.http.request.assert_not_called()
        sleep.assert_not_called()

    def test_partial_store_metadata_waits_for_rotation_and_manual_bypasses(self):
        cached = {**self.client.empty_store_info(), "store_name": "Game"}
        self.client.store_metadata_cache[str(self.appid)] = cached
        with patch("sync.refresh_due", return_value=False), \
                patch.object(self.client, "fetch_appdetails", return_value=(None, "US", False)) as fetch:
            self.assertEqual(self.client.get_store_info(self.appid, self.account), cached)
            fetch.assert_not_called()
            self.config.refresh_store_metadata = True
            self.client.get_store_info(self.appid, self.account)
            self.assertEqual(fetch.call_count, 2)

    def test_new_game_fetches_store_and_shares_result_across_accounts(self):
        with patch("sync.refresh_due", return_value=False), \
                patch.object(self.client, "fetch_appdetails", return_value=({"name": "Game", "type": "game"}, "CN", False)) as fetch, \
                patch.object(self.client, "fetch_store_html_info", return_value={}):
            first = self.client.get_store_info(self.appid, self.account)
            second = self.client.get_store_info(self.appid, Account("76561190000000001", "other", "other"))
        self.assertIs(first, second)
        fetch.assert_called_once()

    def test_rotation_is_spread_and_every_app_is_refreshed(self):
        for interval in (7, 30):
            counts = []
            for offset in range(interval):
                today = date.today() + timedelta(days=offset)
                with patch("sync.date") as fake_date:
                    fake_date.today.return_value = today
                    counts.append(sum(refresh_due(appid, interval) for appid in range(10, 34060, 10)))
            self.assertEqual(sum(counts), 3405)
            self.assertGreater(min(counts), 3405 / interval * 0.65)
            self.assertLess(max(counts), 3405 / interval * 1.35)


class NotionAchievementCacheTests(unittest.TestCase):
    def test_extracts_zero_stats_and_separates_accounts_in_both_schemas(self):
        for fields in (FIELD_DEFS_CN, FIELD_DEFS_LEGACY):
            props = {}
            values = {
                "游戏ID": 100, "总时长小时": 0, "成就总数": 0,
                "已解锁成就": 0, "所属账户": "main",
            }
            for field in fields:
                if field.record_key in values:
                    value = values[field.record_key]
                    props[field.notion_name] = {"type": field.notion_type, field.notion_type: {"name": value} if field.notion_type == "select" else value}
            context = NotionSyncContext(
                db_props={name: prop["type"] for name, prop in props.items()},
                title_prop="title", field_defs=fields, existing_pages={},
                key_property=None, store_metadata_cache={},
                page_snapshots={"page": {"properties": props}},
            )
            with self.subTest(schema=fields[0].notion_name):
                cache = achievement_cache_from_context(context, True)
                key = achievement_cache_key(100, "main", True)
                self.assertEqual(cache[key]["achievements"]["total"], 0)
                self.assertNotIn("100_other", cache)
                total_field = next(f.notion_name for f in fields if f.record_key == "成就总数")
                props[total_field]["number"] = None
                self.assertEqual(achievement_cache_from_context(context, True), {})


class HttpRetryTests(unittest.TestCase):
    @patch("sync.time.sleep")
    def test_final_failed_attempt_does_not_sleep(self, sleep):
        http = HttpClient(retries=2)
        http.session.request = Mock(return_value=Mock(status_code=503, headers={}))
        with self.assertRaises(SyncError):
            http.request("GET", "https://example.com")
        self.assertEqual(http.session.request.call_count, 2)
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
