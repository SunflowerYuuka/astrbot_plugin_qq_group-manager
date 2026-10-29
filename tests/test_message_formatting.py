import asyncio
import importlib.util
import json
import sys
import time
import types
from pathlib import Path


def _load_plugin_module():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    class DummyFilter:
        class EventMessageType:
            ALL = "ALL"

        def command(self, *_args, **_kwargs):
            return lambda function: function

        def event_message_type(self, *_args, **_kwargs):
            return lambda function: function

    class DummyStar:
        def __init__(self, context=None):
            self.context = context

    class DummyStarTools:
        @staticmethod
        def get_data_dir():
            return Path.cwd()

    event.filter = DummyFilter()
    event.AstrMessageEvent = object
    star.Context = object
    star.Star = DummyStar
    star.StarTools = DummyStarTools
    star.register = lambda *_args, **_kwargs: lambda cls: cls
    api.logger = types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )

    module_name = "qq_group_manager_under_test"
    module_path = Path(__file__).parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_module()


def make_manager():
    manager = plugin.GroupManager.__new__(plugin.GroupManager)
    manager.config = {}
    manager.pending_invites = {}
    manager.member_card_cache = {}
    manager._member_card_cache_lock = asyncio.Lock()
    manager._member_card_cache_enabled = False
    manager._member_card_cache_task = None
    manager._member_card_cache_dirty = False
    manager._background_tasks = set()
    manager._timeouts_started = False
    manager._timeout_tasks = {}
    manager._save_pending = lambda: None
    manager._save_member_card_cache = lambda: None
    return manager


class FakeAPI:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action in self.fail_on:
            raise RuntimeError(f"{action} failed")
        if action == "get_group_list":
            return {"data": [{"group_id": 1001}, {"group_id": 1002}]}
        if action == "get_group_member_list":
            members = {
                1001: [{"user_id": 123456, "card": "群内小明", "nickname": "QQ小明"}],
                1002: [{"user_id": 789012, "card": "群内小红", "nickname": "QQ小红"}],
            }
            return {"data": members.get(kwargs["group_id"], [])}
        if action == "get_stranger_info":
            return {"data": {"nickname": "QQ小明"}}
        return {"data": None}


class FakeBot:
    def __init__(self, api=None):
        self.api = api or FakeAPI()


class FakeEvent:
    def __init__(self, bot, sender_id="999"):
        self.bot = bot
        self._sender_id = sender_id

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text


def test_welcome_message_uses_structured_at_segment():
    assert plugin.build_at_message(123456, "欢迎加入！") == [
        {"type": "at", "data": {"qq": "123456"}},
        {"type": "text", "data": {"text": " 欢迎加入！"}},
    ]


def test_farewell_prompt_gives_llm_member_identity_and_departure_context():
    prompt = plugin.build_farewell_prompt(
        "请写一段简短送辞：{member_name}（{user_id}）",
        123456,
        1001,
        "群内小明",
    )

    assert "群内小明（123456）" in prompt
    assert "已经离开这个群聊" in prompt


def test_member_display_name_prefers_card_then_nickname_then_user_id():
    assert plugin.get_member_display_name(
        {"card": "群内小明", "nickname": "QQ小明"}, 123456
    ) == "群内小明"
    assert plugin.get_member_display_name(
        {"card": "", "nickname": "QQ小明"}, 123456
    ) == "QQ小明"
    assert plugin.get_member_display_name({}, 123456) == "123456"


def test_safe_format_keeps_unknown_placeholders_and_literal_braces():
    assert plugin.safe_format(
        "欢迎 {user_id}，JSON 示例 {\\\"a\\\": 1} 与未知 {other}",
        user_id=123,
    ) == "欢迎 123，JSON 示例 {\\\"a\\\": 1} 与未知 {other}"


def test_safe_format_does_not_replace_doubled_braces():
    assert plugin.safe_format("模板 {{user_id}} 与 {user_id}", user_id=123) == (
        "模板 {{user_id}} 与 123"
    )


def test_member_card_cache_is_disabled_by_default():
    schema_path = Path(__file__).parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["member_card_cache_enable"]["default"] is False
    assert schema["member_card_cache_refresh_interval"]["default"] == 60


def test_auto_approve_is_disabled_by_default():
    schema_path = Path(__file__).parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["auto_approve"]["default"] is False


def test_missing_auto_approve_config_does_not_approve_join_request():
    manager = make_manager()
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_add(
            FakeEvent(bot),
            {"group_id": 1001, "user_id": 123456, "flag": "flag-1"},
            {},
        )
    )

    assert not [call for call in bot.api.calls if call[0] == "set_group_add_request"]


def test_auto_approve_enabled_still_approves_join_request():
    manager = make_manager()
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_add(
            FakeEvent(bot),
            {"group_id": 1001, "user_id": 123456, "flag": "flag-1"},
            {"auto_approve": True},
        )
    )

    approve_call = [
        call for call in bot.api.calls if call[0] == "set_group_add_request"
    ][0]
    assert approve_call[1]["approve"] is True


def test_welcome_skips_bot_itself():
    manager = make_manager()

    async def fake_llm(*_args, **_kwargs):
        return "hi"

    manager._generate_llm_response = fake_llm
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_increase(
            FakeEvent(bot),
            {"group_id": 1001, "user_id": 555, "self_id": 555},
            {"welcome_enable": True, "welcome_prompt": "欢迎 {user_id}"},
        )
    )

    assert not [call for call in bot.api.calls if call[0] == "send_group_msg"]


def test_farewell_skips_kick_me():
    manager = make_manager()
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_decrease(
            FakeEvent(bot),
            {"group_id": 1001, "user_id": 555, "sub_type": "kick_me"},
            {"farewell_enable": True},
        )
    )

    assert not [call for call in bot.api.calls if call[0] == "send_group_msg"]


def test_blacklisted_group_invite_is_rejected_without_admin_notice():
    manager = make_manager()
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_invite(
            FakeEvent(bot),
            {"flag": "flag-1", "user_id": 123456, "group_id": 2002},
            {
                "filter_list_mode": "白名单",
                "filter_group_list": [1001],
                "admin_qq": "999",
            },
        )
    )

    actions = [call[0] for call in bot.api.calls]
    assert "set_group_add_request" in actions
    reject_call = [
        call for call in bot.api.calls if call[0] == "set_group_add_request"
    ][0]
    assert reject_call[1]["approve"] is False
    assert "send_private_msg" not in actions
    assert manager.pending_invites == {}


def test_approve_deletes_pending_even_if_notification_fails():
    manager = make_manager()
    manager.config = {"admin_qq": "999"}
    manager.pending_invites = {
        "abc123": {"flag": "flag-1", "user_id": 123456, "group_id": 1001}
    }
    bot = FakeBot(FakeAPI(fail_on={"send_private_msg"}))

    async def run():
        return [item async for item in manager.handle_bot_join(
            FakeEvent(bot, sender_id="999"), "abc123", "同意"
        )]

    asyncio.run(run())

    assert "abc123" not in manager.pending_invites
    assert [call[0] for call in bot.api.calls].count("set_group_add_request") == 1


def test_timeout_rejects_expired_invite_immediately():
    manager = make_manager()
    manager.pending_invites = {
        "expired": {
            "flag": "flag-1",
            "user_id": 123456,
            "group_id": 1001,
            "created_at": time.time() - plugin.INVITE_TIMEOUT_SECONDS - 10,
        }
    }
    bot = FakeBot(FakeAPI(fail_on={"send_private_msg"}))

    asyncio.run(asyncio.wait_for(manager._timeout_task("expired", bot), timeout=2))

    actions = [call[0] for call in bot.api.calls]
    assert "set_group_add_request" in actions
    assert "expired" not in manager.pending_invites


def test_provider_resolution_returns_real_default_id():
    manager = make_manager()

    class FakeContext:
        async def get_current_chat_provider_id(self, _umo):
            return "provider-1"

    manager.context = FakeContext()

    assert asyncio.run(manager._resolve_provider_ids(None, {})) == ["provider-1"]


def test_provider_resolution_never_returns_none_placeholder():
    manager = make_manager()

    class FakeContext:
        async def get_current_chat_provider_id(self, _umo):
            return ""

        def get_using_provider(self):
            return None

    manager.context = FakeContext()

    assert asyncio.run(manager._resolve_provider_ids(None, {})) == []


def test_enabling_cache_refreshes_managed_groups():
    manager = make_manager()

    asyncio.run(
        manager._refresh_member_card_cache(
            FakeBot(),
            {"filter_list_mode": "黑名单", "filter_group_list": []},
        )
    )

    assert set(manager.member_card_cache) == {"1001", "1002"}
    assert manager.member_card_cache["1001"]["123456"]["card"] == "群内小明"
    assert manager.member_card_cache["1002"]["789012"]["card"] == "群内小红"
    assert manager.member_card_cache["1001"]["123456"]["last_seen"] > 0


def test_cache_refresh_keeps_previous_data_when_one_group_fails():
    class PartiallyFailingAPI(FakeAPI):
        async def call_action(self, action, **kwargs):
            if action == "get_group_member_list" and kwargs["group_id"] == 1002:
                self.calls.append((action, kwargs))
                raise RuntimeError("temporary API failure")
            return await super().call_action(action, **kwargs)

    manager = make_manager()
    manager.member_card_cache = {
        "1002": {
            "789012": {
                "card": "旧群内小红",
                "nickname": "QQ小红",
                "last_seen": time.time(),
            }
        }
    }

    asyncio.run(
        manager._refresh_member_card_cache(
            FakeBot(PartiallyFailingAPI()),
            {"filter_list_mode": "黑名单", "filter_group_list": []},
        )
    )

    assert manager.member_card_cache["1002"]["789012"]["card"] == "旧群内小红"


def test_cache_refresh_evicts_entries_older_than_ttl():
    manager = make_manager()
    now = time.time()
    manager.member_card_cache = {
        "1001": {
            "111111": {
                "card": "过期成员",
                "nickname": "过期",
                "last_seen": now - plugin.CACHE_TTL_SECONDS - 60,
            },
            "222222": {
                "card": "近期离群",
                "nickname": "近期",
                "last_seen": now - 60,
            },
        }
    }

    asyncio.run(
        manager._refresh_member_card_cache(
            FakeBot(),
            {"filter_list_mode": "白名单", "filter_group_list": [1001]},
        )
    )

    group_cache = manager.member_card_cache["1001"]
    assert "111111" not in group_cache
    assert group_cache["222222"]["card"] == "近期离群"
    assert group_cache["123456"]["card"] == "群内小明"


def test_provider_resolution_falls_back_to_using_provider():
    manager = make_manager()

    class Meta:
        id = "provider-from-using"

    class Provider:
        def meta(self):
            return Meta()

    class FakeContext:
        async def get_current_chat_provider_id(self, _umo):
            return ""

        def get_using_provider(self):
            return Provider()

    manager.context = FakeContext()

    assert asyncio.run(manager._resolve_provider_ids(None, {})) == [
        "provider-from-using"
    ]


def test_configured_providers_keep_priority_order():
    manager = make_manager()

    class FakeContext:
        async def get_current_chat_provider_id(self, _umo):
            return "should-not-be-used"

    manager.context = FakeContext()

    assert asyncio.run(
        manager._resolve_provider_ids(
            None,
            {
                "llm_provider_id": "main",
                "fallback_provider_1": "backup1",
                "fallback_provider_2": "backup2",
            },
        )
    ) == ["main", "backup1", "backup2"]


def test_blacklist_mode_rejects_invite_for_listed_group():
    manager = make_manager()
    bot = FakeBot()

    asyncio.run(
        manager._handle_group_invite(
            FakeEvent(bot),
            {"flag": "flag-1", "user_id": 123456, "group_id": 1001},
            {
                "filter_list_mode": "黑名单",
                "filter_group_list": [1001],
                "admin_qq": "999",
            },
        )
    )

    reject_call = [
        call for call in bot.api.calls if call[0] == "set_group_add_request"
    ][0]
    assert reject_call[1]["approve"] is False
    assert manager.pending_invites == {}


def test_invite_id_is_twelve_characters():
    manager = make_manager()
    bot = FakeBot()
    manager._schedule_timeout = lambda *_args, **_kwargs: None

    asyncio.run(
        manager._handle_group_invite(
            FakeEvent(bot),
            {"flag": "flag-1", "user_id": 123456, "group_id": 1001},
            {"filter_list_mode": "黑名单", "filter_group_list": [], "admin_qq": ""},
        )
    )

    assert len(manager.pending_invites) == 1
    invite_id = next(iter(manager.pending_invites))
    assert len(invite_id) == 12


def test_startup_timeout_rebuild_schedules_pending_invites():
    manager = make_manager()
    manager.pending_invites = {
        "invite-1": {
            "flag": "flag-1",
            "user_id": 1,
            "group_id": 2,
            "created_at": time.time() - plugin.INVITE_TIMEOUT_SECONDS - 5,
        }
    }
    bot = FakeBot()

    async def run():
        await manager._ensure_pending_timeouts(bot)
        await asyncio.sleep(0)
        await asyncio.gather(*list(manager._background_tasks), return_exceptions=True)

    asyncio.run(run())

    assert "invite-1" not in manager.pending_invites


def test_approval_cancels_pending_timeout_task():
    manager = make_manager()
    manager.config = {"admin_qq": "999"}
    manager.pending_invites = {
        "abc123": {
            "flag": "flag-1",
            "user_id": 1,
            "group_id": 2,
            "created_at": time.time(),
        }
    }
    bot = FakeBot()

    async def run():
        manager._schedule_timeout("abc123", bot)
        await asyncio.sleep(0)
        async for _ in manager.handle_bot_join(
            FakeEvent(bot, sender_id="999"), "abc123", "同意"
        ):
            pass
        await asyncio.gather(*list(manager._background_tasks), return_exceptions=True)

    asyncio.run(run())

    assert "abc123" not in manager._timeout_tasks


def test_cache_refresh_keeps_concurrent_event_updates():
    manager = make_manager()

    class SlowAPI(FakeAPI):
        async def call_action(self, action, **kwargs):
            if action == "get_group_member_list" and kwargs["group_id"] == 1001:
                manager.member_card_cache.setdefault("1001", {})["999999"] = {
                    "card": "并发成员",
                    "nickname": "",
                    "last_seen": time.time(),
                }
            return await super().call_action(action, **kwargs)

    asyncio.run(
        manager._refresh_member_card_cache(
            FakeBot(SlowAPI()),
            {"filter_list_mode": "白名单", "filter_group_list": [1001]},
        )
    )

    assert manager.member_card_cache["1001"]["999999"]["card"] == "并发成员"


def test_timeout_gives_up_after_repeated_reject_failures():
    manager = make_manager()
    manager.pending_invites = {
        "expired": {
            "flag": "flag-1",
            "user_id": 123456,
            "group_id": 1001,
            "created_at": time.time() - plugin.INVITE_TIMEOUT_SECONDS - 10,
            "timeout_attempts": 2,
        }
    }
    bot = FakeBot(FakeAPI(fail_on={"set_group_add_request"}))

    asyncio.run(asyncio.wait_for(manager._timeout_task("expired", bot), timeout=2))

    assert "expired" not in manager.pending_invites


def test_load_pending_drops_malformed_records():
    import tempfile

    manager = make_manager()
    target = Path(tempfile.mkdtemp()) / "pending.json"
    target.write_text(
        json.dumps(
            {
                "good": {"flag": "f", "user_id": 1, "group_id": 2},
                "missing": {"flag": "f"},
                "not-a-dict": "bad",
            }
        ),
        encoding="utf-8",
    )
    manager.pending_file = target

    assert set(manager._load_pending()) == {"good"}


def test_cache_refresh_drops_unmanaged_groups():
    manager = make_manager()
    manager.member_card_cache = {
        "9999": {
            "111": {"card": "旧群", "nickname": "", "last_seen": time.time()}
        }
    }

    asyncio.run(
        manager._refresh_member_card_cache(
            FakeBot(),
            {"filter_list_mode": "黑名单", "filter_group_list": []},
        )
    )

    assert "9999" not in manager.member_card_cache


def test_group_list_failure_still_prunes_expired_entries():
    manager = make_manager()
    manager.member_card_cache = {
        "1001": {
            "111": {
                "card": "过期",
                "nickname": "",
                "last_seen": time.time() - plugin.CACHE_TTL_SECONDS - 60,
            },
            "222": {
                "card": "近期",
                "nickname": "",
                "last_seen": time.time() - 60,
            },
        }
    }
    bot = FakeBot(FakeAPI(fail_on={"get_group_list"}))

    asyncio.run(
        manager._refresh_member_card_cache(
            bot, {"filter_list_mode": "黑名单", "filter_group_list": []}
        )
    )

    group_cache = manager.member_card_cache["1001"]
    assert "111" not in group_cache
    assert group_cache["222"]["card"] == "近期"


def test_atomic_write_produces_valid_json_and_no_temp_file():
    import tempfile

    manager = make_manager()
    target = Path(tempfile.mkdtemp()) / "data.json"
    manager._atomic_write_json(target, {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert not list(target.parent.glob("*.tmp"))


def test_empty_cached_member_info_falls_back_to_qq_nickname():
    class NicknameOnlyAPI:
        async def call_action(self, action, **kwargs):
            assert action == "get_stranger_info"
            return {"data": {"nickname": "QQ小明"}}

    class NicknameOnlyBot:
        api = NicknameOnlyAPI()

    manager = make_manager()
    manager.member_card_cache = {
        "1001": {"123456": {"card": "", "nickname": "", "last_seen": time.time()}}
    }

    assert asyncio.run(
        manager._get_farewell_member_name(
            NicknameOnlyBot(), 1001, 123456, {"member_card_cache_enable": True}
        )
    ) == "QQ小明"
