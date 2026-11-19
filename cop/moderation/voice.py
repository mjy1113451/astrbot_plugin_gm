"""L3b 群违规检测子域：语音转文字（#128）。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _recognize_audio_url

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：get_group_setting（voice_check_provider_id）/ config（ASR 兜底配置与超时）
- context：AstrBot Context（取 provider_manager 作内置 STT 通路）

延迟导入约束（必须保持函数内局部导入，不得提升为模块级）：
    from astrbot.core.provider.entities import ProviderType
    import aiohttp
    from openai import AsyncOpenAI
    from io import BytesIO
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ..config_store import ConfigStore


class VoiceModeration:
    """语音内容识别领域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        context=None,
    ):
        self._store = config_store
        self.config = config_store.config
        # 保留 context 以便取 provider_manager（等价原 self.context）
        self.context = context

    async def _recognize_audio_url(self, event, audio_url: str, group_id: str) -> str:
        """#128：将语音 URL 识别为文本。优先 AstrBot 内置 STT provider，回退到插件独立 ASR 配置。
        返回识别文本，失败返回空串。"""
        if not audio_url:
            return ""
        timeout = max(5, int(self.config.get("voice_check_timeout", 15) or 15))
        provider_id = str(self._store.get_group_setting(group_id, "voice_check_provider_id", "") or "").strip()
        # 1) AstrBot 内置 provider：优先用户指定 provider_id，否则用当前激活的 STT provider
        try:
            ctx = getattr(self, "context", None)
            provider_manager = getattr(ctx, "provider_manager", None) if ctx else None
            if provider_manager is not None:
                from astrbot.core.provider.entities import ProviderType  # 局部导入，避免硬依赖失败
                prov = None
                if provider_id:
                    try:
                        prov = await provider_manager.get_provider_by_id(provider_id)
                    except Exception:
                        prov = None
                if prov is None:
                    try:
                        prov = provider_manager.get_using_provider(ProviderType.SPEECH_TO_TEXT)
                    except Exception:
                        prov = None
                if prov is not None and hasattr(prov, "get_text"):
                    try:
                        text = await asyncio.wait_for(prov.get_text(audio_url), timeout=timeout)
                        if text:
                            return str(text).strip()
                    except Exception as exc:
                        logger.warning(f"AstrBot STT provider 识别失败: {exc}")
        except ImportError:
            logger.debug("未安装 astrbot.core.provider.entities，回退到插件独立 API")
        except Exception as exc:
            logger.warning(f"AstrBot STT 调用异常: {exc}")
        # 2) 插件独立 ASR API（OpenAI 兼容 /audio/transcriptions）
        endpoint = str(self.config.get("voice_asr_endpoint", "") or "").strip()
        api_key = str(self.config.get("voice_asr_api_key", "") or "").strip()
        model = str(self.config.get("voice_asr_model", "") or "").strip() or "whisper-1"
        if not endpoint:
            return ""
        try:
            import aiohttp
            from openai import AsyncOpenAI  # type: ignore
            base_url = endpoint.rstrip("/")
            if not base_url.endswith("/audio/transcriptions"):
                base_url = base_url + ("/audio/transcriptions" if base_url.endswith("/v1") else "/v1/audio/transcriptions")
            client = AsyncOpenAI(api_key=api_key or "EMPTY", base_url=base_url.rsplit("/audio/transcriptions", 1)[0], timeout=timeout)
            # 简化：下载音频为字节流，调用 transcriptions.create
            async with aiohttp.ClientSession() as session:
                async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        return ""
                    audio_bytes = await resp.read()
            from io import BytesIO
            result = await client.audio.transcriptions.create(
                model=model,
                file=("audio.wav", BytesIO(audio_bytes)),
            )
            return str(getattr(result, "text", "") or "").strip()
        except ImportError:
            logger.warning("未安装 openai / aiohttp，无法调用独立 ASR API")
            return ""
        except Exception as exc:
            logger.warning(f"独立 ASR 调用失败: {exc}")
            return ""


__all__ = ["VoiceModeration"]
