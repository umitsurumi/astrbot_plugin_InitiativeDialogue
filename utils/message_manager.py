import asyncio
import json
import random
import re

from astrbot.api import logger  # 使用官方 Logger 打印日志
from astrbot.api.all import MessageChain
from astrbot.api.message_components import Plain

# 引入必要的组件用于手动存档历史记录
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)


class MessageManager:
    """消息管理器，负责生成和发送各类消息"""

    def __init__(self, parent):
        self.parent = parent
        self.context = parent.context

    def _split_text(self, text: str) -> list[str]:
        """
        [新增] 文本分段逻辑
        根据标点符号拆分文本，避免一大坨文字直接糊用户脸上
        """
        # 如果文本很短（比如少于10个字），就不分段了，直接发
        if len(text) < 10:
            return [text]

        # 正则解释：非贪婪匹配直到遇到 句号/问号/感叹号/波浪号/省略号/换行符，或者直接匹配到字符串结尾
        regex_pattern = r".*?[。？！~…\n]+|.+$"
        try:
            segments = re.findall(
                regex_pattern,
                text,
                re.DOTALL | re.MULTILINE,
            )
            # 过滤掉空字符串和纯空白字符
            return [seg.strip() for seg in segments if seg.strip()]
        except Exception:
            # 如果正则炸了，保底返回原文本
            return [text]

    async def _simulate_typing_delay(self, text_segment: str):
        """
        [新增] 模拟打字延迟
        根据字数计算等待时间，让连续消息更自然
        """
        # 基础延迟 1秒 + 每个字 0.1秒
        delay = 1.0 + len(text_segment) * 0.1
        # 限制最大延迟 5秒，防止用户等太久
        delay = min(delay, 5.0)
        # 稍微加一点点随机波动
        delay += random.uniform(0, 0.5)

        await asyncio.sleep(delay)

    async def generate_and_send_message(
        self,
        user_id: str,
        conversation_id: str,
        unified_msg_origin: str,
        prompts: list[str],
        message_type: str = "一般",
        time_period: str | None = None,
        extra_context: str | None = None,
    ):
        """生成并发送消息"""
        try:
            # 1. 获取对话上下文
            conversation = await self.context.conversation_manager.get_conversation(
                unified_msg_origin, conversation_id
            )

            if not conversation:
                logger.error(
                    f"[主动对话] 无法获取用户 {user_id} 的对话，会话ID: {conversation_id} 可能不存在"
                )
                return False

            # 2. 准备 System Prompt (人设)
            system_prompt = ""
            try:
                # 优先获取会话绑定人设
                if conversation and conversation.persona_id:
                    persona = await self.context.persona_manager.get_persona(
                        conversation.persona_id
                    )
                    if persona:
                        system_prompt = persona.system_prompt

                # 回退到全局默认
                if not system_prompt:
                    if hasattr(self.context.persona_manager, "get_default_persona_v3"):
                        default_persona = (
                            await self.context.persona_manager.get_default_persona_v3(
                                umo=unified_msg_origin
                            )
                        )
                        if default_persona:
                            system_prompt = default_persona.get("prompt", "")

            except Exception as e:
                logger.warning(f"[主动对话] 获取人设失败: {e}，使用默认设置。")

            if not system_prompt:
                system_prompt = "你是一个智能AI助手，请根据上下文自然地回复用户。"

            # 3. 准备历史记录
            history_list = []
            if conversation and conversation.history:
                try:
                    if isinstance(conversation.history, str):
                        history_list = await asyncio.to_thread(
                            json.loads, conversation.history
                        )
                    else:
                        history_list = conversation.history
                except Exception as e:
                    logger.warning(f"[主动对话] 解析历史记录失败: {e}")

            # 4. 构建提示词
            prompt = random.choice(prompts)

            # 节日/时间段/日程逻辑
            festival_detector = getattr(self.parent, "festival_detector", None)
            festival_name = (
                festival_detector.get_festival_name() if festival_detector else None
            )
            festival_prompts = (
                festival_detector.get_festival_prompts() if festival_detector else None
            )

            if festival_prompts and message_type not in [
                "主动消息",
                "早安",
                "晚安",
                "日程安排",
            ]:
                prompt = random.choice(festival_prompts)
                logger.info(f"[主动对话] 今天是{festival_name}，使用节日提示词")

            # 日程注入
            ai_schedule = None
            if (
                hasattr(self.parent, "ai_schedule")
                and time_period
                and message_type != "日程安排"
            ):
                ai_schedule = self.parent.ai_schedule.get_schedule_by_time_period(
                    time_period
                )

            if ai_schedule:
                schedule_text = (
                    f"根据你今天的日程安排，{time_period}你计划{ai_schedule}。"
                )
                extra_context = f"{schedule_text} {extra_context or ''}"

            # 组合最终提示词
            # 注意：这里的 prompt 是给 LLM 看的指令，不是发给用户的
            context_requirement = "请确保回复贴合当前的对话上下文情景。"
            base_prompt = f"[系统指令: {prompt}]"

            if festival_name:
                final_prompt = f"{base_prompt}，今天是{festival_name}。{extra_context or ''} {context_requirement}"
            elif time_period:
                final_prompt = f"{base_prompt}，现在是{time_period}。{extra_context or ''} {context_requirement}"
            else:
                final_prompt = (
                    f"{base_prompt}。{extra_context or ''} {context_requirement}"
                )

            logger.info(f"[主动对话] 正在为 {user_id} 生成 [{message_type}] 消息...")

            # 5. 调用 LLM
            provider_id = await self.context.get_current_chat_provider_id(
                unified_msg_origin
            )

            llm_response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=final_prompt,
                system_prompt=system_prompt,
                contexts=history_list,
                conversation=conversation,
            )

            # 6. 处理回复（分段发送 & 统一存档）
            if llm_response and llm_response.completion_text:
                full_response_text = llm_response.completion_text.strip()

                # --- 分段发送逻辑 ---
                segments = self._split_text(full_response_text)
                logger.info(f"[主动对话] 将发送 {len(segments)} 条分段消息。")

                for i, seg in enumerate(segments):
                    # 发送当前片段
                    await self.context.send_message(
                        unified_msg_origin, MessageChain([Plain(seg)])
                    )

                    # 如果不是最后一段，则等待一下，模拟打字
                    if i < len(segments) - 1:
                        await self._simulate_typing_delay(seg)

                logger.info("[主动对话] 所有消息发送完毕。")

                # --- 存档逻辑 (存完整的一条) ---
                try:
                    user_msg_obj = UserMessageSegment(
                        content=[TextPart(text=final_prompt)]
                    )
                    # 注意：这里存的是 full_response_text，保证历史记录的完整性
                    assistant_msg_obj = AssistantMessageSegment(
                        content=[TextPart(text=full_response_text)]
                    )

                    await self.context.conversation_manager.add_message_pair(
                        cid=conversation_id,
                        user_message=user_msg_obj,
                        assistant_message=assistant_msg_obj,
                    )
                    logger.debug("[主动对话] 完整对话历史已存档。")
                except Exception as e:
                    logger.error(f"[主动对话] 存档历史记录失败: {e}")

                # 标记状态
                if message_type == "主动消息":
                    self.parent.dialogue_core.users_received_initiative.add(user_id)

                return True
            else:
                logger.warning("[主动对话] LLM 生成内容为空。")
                return False

        except Exception as e:
            import traceback

            logger.error(f"[主动对话] 执行异常: {str(e)}\n{traceback.format_exc()}")
            return False

    def parse_unified_msg_origin(self, unified_msg_origin: str):
        try:
            parts = unified_msg_origin.split(":")
            if len(parts) >= 3:
                return parts[0], parts[1], ":".join(parts[2:])
            return None, None, None
        except Exception:
            return None, None, None
