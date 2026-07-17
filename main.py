import asyncio

from astrbot.api import star, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Reply
from typing import Optional

class GroupAdminPlugin(star.Star):
    """AstrBot 群管插件 - 提供完整的群组管理功能"""

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        # 读取配置文件
        self.show_recall_notice = config.get("show_recall_notice", True)
        self.reject_re_add = config.get("reject_re_add", False)
        # 读取插件管理员列表，过滤并转为字符串类型以防配置错误
        self.plugin_admins = [str(qq) for qq in config.get("plugin_admins", [])]

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为插件管理员"""
        return str(event.message_obj.sender.user_id) in self.plugin_admins

    @filter.command("设管")
    async def set_plugin_admin_command(self, event: AstrMessageEvent, args: list[str]):
        """添加插件管理员 (仅插件管理员或群主可用)"""
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return

        # 权限检查：必须是现有的插件管理员，或者是群主
        if not self._is_plugin_admin(event) and event.message_obj.sender.role != "owner":
            yield event.plain_result("你没有权限执行此操作，仅限插件管理员或群主使用。")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要设为插件管理员的人")
            return
            
        target_qq = str(at_segment.qq)
        
        if target_qq in self.plugin_admins:
            yield event.plain_result(f"QQ {target_qq} 已经是插件管理员了。")
            return
        
        self.plugin_admins.append(target_qq)
        # 同步回写到 AstrBot 配置中，使其持久化
        self.config["plugin_admins"] = self.plugin_admins
        self.config.save_config()
        yield event.plain_result(f"已将 @ qq={target_qq} 设为插件管理员。")

    @filter.command("取管")
    async def remove_plugin_admin_command(self, event: AstrMessageEvent, args: list[str]):
        """移除插件管理员 (仅插件管理员或群主可用)"""
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return

        if not self._is_plugin_admin(event) and event.message_obj.sender.role != "owner":
            yield event.plain_result("你没有权限执行此操作，仅限插件管理员或群主使用。")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要移除的插件管理员")
            return
            
        target_qq = str(at_segment.qq)
        
        if target_qq not in self.plugin_admins:
            yield event.plain_result(f"QQ {target_qq} 不是插件管理员。")
            return
        
        self.plugin_admins.remove(target_qq)
        # 同步回写到 AstrBot 配置中，使其持久化
        self.config["plugin_admins"] = self.plugin_admins
        self.config.save_config()
        yield event.plain_result(f"已移除 @ qq={target_qq} 的插件管理员身份。")

    @filter.command("禁言")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mute_command(self, event: AstrMessageEvent, args: list[str]):
        """禁言指定群成员"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要禁言的成员")
            return
            
        target_qq = at_segment.qq
        duration_minutes = 10  # 默认10分钟
        
        if args:
            try:
                duration_minutes = int(args[0])
            except ValueError:
                yield event.plain_result("时长格式错误: 请输入纯数字的分钟数\n例如: 1440 (代表1天)")
                return
            
        duration_seconds = duration_minutes * 60
            
        try:
            await self._mute_user(event.message_obj.group_id, target_qq, duration_seconds)
            yield event.plain_result(f"已禁言 @ qq={target_qq} {duration_minutes} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {str(e)}")
    
    @filter.command("解禁")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unmute_command(self, event: AstrMessageEvent, args: list[str]):
        """解除禁言"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要解禁的成员")
            return
            
        target_qq = at_segment.qq
        
        try:
            await self._unmute_user(event.message_obj.group_id, target_qq)
            yield event.plain_result(f"已解禁 @ qq={target_qq}")
        except Exception as e:
            yield event.plain_result(f"解禁失败: {str(e)}")
    
    @filter.command("踢")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def kick_command(self, event: AstrMessageEvent, args: list[str]):
        """踢出群成员"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要踢出的成员")
            return
            
        target_qq = at_segment.qq
        
        try:
            # 应用配置：是否拒绝再次加群
            await self._kick_user(event.message_obj.group_id, target_qq, self.reject_re_add)
            yield event.plain_result(f"已踢出 @ qq={target_qq}")
        except Exception as e:
            yield event.plain_result(f"踢出失败: {str(e)}")
    
    @filter.command("头衔")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_title_command(self, event: AstrMessageEvent, args: list[str]):
        """设置群成员专属头衔"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return

        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return

        # 从消息段中解析: 找 @ 段，收集其后的文本段作为头衔
        at_segment = None
        title_parts = []
        found_at = False

        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                found_at = True
                continue
            if found_at and hasattr(segment, 'text'):
                title_parts.append(str(segment.text))

        if not at_segment:
            yield event.plain_result("请使用 @ 提及要设置头衔的成员")
            return

        title = "".join(title_parts).strip()

        if not title:
            yield event.plain_result("请输入头衔名称。用法: /头衔 @某人 头衔名称")
            return

        target_qq = at_segment.qq

        try:
            await self._set_special_title(event.message_obj.group_id, target_qq, title)
            yield event.plain_result(f"已设置 @ qq={target_qq} 的头衔为: {title}")
        except Exception as e:
            yield event.plain_result(f"设置头衔失败: {str(e)}")

    @filter.command("取消头衔")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def remove_title_command(self, event: AstrMessageEvent, args: list[str]):
        """取消群成员专属头衔"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要取消头衔的成员")
            return
            
        target_qq = at_segment.qq
        
        try:
            # 传入空字符串即可清除头衔
            await self._set_special_title(event.message_obj.group_id, target_qq, "")
            yield event.plain_result(f"已取消 @ qq={target_qq} 的头衔")
        except Exception as e:
            yield event.plain_result(f"取消头衔失败: {str(e)}")

    @filter.command("设管理")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_admin_command(self, event: AstrMessageEvent, args: list[str]):
        """设置群管理员"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要设为管理的成员")
            return
            
        target_qq = at_segment.qq
        
        try:
            await self._set_group_admin(event.message_obj.group_id, target_qq, True)
            yield event.plain_result(f"已将 @ qq={target_qq} 设为管理员")
        except Exception as e:
            yield event.plain_result(f"设置管理员失败: {str(e)}")

    @filter.command("取消管理")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def remove_admin_command(self, event: AstrMessageEvent, args: list[str]):
        """取消群管理员"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        at_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                break
                
        if not at_segment:
            yield event.plain_result("请使用 @ 提及要取消管理的成员")
            return
            
        target_qq = at_segment.qq
        
        try:
            await self._set_group_admin(event.message_obj.group_id, target_qq, False)
            yield event.plain_result(f"已取消 @ qq={target_qq} 的管理员身份")
        except Exception as e:
            yield event.plain_result(f"取消管理员失败: {str(e)}")
    
    @filter.command("设精华")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_essence_command(self, event: AstrMessageEvent, args: list[str]):
        """将消息设为精华消息"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return
            
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        reply_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, Reply):
                reply_segment = segment
                break
                
        if not reply_segment:
            yield event.plain_result("请引用一条消息来设为精华")
            return
            
        message_id = reply_segment.id
        
        try:
            await self._set_essence_message(event.message_obj.group_id, message_id)
            yield event.plain_result(f"已将消息设为精华")
        except Exception as e:
            yield event.plain_result(f"设为精华失败: {str(e)}")
    
    @filter.command("设群昵称")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_card_command(self, event: AstrMessageEvent, args: list[str]):
        """设置群成员的群昵称"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return

        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return

        # 从消息段中解析: 找 @ 段，收集其后的文本段作为昵称
        at_segment = None
        card_parts = []
        found_at = False

        for segment in event.message_obj.message:
            if isinstance(segment, At):
                at_segment = segment
                found_at = True
                continue
            if found_at and hasattr(segment, 'text'):
                card_parts.append(str(segment.text))

        if not at_segment:
            yield event.plain_result("请使用 @ 提及要设置群昵称的成员")
            return

        new_card = "".join(card_parts).strip()

        if not new_card:
            yield event.plain_result("请输入群昵称。用法: /设群昵称 @某人 新昵称")
            return

        target_qq = at_segment.qq

        try:
            await self._set_group_card(event.message_obj.group_id, target_qq, new_card)
            yield event.plain_result(f"已设置 @ qq={target_qq} 的群昵称为: {new_card}")
        except Exception as e:
            yield event.plain_result(f"设置群昵称失败: {str(e)}")

    @filter.command("改昵称")
    async def set_my_group_card_command(self, event: AstrMessageEvent, args: list[str]):
        """设置自己的群昵称"""
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        if len(args) < 1:
            yield event.plain_result("用法: /改昵称 新昵称")
            return
            
        new_card = " ".join(args)
        sender_qq = event.message_obj.sender.user_id

        try:
            await self._set_group_card(event.message_obj.group_id, sender_qq, new_card)
            yield event.plain_result(f"已设置你的群昵称为: {new_card}")
        except Exception as e:
            yield event.plain_result(f"设置群昵称失败: {str(e)}")

    @filter.command("撤回")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def recall_command(self, event: AstrMessageEvent, args: list[str]):
        """撤回消息 - 支持引用撤回或按数量 /撤回 N"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("你没有权限使用此命令，需要先被设为插件管理员。使用 /设管 @某人 添加插件管理员。")
            return

        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return

        # 模式1: 按数量撤回 /撤回 N
        if args:
            try:
                count = int(args[0])
                if count <= 0:
                    yield event.plain_result("数量必须大于 0")
                    return
                if count > 999:
                    yield event.plain_result("单次最多撤回 999 条消息")
                    return

                current_msg_id = self._get_message_id(event)
                if current_msg_id is None:
                    yield event.plain_result("无法获取当前消息 ID，请改用引用撤回")
                    return

                recalled = 0
                failed = 0
                # 从当前消息开始往上撤回 count 条（包含命令消息本身）
                for i in range(count):
                    target_id = current_msg_id - i
                    try:
                        await self._recall_message(event.message_obj.group_id, target_id)
                        recalled += 1
                    except Exception:
                        failed += 1
                    # 小延迟避免触发频率限制
                    if i < count - 1:
                        await asyncio.sleep(0.3)

                if self.show_recall_notice:
                    yield event.plain_result(
                        f"撤回完成: 成功 {recalled} 条, 失败 {failed} 条"
                    )
                return
            except ValueError:
                pass  # 非纯数字，继续走引用撤回模式

        # 模式2: 引用撤回 /撤回 (回复某条消息)
        reply_segment = None
        for segment in event.message_obj.message:
            if isinstance(segment, Reply):
                reply_segment = segment
                break

        if not reply_segment:
            yield event.plain_result("请引用一条要撤回的消息，或发送 /撤回 数字 来撤回最近的消息")
            return

        message_id = reply_segment.id

        try:
            await self._recall_message(event.message_obj.group_id, message_id)
            if self.show_recall_notice:
                yield event.plain_result(f"已撤回该消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {str(e)}")
    
    @filter.command("禁我")
    async def mute_myself_command(self, event: AstrMessageEvent, args: list[str]):
        """禁言自己"""
        if not event.message_obj.group_id:
            yield event.plain_result("此命令仅在群聊中可用")
            return
            
        duration_minutes = 10  # 默认10分钟
        if args:
            try:
                duration_minutes = int(args[0])
            except ValueError:
                yield event.plain_result("时长格式错误: 请输入纯数字的分钟数\n例如: 1440 (代表1天)")
                return
            
        duration_seconds = duration_minutes * 60
        sender_qq = event.message_obj.sender.user_id
        
        try:
            await self._mute_user(event.message_obj.group_id, sender_qq, duration_seconds)
            yield event.plain_result(f"已禁言自己 {duration_minutes} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {str(e)}")
    
    def _get_message_id(self, event: AstrMessageEvent):
        """获取当前消息的 message_id"""
        msg_obj = event.message_obj
        for attr in ("message_id", "id", "msg_id"):
            val = getattr(msg_obj, attr, None)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return None

    # 以下是内部方法
    async def _get_platform(self):
        """获取 QQ 平台实例"""
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name == "aiocqhttp":
                return platform
        raise RuntimeError("未找到 QQ 平台实例，请检查 OneBot 连接状态")

    async def _call_qq_api(self, api: str, **params):
        """调用 QQ 平台 API"""
        platform = await self._get_platform()

        # 尝试多种方式调用 API，兼容不同版本的 AstrBot
        methods_to_try = [
            # AiocqhttpAdapter 可能直接有 call_action
            lambda: platform.call_action(api, **params),
            # 或者通过 client 属性
            lambda: platform.client.call_action(api, **params),
            # 或者通过 _client 属性
            lambda: platform._client.call_action(api, **params),
            # 或者通过 bot 属性
            lambda: platform.bot.call_action(api, **params),
            # 或者 adapter 直接支持 call_api
            lambda: platform.call_api(api, **params),
        ]

        last_error = None
        for method in methods_to_try:
            try:
                return await method()
            except (AttributeError, TypeError) as e:
                last_error = e
                continue

        raise RuntimeError(
            f"无法调用 QQ API '{api}'，已尝试 call_action/client.call_action 等方式均失败。"
            f"平台类型: {type(platform).__name__}, 最后错误: {last_error}"
        )

    async def _mute_user(self, group_id: str, user_id: str, duration: int):
        params = {"group_id": group_id, "user_id": user_id, "duration": duration}
        await self._call_qq_api("set_group_ban", **params)

    async def _unmute_user(self, group_id: str, user_id: str):
        await self._mute_user(group_id, user_id, 0)

    async def _kick_user(self, group_id: str, user_id: str, reject_add: bool):
        params = {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add}
        await self._call_qq_api("set_group_kick", **params)

    async def _set_special_title(self, group_id: str, user_id: str, title: str):
        params = {"group_id": int(group_id), "user_id": int(user_id), "special_title": title, "duration": -1}
        await self._call_qq_api("set_group_special_title", **params)

    async def _set_group_admin(self, group_id: str, user_id: str, enable: bool):
        params = {"group_id": group_id, "user_id": user_id, "enable": enable}
        await self._call_qq_api("set_group_admin", **params)

    async def _set_essence_message(self, group_id: str, message_id: str):
        params = {"group_id": group_id, "message_id": message_id}
        await self._call_qq_api("set_essence_message", **params)

    async def _set_group_card(self, group_id: str, user_id: str, card: str):
        params = {"group_id": group_id, "user_id": user_id, "card": card}
        await self._call_qq_api("set_group_card", **params)

    async def _recall_message(self, group_id: str, message_id: str):
        params = {"message_id": message_id}
        await self._call_qq_api("delete_msg", **params)
