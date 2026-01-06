# 修复版 v3 (最终版): astrbot_plugin_initiativedialogue/utils/message_manager.py
import asyncio
import json
import random

from astrbot.api import logger  # <--- 关键修改：使用官方 Logger 确保能看到日志
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
            # 1. 获取对话上下文对象
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

            # 3. 准备历史记录 (Contexts)
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

            # 4. 构建提示词 (Prompt)
            prompt = random.choice(prompts)

            # 节日与时间段逻辑
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
            base_prompt = f"[系统指令: {prompt}]"  # 标记为系统指令

            if festival_name:
                final_prompt = f"{base_prompt}，今天是{festival_name}。{extra_context or ''} {context_requirement}"
            elif time_period:
                final_prompt = f"{base_prompt}，现在是{time_period}。{extra_context or ''} {context_requirement}"
            else:
                final_prompt = (
                    f"{base_prompt}。{extra_context or ''} {context_requirement}"
                )

            logger.info(
                f"[主动对话] 正在为 {user_id} 生成 [{message_type}] 消息 (历史记录: {len(history_list)}条)..."
            )

            # 5. 调用 LLM 生成
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

            # 6. 发送与存档
            if llm_response and llm_response.completion_text:
                response_text = llm_response.completion_text

                # --- 发送消息 ---
                await self.context.send_message(
                    unified_msg_origin, MessageChain([Plain(response_text)])
                )
                logger.info(f"[主动对话] 消息已发送: {response_text[:20]}...")

                # --- 核心修复：手动存入历史记录 ---
                try:
                    # 构造要存入数据库的消息对象
                    # 我们把 Prompt 存为 User 消息，把回复存为 Assistant 消息
                    # 这样下次 LLM 生成时就能看到这个上下文了
                    user_msg_obj = UserMessageSegment(
                        content=[TextPart(text=final_prompt)]
                    )
                    assistant_msg_obj = AssistantMessageSegment(
                        content=[TextPart(text=response_text)]
                    )

                    await self.context.conversation_manager.add_message_pair(
                        cid=conversation_id,
                        user_message=user_msg_obj,
                        assistant_message=assistant_msg_obj,
                    )
                    logger.debug("[主动对话] 历史记录已存档。")
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
