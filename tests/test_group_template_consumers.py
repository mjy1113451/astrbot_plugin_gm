"""按群模板在业务层生效，且不影响继承全局配置的群。"""

import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_config_store import _Config, _template_entry, make_store

if "astrbot.api.event" not in sys.modules:
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    sys.modules["astrbot.api.event"] = event

from cop.history import HistoryService
from cop.moderation import ModerationService, VoiceModeration
from cop.runtime import RuntimeState
from cop.stats import StatsService


class GroupTemplateConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_scope_honors_template_and_bool_runtime_override(self):
        store = make_store(self, _Config(
            enabled_groups=["100"],
            group_overrides_templates=[
                _template_entry("100", enabled_groups="off"),
                _template_entry("200", enabled_groups="on"),
            ],
        ))
        service = ModerationService(
            config_store=store, onebot_api=None, stats=None, permissions=None,
            message_parse=None, runtime=None, text_moderation=None,
            image_moderation=None, voice_moderation=None,
        )
        self.assertFalse(service._is_group_monitoring_enabled("100"))
        self.assertTrue(service._is_group_monitoring_enabled("200"))
        self.assertFalse(service._is_group_monitoring_enabled("300"))
        store.set_group_override("100", "enabled_groups", True)
        self.assertTrue(service._is_group_monitoring_enabled("100"))

    async def test_history_and_ranking_use_the_configured_group_limits(self):
        store = make_store(self, _Config(
            max_message_history=5, rank_top_n=3,
            group_overrides_templates=[_template_entry("100", max_message_history=2, rank_top_n=1)],
        ))
        counts = {"10001": 3, "10002": 2, "10003": 1}
        runtime = RuntimeState(
            stats={"groups": {gid: {"messages": counts} for gid in ("100", "200")}},
            reports={}, max_history=5,
        )
        stats = StatsService(runtime=runtime, config_store=store, onebot_api=None, message_parse=None)
        history = HistoryService(runtime=runtime, config_store=store, onebot_api=None, message_parse=None)
        for gid in ("100", "200"):
            for msg_id in (1, 2, 3):
                history._add_message_to_history(gid, msg_id, "测试消息", "10001", "测试")
        self.assertEqual(len(await history._get_history_snapshot(None, "100")), 2)
        self.assertEqual(len(await history._get_history_snapshot(None, "200")), 3)
        self.assertEqual(stats.get_rank("100"), [("10001", 3)])
        self.assertEqual(stats.get_rank("200"), list(counts.items()))
        store.set_group_override("100", "max_message_history", 1)
        self.assertEqual(len(await history._get_history_snapshot(None, "100")), 1)
        self.assertEqual(len(await history._get_history_snapshot(None, "200")), 3)

    async def test_per_group_stt_provider_is_looked_up(self):
        store = make_store(self, _Config(
            voice_check_provider_id="shared-stt",
            group_overrides_templates=[_template_entry("100", voice_check_provider_id="local-stt")],
        ))
        providers = {
            name: types.SimpleNamespace(get_text=AsyncMock(return_value=name))
            for name in ("shared-stt", "local-stt")
        }
        manager = types.SimpleNamespace(get_provider_by_id=AsyncMock(side_effect=providers.get))
        voice = VoiceModeration(config_store=store, context=types.SimpleNamespace(provider_manager=manager))
        provider_module = types.ModuleType("astrbot.core.provider.entities")
        provider_module.ProviderType = types.SimpleNamespace(SPEECH_TO_TEXT="stt")
        with patch.dict(sys.modules, {provider_module.__name__: provider_module}):
            for group_id, expected in (("100", "local-stt"), ("200", "shared-stt")):
                self.assertEqual(await voice._recognize_audio_url(None, "sample.ogg", group_id), expected)
                providers[expected].get_text.assert_awaited_once_with("sample.ogg")
        self.assertEqual([call.args for call in manager.get_provider_by_id.await_args_list],
                         [("local-stt",), ("shared-stt",)])


if __name__ == "__main__":
    unittest.main()
