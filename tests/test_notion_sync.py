import copy
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from sync import (
    Account,
    Config,
    FIELD_DEFS_CN,
    NotionClient,
    NotionSyncContext,
    STEAM_LOGO_URL,
    SyncError,
    build_notion_properties,
    normalize_notion_property,
)


def api_properties(properties):
    result = copy.deepcopy(properties)
    for name, prop in result.items():
        prop_type = next(iter(prop))
        prop["id"] = name
        prop["type"] = prop_type
        if prop_type in {"title", "rich_text"}:
            for text in prop[prop_type]:
                text["plain_text"] = text["text"]["content"]
                text["annotations"] = {"bold": False}
        elif prop_type in {"select", "status"} and prop[prop_type]:
            prop[prop_type].update({"id": "option-id", "color": "blue"})
        elif prop_type == "multi_select":
            for option in prop[prop_type]:
                option.update({"id": option["name"], "color": "default"})
    return result


class NotionDiffTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            steam_api_key="unused",
            notion_api_key="unused",
            notion_database_id="database-id",
            accounts=[Account("76561190000000000", "profile", "main")],
            request_delay=0.25,
        )
        self.http = Mock()
        self.http.request.return_value.json.return_value = {"id": "page-1"}
        self.client = NotionClient(self.http, self.config)
        names = {
            "游戏名称", "游戏ID", "游玩时间", "总时长/h", "游玩状态",
            "游戏封面", "商店标签", "最后游玩日期", "游戏简介",
        }
        self.fields = [field for field in FIELD_DEFS_CN if field.notion_name in names]
        self.db_props = {field.notion_name: field.notion_type for field in self.fields}
        self.db_props["已上传"] = "checkbox"
        self.record = {
            "游戏名称": "A game",
            "游戏ID": 123,
            "游玩时间": "2 小时",
            "总时长小时": 2.0,
            "游玩状态": "游玩中",
            "最后游玩": "2026-11-01",
            "内容类型": "游戏本体",
            "封面": "https://example.com/cover.jpg",
            "商店标签": ["策略", "冒险"],
            "游戏简介": "Hello world",
        }
        self.properties = build_notion_properties(
            self.record, self.db_props, "游戏名称", self.fields,
            self.config, True, "游戏ID",
        )
        self.snapshot = {
            "id": "page-1",
            "properties": api_properties(self.properties),
            "cover": {"type": "external", "external": {"url": self.record["封面"]}},
            "icon": {"type": "external", "external": {"url": STEAM_LOGO_URL}},
        }
        self.snapshot["properties"]["评分"] = {"type": "number", "number": 5}
        self.context = NotionSyncContext(
            db_props=self.db_props,
            title_prop="游戏名称",
            field_defs=self.fields,
            existing_pages={"123": "page-1", "A game": "page-1"},
            key_property="游戏ID",
            store_metadata_cache={},
            page_snapshots={"page-1": self.snapshot},
        )

    def test_normalizes_api_metadata_text_chunks_and_tag_order(self):
        comparisons = [
            (
                {"rich_text": [{"text": {"content": "Hello world"}}]},
                {"id": "x", "type": "rich_text", "rich_text": [
                    {"plain_text": "Hello "}, {"plain_text": "world"},
                ]},
            ),
            (
                {"multi_select": [{"name": "策略"}, {"name": "冒险"}]},
                {"type": "multi_select", "multi_select": [
                    {"name": "冒险", "id": "1"}, {"name": "策略", "color": "blue"},
                ]},
            ),
            ({"checkbox": True}, {"type": "checkbox", "checkbox": True, "id": "x"}),
            ({"number": 2}, {"type": "number", "number": 2.0}),
            (
                {"date": {"start": "2026-11-01"}},
                {"type": "date", "date": {"start": "2026-11-01", "end": None, "time_zone": None}},
            ),
            (
                {"files": [{"name": "Steam Header", "type": "external", "external": {"url": "https://example.com/a"}}]},
                {"type": "files", "files": [{"name": "User label", "type": "external", "external": {"url": "https://example.com/a"}}]},
            ),
            ({"status": {"name": "游玩中"}}, {"type": "status", "status": {"name": "游玩中", "id": "1", "color": "blue"}}),
        ]
        for desired, current in comparisons:
            with self.subTest(desired=desired):
                self.assertEqual(normalize_notion_property(desired), normalize_notion_property(current))

    @patch("sync.time.sleep")
    def test_unchanged_record_skips_request_and_wait(self, sleep):
        self.snapshot["properties"]["商店标签"]["multi_select"].reverse()
        self.snapshot["properties"]["游戏简介"]["rich_text"] = [
            {"plain_text": "Hello "}, {"plain_text": "world"},
        ]
        self.client.sync_dataframe(pd.DataFrame([self.record]), self.context)
        self.http.request.assert_not_called()
        sleep.assert_not_called()

    @patch("sync.time.sleep")
    def test_only_changed_status_is_patched_then_snapshot_reused(self, sleep):
        record = {**self.record, "游玩状态": "暂搁置"}
        self.client.sync_dataframe(pd.DataFrame([record, record]), self.context)
        self.http.request.assert_called_once()
        self.assertEqual(
            self.http.request.call_args.kwargs["json"],
            {"properties": {"游玩状态": {"status": {"name": "暂搁置"}}}},
        )
        sleep.assert_called_once_with(0.25)
        self.assertEqual(self.context.page_snapshots["page-1"]["properties"]["评分"]["number"], 5)

    def test_checkbox_and_file_url_changes_are_detected(self):
        self.snapshot["properties"]["已上传"]["checkbox"] = False
        self.snapshot["properties"]["游戏封面"]["files"][0]["external"]["url"] = "https://example.com/old.jpg"
        self.client.update_page("page-1", self.properties, self.record, self.snapshot)
        body = self.http.request.call_args.kwargs["json"]
        self.assertEqual(set(body), {"properties"})
        self.assertEqual(set(body["properties"]), {"已上传", "游戏封面"})

    def test_media_only_changes_do_not_resend_properties(self):
        for media in ("cover", "icon"):
            with self.subTest(media=media):
                self.http.reset_mock()
                snapshot = copy.deepcopy(self.snapshot)
                snapshot[media]["external"]["url"] = "https://example.com/old.jpg"
                self.client.update_page("page-1", self.properties, self.record, snapshot)
                body = self.http.request.call_args.kwargs["json"]
                self.assertEqual(set(body), {media})
                self.assertEqual(body[media], self.snapshot[media])

    def test_disabled_media_is_preserved(self):
        self.config.set_page_cover = False
        self.config.set_page_icon = False
        self.snapshot["cover"] = None
        self.snapshot["icon"] = {"type": "emoji", "emoji": "🎮"}
        result = self.client.update_page("page-1", self.properties, self.record, self.snapshot)
        self.assertIsNone(result)
        self.http.request.assert_not_called()

    def test_unknown_snapshot_conservatively_updates(self):
        result = self.client.update_page("page-1", self.properties, self.record)
        self.http.request.assert_called_once()
        body = self.http.request.call_args.kwargs["json"]
        self.assertEqual(body["properties"], self.properties)
        self.assertEqual(set(body), {"properties", "cover", "icon"})
        self.assertEqual(result["id"], "page-1")

    @patch("sync.time.sleep")
    def test_new_page_is_not_created_twice_in_same_run(self, sleep):
        self.context.existing_pages.clear()
        self.context.page_snapshots.clear()
        self.http.request.return_value.json.return_value = {"id": "new-page"}
        self.client.sync_dataframe(pd.DataFrame([self.record, self.record]), self.context)
        self.http.request.assert_called_once()
        self.assertEqual(self.http.request.call_args.args[0], "POST")
        self.assertEqual(self.context.existing_pages["123"], "new-page")
        self.assertIn("new-page", self.context.page_snapshots)
        sleep.assert_called_once_with(0.25)

    @patch("sync.time.sleep")
    def test_disabled_updates_still_skip_changed_record(self, sleep):
        self.config.enable_item_update = False
        self.client.sync_dataframe(
            pd.DataFrame([{**self.record, "总时长小时": 3}]), self.context,
        )
        self.http.request.assert_not_called()
        sleep.assert_not_called()

    @patch("sync.time.sleep")
    def test_failed_write_does_not_update_snapshot(self, sleep):
        self.http.request.side_effect = SyncError("request failed")
        original = copy.deepcopy(self.context.page_snapshots)
        with self.assertRaises(SyncError):
            self.client.sync_dataframe(
                pd.DataFrame([{**self.record, "游玩状态": "暂搁置"}]), self.context,
            )
        self.assertEqual(self.context.page_snapshots, original)

    def test_database_pagination_keeps_raw_page_snapshots(self):
        page_two = {"id": "page-2", "properties": {
            "游戏名称": {"type": "title", "title": [{"plain_text": "B game"}]},
            "游戏ID": {"type": "number", "number": 456},
        }}
        with patch.object(self.client, "query_database", side_effect=[
            {"results": [self.snapshot], "has_more": True, "next_cursor": "cursor"},
            {"results": [page_two], "has_more": False},
        ]) as query:
            existing, key_property, store_cache, snapshots = self.client.fetch_existing_pages(
                self.db_props, "游戏名称", self.fields,
            )
        self.assertEqual(query.call_args_list[1].args, ("cursor",))
        self.assertEqual(existing["456"], "page-2")
        self.assertEqual(key_property, "游戏ID")
        self.assertIs(snapshots["page-1"], self.snapshot)
        self.assertIs(snapshots["page-2"], page_two)
        self.assertIn("123", store_cache)


if __name__ == "__main__":
    unittest.main()
