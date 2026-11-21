"""L3b 群违规检测子域：图片检测（MD5 违禁图 + AI 鉴图）。

迁出自 main.py 的以下方法：
    _collect_image_urls / _check_banned_image / _banned_image_file_paths
    _get_banned_file_md5s / _compute_image_md5 / _check_image
    _check_with_openai_vision / _parse_openai_response / _check_with_moderation_api
    _download_image

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / get_group_setting / get_group_banned_image_files / data_dir
- OneBotApi（L2）：无直接调用（保留以对齐注入约定）
- RuntimeState（L2）：_banned_file_md5_cache

延迟导入约束：本模块使用 compat 提供的模块级 aiohttp（与原 main 模块级导入等价）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import TYPE_CHECKING, Iterator

from astrbot.api import logger

from ..compat import aiohttp

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..onebot_api import OneBotApi
    from ..runtime import RuntimeState


_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_HASH_CHUNK_BYTES = 128 * 1024
_MD5_CACHE_LIMIT = 512
# AstrBot 将模板字段键 banned_images_group_overrides.templates.group.files 转为目录；
# 目录由所有群共享，不能靠扫描目录判断归属，只使用当前群配置引用的文件。
_GROUP_UPLOAD_DIR = Path("files/banned_images_group_overrides/templates/group/files")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _file_signature(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


class ImageModeration:
    """图片违规检测领域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        onebot_api: "OneBotApi",
        runtime: "RuntimeState",
    ):
        self._store = config_store
        self.config = config_store.config
        self._api = onebot_api
        self._runtime = runtime
        self._banned_file_cache_lock = Lock()

    # ===================== 图片检测 =====================

    def _collect_image_urls(self, raw) -> list:
        urls = []
        if not isinstance(raw, dict):
            return urls
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "image":
                data = seg.get("data", {}) or {}
                u = data.get("url") or data.get("file") or ""
                if u and u not in urls:
                    urls.append(u)
        return urls

    async def _check_banned_image(self, image_url: str, group_id: str) -> bool:
        """#162：检查图片 MD5 是否在全局/本群违禁图列表中。

        仅快速预筛原图二次传播；截断或下载失败一律视为未命中（放行），
        不阻塞后续 AI 鉴图链路。
        """
        image_data, truncated = await self._download_image(image_url)
        if truncated or not image_data:
            return False
        md5 = hashlib.md5(image_data).hexdigest()
        # #265：schema 已移除 banned_images 填空式录入项，此处全局读取仅为
        # 兼容老用户 config.json 中的残留数据（仍生效、避免升级后行为漂移）；
        # 新配置统一通过 /添加违禁图片（按群覆盖）与 WebUI 上传维护。
        global_banned = set(self.config.get("banned_images", []) or [])
        group_banned = set(self._store.get_group_setting(group_id, "banned_images", []) or [])
        if md5 in global_banned or md5 in group_banned:
            return True
        # #184 / #296：上传文件独立于手动 MD5 配置，不受运行时名单覆盖影响。
        group_files = self._store.get_group_banned_image_files(group_id)
        global_files = self.config.get("banned_image_files", []) or []
        if not (global_files or group_files):
            return False
        return await asyncio.to_thread(self._matches_banned_upload, md5, global_files, group_files)

    def _matches_banned_upload(self, md5: str, global_files: list, group_files: list) -> bool:
        return md5 in self._iter_banned_md5s(self._resolve_banned_uploads(global_files)) or (
            md5 in self._iter_banned_md5s(self._resolve_banned_uploads(group_files, upload_dir=_GROUP_UPLOAD_DIR))
        )

    def _banned_image_file_paths(self, group_id: str | None = None) -> list[Path]:
        """返回指定群的上传文件路径；不传群号时保留原全局行为。"""
        if group_id is None:
            return self._resolve_banned_uploads(self.config.get("banned_image_files", []) or [])
        return self._resolve_banned_uploads(
            self._store.get_group_banned_image_files(group_id), upload_dir=_GROUP_UPLOAD_DIR
        )

    def _resolve_banned_uploads(
        self, rel_paths: list, *, upload_dir: Path = Path()
    ) -> list[Path]:
        if not isinstance(rel_paths, list) or not rel_paths:
            return []
        try:
            # data_dir = <plugin_data>/<plugin>/group_admin
            data_root = Path(self._store.data_dir).parent.resolve()
        except (OSError, ValueError, RuntimeError):
            return []
        allowed_root = data_root / upload_dir
        result = {}
        for rel in rel_paths:
            if not isinstance(rel, str):
                continue
            # 全局保留旧相对路径兼容；本群新字段只接收 AstrBot 上传格式。
            rel_str = rel.replace("\\", "/")
            if upload_dir == _GROUP_UPLOAD_DIR and rel_str.startswith("/"):
                continue
            rel_str = rel_str.lstrip("/")
            if not rel_str or ".." in rel_str.split("/") or ":" in rel_str:
                continue
            relative = Path(rel_str)
            if upload_dir == _GROUP_UPLOAD_DIR and relative.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            if not relative.is_relative_to(upload_dir) or PureWindowsPath(rel_str).is_reserved():
                continue
            try:
                p = (data_root / relative).resolve(strict=True)
                if p.is_relative_to(allowed_root) and p.is_file():
                    result[p] = None
            except (OSError, ValueError, RuntimeError):
                continue
        return list(result)

    def _get_banned_file_md5s(self, group_id: str | None = None) -> set[str]:
        """返回指定群上传文件的 MD5；不传群号时只返回全局上传文件的 MD5。"""
        return set(self._iter_banned_md5s(self._banned_image_file_paths(group_id)))

    def _iter_banned_md5s(self, paths: list[Path]) -> Iterator[str]:
        cache = self._runtime._banned_file_md5_cache
        for p in paths:
            key = p.as_posix()
            with self._banned_file_cache_lock:
                cached = cache.pop(key, None)
                try:
                    st = p.stat()
                    if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _MAX_IMAGE_BYTES:
                        continue
                    signature = _file_signature(st)
                    if cached is not None and cached[0] == signature:
                        digest = cached[1]
                    else:
                        digest = self._hash_banned_path(p, signature)
                        if digest is None:
                            continue
                    if len(cache) >= _MD5_CACHE_LIMIT:
                        cache.pop(next(iter(cache)))
                    cache[key] = (signature, digest)
                except (OSError, ValueError) as e:
                    logger.error(f"计算违禁图片文件 MD5 失败 {p}: {e}")
                    continue
            yield digest

    @staticmethod
    def _hash_banned_path(p: Path, signature: tuple[int, ...]) -> str | None:
        md5 = hashlib.md5()
        total = 0
        with p.open("rb") as file:
            if _file_signature(os.fstat(file.fileno())) != signature:
                return None
            while chunk := file.read(min(_HASH_CHUNK_BYTES, _MAX_IMAGE_BYTES - total + 1)):
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    return None
                md5.update(chunk)
            if _file_signature(os.fstat(file.fileno())) != signature:
                return None
        # 检测读取过程中发生的覆盖/替换，避免把旧内容记为当前路径的指纹。
        if total != signature[2] or _file_signature(p.stat()) != signature:
            return None
        return md5.hexdigest()

    async def _compute_image_md5(self, image_url: str):
        """下载图片并计算 MD5，供 /添加违禁图片 使用。

        返回 (md5: str|None, truncated: bool)。下载失败返回 (None, False)；
        图片超过 10MB 时返回 (None, True)，调用方应提示用户图片过大。
        """
        image_data, truncated = await self._download_image(image_url)
        if not image_data:
            return None, truncated
        return hashlib.md5(image_data).hexdigest(), truncated

    async def _check_image(self, image_url: str):
        """调用 AI API 审核图片。返回 (is_violation, reason)。"""
        if aiohttp is None:
            logger.warning("[群违规检测] aiohttp 未安装，跳过图片审核")
            return False, ""
        api_endpoint = self.config.get("api_endpoint", "")
        api_key = self.config.get("api_key", "")
        api_type = self.config.get("api_type", "openai_vision")
        if not api_endpoint:
            return False, ""
        try:
            image_data, _truncated = await self._download_image(image_url)
            if not image_data:
                return False, ""
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            if api_type == "moderation":
                return await self._check_with_moderation_api(api_endpoint, api_key, image_b64)
            return await self._check_with_openai_vision(api_endpoint, api_key, image_b64)
        except Exception as e:
            logger.error(f"[群违规检测] 图片审核失败: {e}")
            return False, ""

    async def _check_qr_code(self, image_url: str) -> tuple:
        """#237：识别图片中的二维码并对内容分类。

        返回 (category, reason) 二元组，category 取值：
        - "none"：不含二维码，或下载/识别失败（放行）
        - "plain"：普通二维码（收款码/好友码/正常活动码/小程序码等无危害内容）
        - "group_ad"：拉人入群的群二维码或带广告引流性质的推广码
        - "adult"：二维码内容涉及色情/赌博/毒品等黄赌毒内容

        仅 openai_vision 审核通路支持；moderation API 无法判定二维码，跳过并记
        debug 日志。失败一律返回 ("none", "")，不抛异常。
        """
        if aiohttp is None:
            return "none", ""
        api_endpoint = self.config.get("api_endpoint", "")
        api_key = self.config.get("api_key", "")
        if not api_endpoint:
            return "none", ""
        if self.config.get("api_type", "openai_vision") != "openai_vision":
            logger.debug("[二维码检测] 当前 api_type 非 openai_vision，无法识别二维码，跳过")
            return "none", ""
        try:
            image_data, _truncated = await self._download_image(image_url)
            if not image_data:
                return "none", ""
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            prompt = (
                "请判断这张图片中是否包含二维码（QR code，包括群二维码、好友二维码、"
                "小程序码、赞赏码/收款码等），并对二维码所指内容或图片整体宣传内容分类："
                "group_ad=拉人入群的群二维码/带广告引流推广性质的码；"
                "adult=涉及色情、赌博、毒品等黄赌毒内容的码或图片；"
                "plain=收款码/好友码/正常活动码等无危害普通二维码。请仅返回 JSON："
                "{\"has_qr_code\": true/false, \"category\": \"plain/group_ad/adult\", "
                "\"reason\": \"简短原因\"}"
            )
            payload = {
                "model": self.config.get("model_name", "gpt-4o"),
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ]}],
                "max_tokens": 200,
                "temperature": 0.1,
            }
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"[二维码检测] API 失败: {resp.status} {text[:200]}")
                        return "none", ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            if not content:
                return "none", ""
            # 解析容错：先尝试整体 JSON（模型规范输出），失败再正则提取首个
            # {...} 片段（模型可能附加说明文字）；均失败按未命中放行（fail-open，
            # 误放行优于误撤回，失败已记 error 日志）。
            try:
                result = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
                if not match:
                    return "none", ""
                try:
                    result = json.loads(match.group())
                except (json.JSONDecodeError, ValueError):
                    return "none", ""
            if not bool(result.get("has_qr_code", False)):
                return "none", ""
            category = str(result.get("category", "plain")).lower().strip()
            if category not in ("plain", "group_ad", "adult"):
                # 分类缺失或非法时兜底按普通二维码处理（保持旧行为：仅撤回）
                category = "plain"
            reason = str(result.get("reason", ""))[:100]
            return category, reason
        except Exception as e:
            logger.error(f"[二维码检测] 识别失败: {e}")
            return "none", ""

    async def _check_with_openai_vision(self, api_endpoint: str, api_key: str, image_b64: str):
        model_name = self.config.get("model_name", "gpt-4o")
        prompt = self.config.get("detection_prompt") or (
            "请分析这张图片，判断是否包含违规内容（色情/擦边等）。"
            "请仅返回 JSON：{\"is_violation\": true/false, \"type\": \"porn/sexy/normal\", "
            "\"confidence\": 0.0-1.0, \"reason\": \"简短原因\"}"
        )
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            "max_tokens": 500,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"[群违规检测] OpenAI Vision API 失败: {resp.status} {text[:200]}")
                        return False, ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            return self._parse_openai_response(content)
        except Exception as e:
            logger.error(f"[群违规检测] OpenAI Vision 调用失败: {e}")
            return False, ""

    def _parse_openai_response(self, content: str):
        if not content:
            return False, ""
        try:
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            text = match.group() if match else content
            data = json.loads(text)
            is_violation = bool(data.get("is_violation", False))
            v_type = str(data.get("type", "normal")).lower()
            try:
                confidence = float(data.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(data.get("reason", ""))
            threshold = float(self.config.get("threshold", 0.7) or 0.7)
            check_porn = bool(self.config.get("check_porn", True))
            check_sexy = bool(self.config.get("check_sexy", True))
            if is_violation and confidence >= threshold:
                if v_type == "porn" and check_porn:
                    return True, f"检测到色情内容 (置信度: {confidence:.0%}) - {reason}"
                if v_type == "sexy" and check_sexy:
                    return True, f"检测到擦边内容 (置信度: {confidence:.0%}) - {reason}"
            return False, ""
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[群违规检测] 解析 OpenAI 响应失败: {e}")
            return False, ""

    async def _check_with_moderation_api(self, api_endpoint: str, api_key: str, image_b64: str):
        payload = {"input": image_b64}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"[群违规检测] Moderation API 失败: {resp.status}")
                        return False, ""
                    data = await resp.json()
            results = data.get("results") or []
            if not results:
                return False, ""
            categories = results[0].get("categories", {}) or {}
            scores = results[0].get("category_scores", {}) or {}
            if categories.get("sexual"):
                return True, f"检测到性内容 (置信度: {scores.get('sexual', 0):.0%})"
            return False, ""
        except Exception as e:
            logger.error(f"[群违规检测] Moderation API 调用失败: {e}")
            return False, ""

    async def _download_image(self, url: str, max_size: int = _MAX_IMAGE_BYTES):
        """下载图片。#162 增强：限制最大 10MB，避免慢速/大文件阻塞检测。

        返回 (data, truncated) 元组：truncated=True 表示数据被截断到 max_size+1，
        调用方（如 /添加违禁图片）应拒绝基于截断数据计算指纹。
        """
        if aiohttp is None:
            return None, False
        try:
            if url.startswith("http://") or url.startswith("https://"):
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            return None, False
                        # Content-Type 必须为图片
                        ct = resp.headers.get("Content-Type", "")
                        if ct and not ct.startswith("image/"):
                            return None, False
                        data = await resp.content.read(max_size + 1)
                        return data, len(data) > max_size
            elif url.startswith("base64://"):
                # base64 内嵌数据本身即图片内容，无需 Content-Type 校验
                data = base64.b64decode(url[9:])
                truncated = len(data) > max_size
                return (data[:max_size + 1] if truncated else data), truncated
            elif url.startswith("file://"):
                with open(url[7:], "rb") as f:
                    data = f.read(max_size + 1)
                    return data, len(data) > max_size
            elif url and not url.startswith("http"):
                # 某些实现把图片作为本地路径返回
                try:
                    with open(url, "rb") as f:
                        data = f.read(max_size + 1)
                        return data, len(data) > max_size
                except OSError:
                    return None, False
        except Exception as e:
            logger.error(f"[群违规检测] 下载图片失败: {e}")
        return None, False


__all__ = ["ImageModeration"]
