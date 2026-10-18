"""L3b 群违规检测子域：文本检测（刷屏/涉政/骂人/广告/链接/群号推广）。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _check_spam / _check_political / _collect_profanity_keywords
    _check_profanity / _check_profanity_with_ai / _normalize_severity
    _check_ad / _normalize_host / _is_link_whitelisted / _check_link
    _check_group_promotion

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / get_group_setting
- RuntimeState（L2）：spam_records

延迟导入约束：`_is_link_whitelisted` 内 urlparse 保持函数内局部导入（与原实现一致）。

#272/#274 新增（非迁出）：
- _fetch_page_text / _extract_link_hosts / _extract_probe_urls
- _hosts_hit_domain_list / _check_link_safety
- _check_ad 扩展：链接目标页内容广告检测（ad_check_link_content）
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from astrbot.api import logger

from ..compat import aiohttp
from ..constants import _DEFAULT_PROFANITY_PROMPT

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..runtime import RuntimeState

# #272：内置不安全网站域名特征（保守清单，宁缺勿滥防误报）。
# 与归一化域名（去端口/去 www./小写）做子串匹配，命中即判违规，不访问目标页。
_UNSAFE_DOMAIN_PATTERNS = (
    "porn", "caoliu", "t66y", "aisex", "bocai", "yulecheng",
)

# 单条消息最多探测的目标页 URL 数（防单条刷屏消息触发大量抓取）
_MAX_LINK_PROBES = 3
# 抓取目标页返回的文本上限（title + 正文前若干字符）
_PAGE_TEXT_LIMIT = 1500
# 目标页响应体最多读取的字节数（防大文件拖爆内存）
_PAGE_MAX_BYTES = 65536
# 连续抓取失败熔断阈值：疑似服务器断网时，"拦截=不安全" 判定自动放行（#272 review 防误伤）
_LINK_FETCH_BREAKER = 5


class TextModeration:
    """文本违规检测领域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        runtime: "RuntimeState",
    ):
        self._store = config_store
        self.config = config_store.config
        self._runtime = runtime
        # #272：目标页抓取连续失败计数（熔断用，进程内状态不落盘）
        self._link_fetch_fail_streak = 0

    # ===================== 刷屏检测 =====================

    async def _check_spam(self, group_id: str, user_id: str,
                         has_text: bool = True, has_media: bool = False) -> bool:
        """刷屏检测。

        #260：新增 `spam_exclude_pure_media`（默认关）——开启后，纯图片/表情包
        （无可见文本的媒体消息）不计入刷屏窗口，避免「只发表情包被误判刷屏」。
        """
        try:
            threshold = int(self._store.get_group_setting(group_id, "spam_threshold", 5) or 5)
            window = int(self._store.get_group_setting(group_id, "spam_time_window", 10) or 10)
        except (TypeError, ValueError):
            return False
        if not self._store.get_group_setting(group_id, "spam_check_enabled", True):
            return False
        if threshold <= 0 or window <= 0:
            return False
        # #260：可选排除纯媒体消息（无文本且含图片/表情包）
        if not has_text and has_media \
                and self._store.get_group_setting(group_id, "spam_exclude_pure_media", False):
            return False
        now = time.time()
        key = f"{group_id}_{user_id}"
        records = self._runtime.spam_records[key]
        records[:] = [t for t in records if now - t < window]
        records.append(now)
        return len(records) >= threshold

    # ===================== 骂人检测 =====================

    def _check_political(self, msg_text: str) -> str:
        """#204：涉政关键词命中检测（全局配置 political_keywords），返回命中词或空串。

        涉政清单为硬清单：不受 AI 模式/管理员豁免影响前的文本检测顺序由 dispatch 保证。
        """
        if not msg_text:
            return ""
        text_lower = msg_text.lower()
        for kw in self.config.get("political_keywords", []) or []:
            k = str(kw).lower()
            if k and k in text_lower:
                return str(kw)
        return ""

    def _collect_profanity_keywords(self, group_id: str) -> list:
        """汇总骂人/违禁词关键词列表（review#208 config-coverage-regression）。

        来源清单（扩展时勿遗漏）：
        1. group_overrides[gid]["profanity_keywords"]（按群指令写入）
        2. top-level config["profanity_keywords"]（全局默认；无按群覆盖时由 get_group_setting 返回）
        3. 旧 config["violation_keywords"]（WebUI 历史入口，兼容保留）
        """
        kws = list(self._store.get_group_setting(group_id, "profanity_keywords", []) or [])
        for src in (
            self.config.get("profanity_keywords", []) or [],
            self.config.get("violation_keywords", []) or [],
        ):
            for k in src:
                if k not in kws:
                    kws.append(k)
        return kws

    async def _check_profanity(self, msg_text: str, event, group_id: str, user_id: str):
        """检测骂人：返回 (是否违规, 严重程度)。

        #243：严重程度（mild/medium/severe）仅在 AI 判定骂人时由模型返回；
        关键词硬清单命中不给分级（沿用 profanity_ban_duration），
        调用方无分级时按固定时长处理。
        """
        if not self._store.get_group_setting(group_id, "profanity_check_enabled", True):
            return False, ""
        if not msg_text:
            return False, ""
        # #207：违禁词为硬清单，优先匹配且不受 AI 模式影响；
        # 来源统一由 _collect_profanity_keywords 收敛（按群/全局/旧 violation_keywords）
        keywords = self._collect_profanity_keywords(group_id)
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                logger.warning(f"[群违规检测] 命中违禁词 用户 {user_id}: {kw}")
                return True, ""
        use_ai = bool(self._store.get_group_setting(group_id, "profanity_use_ai", True))
        if use_ai and aiohttp is not None:
            api_endpoint = self.config.get("api_endpoint", "")
            api_key = self.config.get("api_key", "")
            if api_endpoint:
                is_profanity, reason, severity = await self._check_profanity_with_ai(
                    api_endpoint, api_key, msg_text, group_id)
                if is_profanity:
                    logger.warning(f"[群违规检测] 骂人 用户 {user_id} {reason} 严重度={severity or '未给出'}")
                    return True, severity
        return False, ""

    async def _check_profanity_with_ai(self, api_endpoint: str, api_key: str, msg_text: str,
                                       group_id: str = ""):
        """AI 判骂人，返回 (是否骂人, 原因, 严重程度)。

        #243：提示词要求模型同时输出 severity（mild/medium/severe），解析做容错——
        字段缺失或非法时返回空串，由调用方回退到固定禁言时长（等价旧行为）。
        提示词取配置 `profanity_detection_prompt`（可按群覆盖），留空用内置默认；
        含 `{text}` 占位符时替换为待检测文本，否则把文本追加在末尾。
        """
        model_name = self.config.get("model_name", "gpt-4o")
        custom_prompt = str(
            self._store.get_group_setting(group_id, "profanity_detection_prompt", "") or ""
        ).strip()
        prompt = custom_prompt or _DEFAULT_PROFANITY_PROMPT
        if "{text}" in prompt:
            content_text = prompt.replace("{text}", msg_text)
        else:
            content_text = f"{prompt}\n\n待检测文本：{msg_text}"
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content_text}],
            "max_tokens": 200,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"[群违规检测] 骂人 AI 失败: {resp.status}")
                        return False, "", ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            text = match.group() if match else content
            obj = json.loads(text)
            return (bool(obj.get("is_profanity", False)), str(obj.get("reason", "")),
                    self._normalize_severity(obj.get("severity")))
        except Exception as e:
            logger.error(f"[群违规检测] 骂人 AI 解析失败: {e}")
            return False, "", ""

    @staticmethod
    def _normalize_severity(value) -> str:
        """#243：把模型返回的严重程度归一化；无法识别时返回空串（回退固定时长）。"""
        if value is None:
            return ""
        v = str(value).strip().lower()
        alias = {
            "mild": "mild", "low": "mild", "轻微": "mild", "轻度": "mild", "1": "mild",
            "medium": "medium", "moderate": "medium", "中等": "medium", "中度": "medium", "2": "medium",
            "severe": "severe", "high": "severe", "严重": "severe", "重度": "severe", "3": "severe",
        }
        return alias.get(v, "")

    # ===================== 广告 / 链接 / 群号推广 =====================

    async def _check_ad(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "ad_check_enabled", True):
            return False
        if not msg_text:
            return False
        keywords = self._store.get_group_setting(group_id, "ad_keywords", []) or []
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                return True
        # #274：文本未命中时可选检测链接目标页内容（默认关，按群覆盖）。
        # 目标页正文命中同一份 ad_keywords 即按广告违规处置；抓取失败/无文本静默放行。
        if not self._store.get_group_setting(group_id, "ad_check_link_content", False):
            return False
        return await self._ad_hit_in_link_pages(msg_text, group_id, user_id, keywords)

    async def _ad_hit_in_link_pages(self, msg_text: str, group_id: str, user_id: str,
                                    keywords: list) -> bool:
        """#274：抓取消息中链接的目标页正文，用 ad_keywords 匹配广告内容。"""
        if not keywords:
            return False
        # 链接白名单命中（全局+按群）不抓取，与链接检测口径一致
        if self._is_link_whitelisted(msg_text, group_id):
            return False
        urls = self._extract_probe_urls(msg_text)[:_MAX_LINK_PROBES]
        timeout = self._store.get_group_setting(group_id, "link_fetch_timeout", 5)
        for url in urls:
            page_text = await self._fetch_page_text(url, timeout)
            if not page_text:
                continue
            page_lower = page_text.lower()
            for kw in keywords:
                k = str(kw).lower()
                if k and k in page_lower:
                    logger.info(f"[群违规检测] 群 {group_id} 用户 {user_id} "
                                f"链接目标页命中广告关键词: {k}")
                    return True
        return False

    @staticmethod
    def _normalize_host(raw_host: str) -> str:
        """归一化域名：去端口、去 www. 前缀、小写。"""
        h = raw_host.split(":")[0].lower().strip(".")
        return h.removeprefix("www.")

    def _is_link_whitelisted(self, msg_text: str, group_id: str) -> bool:
        """#195：检查消息中的链接是否命中白名单（全局+按群）。命中则不触发链接检测。
        匹配规则：精确域名匹配（已做归一化去端口/去 www./小写）。
        白名单条目支持通配前缀 `*.example.com` 表示匹配该域及其所有子域。"""
        if not msg_text:
            return False
        # 提取消息中所有链接的 hostname（用 urlparse 兼容带端口/路径的 URL）
        from urllib.parse import urlparse
        hosts = set()
        for url_match in re.finditer(r"https?://[^\s]+", msg_text, re.IGNORECASE):
            url = url_match.group(0)
            parsed = urlparse(url)
            if parsed.hostname:
                hosts.add(self._normalize_host(parsed.hostname))
        # 也匹配裸 www.example.com（无协议前缀）
        for www_match in re.finditer(r"(?:^|\s)www\.[^\s]+", msg_text, re.IGNORECASE):
            url = www_match.group(0).strip()
            parsed = urlparse("http://" + url)
            if parsed.hostname:
                hosts.add(self._normalize_host(parsed.hostname))
        if not hosts:
            return False
        # 构建归一化白名单集合（精确条目 + 通配条目分开）
        raw_global = self.config.get("link_whitelist", []) or []
        raw_group = self._store.get_group_setting(group_id, "link_whitelist", []) or []
        exact_wl = set()
        wildcard_wl = set()  # 通配域名（去掉 *. 前缀）
        for entry in list(raw_global) + list(raw_group):
            e = self._normalize_host(str(entry))
            if e.startswith("*."):
                wildcard_wl.add(e[2:])  # "*.example.com" → "example.com"
            else:
                exact_wl.add(e)
        # 精确匹配
        if hosts & exact_wl:
            return True
        # 通配匹配：host 以 ".suffix" 结尾或等于 suffix
        for host in hosts:
            for domain in wildcard_wl:
                if host == domain or host.endswith("." + domain):
                    return True
        return False

    async def _check_link(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "link_check_enabled", False):
            return False
        if not msg_text:
            return False
        # #195：链接白名单命中则跳过
        if self._is_link_whitelisted(msg_text, group_id):
            return False
        pattern = r"(https?://[^\s]+|www\.[^\s]+\.[^\s]+|[^\s]+\.(com|cn|net|org|io|xyz|top|vip|cc|me|tv|edu|gov)[^\s]*)"
        return re.search(pattern, msg_text, re.IGNORECASE) is not None

    async def _check_group_promotion(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self._store.get_group_setting(group_id, "group_promotion_check_enabled", True):
            return False
        if not msg_text:
            return False
        promotion_keywords = ["进群", "加群", "群号", "入群", "拉群", "建群"]
        if not any(kw in msg_text for kw in promotion_keywords):
            return False
        group_pattern = r"[;；:,，\s]*(\d{5,12})"
        return bool(re.findall(group_pattern, msg_text))

    # ===================== 链接安全 / 目标页抓取（#272 #274） =====================

    @classmethod
    def _extract_probe_urls(cls, msg_text: str) -> list:
        """提取可探测的目标页 URL：完整 http(s) 链接 + 补协议的 www. 裸域。

        仅匹配 RFC3986 合法字符，天然规避中文标点/文字粘连
        （如「http://a.com/x，快来看」不会把标点并入 URL）。
        """
        url_chars = r"[A-Za-z0-9\-._~:/?#@!$&'()*+,;=%\[\]]"
        urls = [m.group(0) for m in
                re.finditer(r"https?://" + url_chars + "+", msg_text, re.IGNORECASE)]
        for m in re.finditer(r"(?:^|\s)(www\." + url_chars + "+)", msg_text, re.IGNORECASE):
            urls.append("http://" + m.group(1))
        return urls

    def _extract_link_hosts(self, msg_text: str) -> set:
        """提取消息中所有链接的归一化域名（口径同 _normalize_host）。"""
        hosts = set()
        for raw_url in self._extract_probe_urls(msg_text):
            parsed = urlparse(raw_url)
            if parsed.hostname:
                hosts.add(self._normalize_host(parsed.hostname))
        return hosts

    def _hosts_hit_domain_list(self, hosts: set, entries) -> bool:
        """hosts 是否命中域名列表（支持 `*.example.com` 通配子域，口径同 _is_link_whitelisted）。"""
        exact_wl = set()
        wildcard_wl = set()
        for entry in entries or []:
            e = self._normalize_host(str(entry))
            if e.startswith("*."):
                wildcard_wl.add(e[2:])
            else:
                exact_wl.add(e)
        for host in hosts:
            if host in exact_wl:
                return True
            for domain in wildcard_wl:
                if host == domain or host.endswith("." + domain):
                    return True
        return False

    async def _fetch_page_text(self, url: str, timeout) -> str:
        """抓取 URL 目标页正文文本（#272/#274 共用探测通道）。

        返回值三态：
        - None：抓取被拒/失败（403/451/超时/连接重置等网络异常）——可能被站点拦截
        - ""：可达但无可提取文本（非文本响应/其他状态码/页面无正文）
        - 非空串：title + 正文前 _PAGE_TEXT_LIMIT 字符（去脚本/样式/标签后）

        熔断：连续 _LINK_FETCH_BREAKER 次失败视为服务器断网，失败仍返回 None，
        由调用方结合熔断状态决定是否把「被拦截」当违规。
        """
        if aiohttp is None:
            return None
        try:
            t = max(1, min(15, int(timeout))) if timeout is not None else 5
        except (TypeError, ValueError):
            t = 5
        try:
            timeout_cfg = aiohttp.ClientTimeout(total=t)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=timeout_cfg,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AstrBotGM/1.0)"},
                ) as resp:
                    if resp.status == 403 or resp.status == 451:
                        self._link_fetch_fail_streak += 1
                        return None
                    # 站点有响应（含 404/5xx/非文本）：服务器可达，重置熔断计数
                    self._link_fetch_fail_streak = 0
                    if resp.status != 200:
                        return ""
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if ctype and not ctype.startswith("text/"):
                        return ""
                    data = await resp.content.read(_PAGE_MAX_BYTES)
        except Exception as e:
            self._link_fetch_fail_streak += 1
            logger.debug(f"[链接探测] 抓取失败（已连续 {self._link_fetch_fail_streak} 次）: "
                         f"{type(e).__name__}")
            return None
        try:
            encoding = resp.get_encoding()
        except Exception:
            encoding = "utf-8"
        raw_html = data.decode(encoding, errors="ignore")
        # 去脚本/样式，取 title + 正文
        raw_html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw_html,
                          flags=re.IGNORECASE | re.DOTALL)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html,
                            re.IGNORECASE | re.DOTALL)
        title = html.unescape(title_m.group(1)).strip() if title_m else ""
        # title 已单独提取，从原文移除避免正文里重复出现
        raw_html = re.sub(r"<title\b[^>]*>.*?</title>", " ", raw_html,
                          flags=re.IGNORECASE | re.DOTALL)
        body = re.sub(r"<[^>]+>", " ", raw_html)
        body = html.unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        return (f"{title} {body}").strip()[:_PAGE_TEXT_LIMIT]

    async def _check_link_safety(self, msg_text: str, group_id: str) -> tuple:
        """#272：不安全网站检测，返回 (是否违规, 原因)。

        判定顺序（前置免抓取，抓取兜底）：
        1. 消息无链接 → 放行
        2. 白名单（link_safety_whitelist + 复用链接白名单 link_whitelist）→ 放行
        3. 内置黄赌毒域名特征 _UNSAFE_DOMAIN_PATTERNS 命中 → 违规
        4. link_safety_keywords 与域名匹配 → 违规
        5. 抓取目标页：被拦截（403/451/超时/重置，返回 None）且
           link_safety_blocked_is_unsafe 开 → 违规（连续失败熔断时放行）
        6. 目标页正文命中 link_safety_keywords → 违规
        其余情况放行（抓取失败/无关键词命中不误伤）。
        """
        if not self._store.get_group_setting(group_id, "link_safety_check_enabled", False):
            return False, ""
        if not msg_text:
            return False, ""
        hosts = self._extract_link_hosts(msg_text)
        if not hosts:
            return False, ""
        # 2) 白名单：专属白名单 + 复用链接检测白名单（get_group_setting 已含全局回退）
        wl_entries = list(self._store.get_group_setting(group_id, "link_safety_whitelist", []) or []) \
            + list(self._store.get_group_setting(group_id, "link_whitelist", []) or [])
        if self._hosts_hit_domain_list(hosts, wl_entries):
            return False, ""
        # 3) 内置黄赌毒域名特征
        for host in hosts:
            if any(pat in host for pat in _UNSAFE_DOMAIN_PATTERNS):
                return True, f"域名特征疑似不安全网站（{host}）"
        # 4) 自定义关键词与域名匹配
        kws = [str(k).lower() for k in
               (self._store.get_group_setting(group_id, "link_safety_keywords", []) or []) if str(k)]
        for host in hosts:
            if any(k in host for k in kws):
                return True, "域名命中不安全网站关键词"
        # 5)/6) 抓取目标页兜底
        blocked_is_unsafe = self._store.get_group_setting(
            group_id, "link_safety_blocked_is_unsafe", True)
        breaker_open = self._link_fetch_fail_streak >= _LINK_FETCH_BREAKER
        if breaker_open:
            logger.warning(f"[链接安全] 连续 {self._link_fetch_fail_streak} 次抓取失败，"
                           f"疑似服务器断网，本轮「拦截=不安全」判定放行")
        urls = self._extract_probe_urls(msg_text)[:_MAX_LINK_PROBES]
        timeout = self._store.get_group_setting(group_id, "link_fetch_timeout", 5)
        for url in urls:
            page_text = await self._fetch_page_text(url, timeout)
            if page_text is None:
                if blocked_is_unsafe and not breaker_open:
                    return True, "站点拦截了服务器请求，视为不安全网站"
                continue
            if not page_text:
                continue
            page_lower = page_text.lower()
            for k in kws:
                if k and k in page_lower:
                    return True, "页面内容命中不安全网站关键词"
        return False, ""


__all__ = ["TextModeration"]
