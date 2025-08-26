# astrbot_network_error_plugin.py

from astrbot import on_message, send_message, recall_message
from astrbot.exceptions import NetworkError
import asyncio
import logging
import math
from typing import Callable, Awaitable, Dict, Any, Optional

# 配置日志
# 建议在实际部署时，根据环境配置日志级别和输出方式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 插件配置的默认值
DEFAULT_CONFIG = {
    'retry_attempts': 3,
    'retry_delay_base': 1000,  # 毫秒，指数退避的基数
    'error_message': '抱歉，发生网络错误，消息已撤回。',
    'log_sensitive_info': False, # 是否记录可能敏感的信息，如消息ID
    'recall_retry_attempts': 2, # 撤回消息的重试次数
    'recall_retry_delay_base': 500 # 撤回消息的指数退避基数（毫秒）
}

# 存储每个channel_id最后发送的错误消息ID，以便在出错时撤回
# 使用 asyncio.Lock 保护共享状态，解决并发风险
last_sent_message_ids: Dict[str, str] = {} # 明确指定value为str类型
_message_ids_lock = asyncio.Lock()

def is_network_error(error: Exception) -> bool:
    """
    判断是否为网络错误的辅助函数。
    根据错误类型或错误消息内容进行判断。
    """
    if isinstance(error, NetworkError):
        return True
    
    error_message = str(error).lower()
    if 'network' in error_message or 'timeout' in error_message or \
       'connection reset' in error_message or 'connection refused' in error_message or \
       'host unreachable' in error_message or 'name or service not known' in error_message:
        return True
    
    # 针对 fetch 相关的网络错误通常是 TypeError
    if isinstance(error, TypeError) and ('fetch' in error_message or 'network request' in error_message):
        return True
    
    return False

def validate_config(config: Dict[str, Any]) -> None:
    """对插件配置进行类型和值校验。"""
    if not isinstance(config.get('retry_attempts'), int) or config['retry_attempts'] < 0:
        raise ValueError("retry_attempts 必须是非负整数。")
    if not isinstance(config.get('retry_delay_base'), (int, float)) or config['retry_delay_base'] <= 0:
        raise ValueError("retry_delay_base 必须是正数（毫秒）。")
    if not isinstance(config.get('error_message'), str):
        raise ValueError("error_message 必须是字符串。")
    if not isinstance(config.get('log_sensitive_info'), bool):
        raise ValueError("log_sensitive_info 必须是布尔值。")
    if not isinstance(config.get('recall_retry_attempts'), int) or config['recall_retry_attempts'] < 0:
        raise ValueError("recall_retry_attempts 必须是非负整数。")
    if not isinstance(config.get('recall_retry_delay_base'), (int, float)) or config['recall_retry_delay_base'] <= 0:
        raise ValueError("recall_retry_delay_base 必须是正数（毫秒）。")

async def _recall_and_clear_message_id(channel_id: str, message_id: str, log_sensitive_info: bool, 
                                        recall_retry_attempts: int, recall_retry_delay_base_ms: int):
    """
    尝试撤回消息并从存储中清除ID，支持重试。
    """
    message_id_str = str(message_id)
    async with _message_ids_lock:
        if last_sent_message_ids.get(channel_id) == message_id_str:
            for attempts in range(recall_retry_attempts + 1): # +1 是为了包含第一次尝试
                try:
                    await recall_message(channel_id, message_id_str)
                    if log_sensitive_info:
                        logger.debug(f"频道 {channel_id} 错误消息 {message_id_str} 撤回成功。")
                    else:
                        logger.info(f"频道 {channel_id} 错误消息撤回成功。")
                    last_sent_message_ids.pop(channel_id, None)
                    return # 撤回成功，退出
                except Exception as recall_e:
                    if attempts < recall_retry_attempts:
                        delay = recall_retry_delay_base_ms * (2 ** attempts) / 1000 # 指数退避，转换为秒
                        logger.warning(f"在频道 {channel_id} 撤回消息 {message_id_str} 失败: {recall_e}。尝试重试 {attempts + 1}/{recall_retry_attempts}，延迟 {delay:.2f} 秒...")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"在频道 {channel_id} 撤回消息 {message_id_str} 失败，所有重试均已用尽: {recall_e}")
            
        else:
            if log_sensitive_info:
                logger.debug(f"频道 {channel_id} 的消息 {message_id_str} 已被其他操作更新或清除，无需撤回。")
            else:
                logger.info(f"频道 {channel_id} 的消息已更新或清除，无需撤回。")


def create_network_error_handler(
    next_action: Callable[[Any], Awaitable[Optional[str]]], # next_action 应该返回消息ID或None
    config: Optional[Dict[str, Any]] = None
) -> Callable[[Any], Awaitable[None]]:
    """
    创建一个astrbot消息处理器，用于处理网络错误并实现消息撤回和指数退避重试。
    
    Args:
        next_action: 一个异步函数，代表插件实际要执行的操作。
                     它接受 astrbot 的 event 对象作为参数，并应返回发送的消息ID（如果适用）。
        config: 可选的配置字典，用于覆盖默认设置。
    
    Returns:
        一个异步消息处理器函数，可以直接用 @on_message 装饰。
    """
    current_config = DEFAULT_CONFIG.copy()
    if config:
        current_config.update(config)
    
    validate_config(current_config) # 校验配置

    retry_attempts = current_config['retry_attempts']
    retry_delay_base_ms = current_config['retry_delay_base']
    error_message_text = current_config['error_message']
    log_sensitive_info = current_config['log_sensitive_info']
    recall_retry_attempts = current_config['recall_retry_attempts']
    recall_retry_delay_base_ms = current_config['recall_retry_delay_base']

    @on_message
    async def network_error_handler(event):
        channel_id = event.channel_id
        
        # 获取当前频道最后发送的消息ID，用于可能的撤回
        async with _message_ids_lock:
            last_message_id_in_channel = last_sent_message_ids.get(channel_id)
        
        try:
            # 尝试执行受保护的操作
            message_id_raw = await next_action(event)
            if message_id_raw is not None:
                message_id_str = str(message_id_raw) # 转换为字符串再存储
                async with _message_ids_lock:
                    last_sent_message_ids[channel_id] = message_id_str
            
            if log_sensitive_info:
                logger.debug(f"频道 {channel_id} 操作成功，消息ID: {message_id_raw}")
            else:
                logger.info(f"频道 {channel_id} 操作成功。")

        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.warning(f"在频道 {channel_id} 捕获到中断或取消信号，正在退出。")
            raise # 重新抛出，允许上层处理中断

        except Exception as initial_error:
            if is_network_error(initial_error):
                logger.warning(f"在频道 {channel_id} 捕获到网络错误: {initial_error}")
                
                for attempts in range(retry_attempts):
                    delay = retry_delay_base_ms * (2 ** attempts) / 1000 # 指数退避，转换为秒
                    logger.info(f"在频道 {channel_id} 尝试重试 {attempts + 1}/{retry_attempts}，延迟 {delay:.2f} 秒...")
                    await asyncio.sleep(delay)
                    
                    try:
                        message_id_raw = await next_action(event)
                        if message_id_raw is not None:
                            message_id_str = str(message_id_raw) # 转换为字符串再存储
                            async with _message_ids_lock:
                                last_sent_message_ids[channel_id] = message_id_str
                        if log_sensitive_info:
                            logger.debug(f"在频道 {channel_id} 重试成功，消息ID: {message_id_raw}。")
                        else:
                            logger.info(f"在频道 {channel_id} 重试成功。")
                        return # 重试成功，退出
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        logger.warning(f"在频道 {channel_id} 重试期间捕获到中断或取消信号，正在退出。")
                        raise
                    except Exception as retry_error:
                        if is_network_error(retry_error):
                            logger.warning(f"在频道 {channel_id} 重试 {attempts + 1} 失败: {retry_error}")
                        else:
                            logger.error(f"在频道 {channel_id} 重试时捕获到非网络错误: {retry_error}")
                            raise retry_error # 非网络错误，直接抛出
                
                # 如果所有重试都失败
                logger.error(f"在频道 {channel_id} 所有重试均失败。")
                if last_message_id_in_channel:
                    await _recall_and_clear_message_id(channel_id, last_message_id_in_channel, 
                                                        log_sensitive_info, recall_retry_attempts, recall_retry_delay_base_ms)
                
                # 发送新的错误提示
                try:
                    error_response = await send_message(channel_id, error_message_text)
                    if error_response and hasattr(error_response, 'message_id'):
                        error_message_id_str = str(error_response.message_id) # 转换为字符串再存储
                        async with _message_ids_lock:
                            last_sent_message_ids[channel_id] = error_message_id_str
                        if log_sensitive_info:
                            logger.debug(f"在频道 {channel_id} 新的错误消息已发送，ID: {last_sent_message_ids[channel_id]}")
                        else:
                            logger.info(f"在频道 {channel_id} 新的错误消息已发送。")
                    else:
                        logger.warning(f"在频道 {channel_id} 发送错误消息但未能获取到message_id。")
                except Exception as send_error_e:
                    logger.error(f"在频道 {channel_id} 发送新的错误提示消息也失败了: {send_error_e}")
            else:
                # 如果不是网络错误，继续抛出
                logger.error(f"在频道 {channel_id} 捕获到非网络错误: {initial_error}")
                raise initial_error
        except Exception as e:
            logger.critical(f"在频道 {channel_id} 插件发生未知错误: {e}")
            # 确保所有未捕获的异常都被记录
    
    return network_error_handler

# --- 插件使用示例和测试钩子 ---

# 这是一个示例的 next_action，你可以替换成你的实际业务逻辑
async def my_actual_send_message_action(event) -> Optional[str]:
    """
    模拟一个实际的消息发送操作，可能发生网络错误。
    在实际使用中，这里会是你的业务逻辑，例如调用外部API，然后发送结果消息。
    """
    # 模拟一个可能失败的网络操作
    # 为了演示，我们直接尝试发送一条消息
    logger.info(f"[{event.channel_id}] 正在执行 my_actual_send_message_action...")
    
    # --- 测试钩子：在这里手动抛出错误来测试插件 ---
    if hasattr(event, 'test_fail_count'):
        event.test_fail_count -= 1
        if event.test_fail_count >= 0:
            logger.warning(f"[{event.channel_id}] 模拟网络错误！剩余失败次数: {event.test_fail_count}")
            raise NetworkError(f"模拟网络连接中断进行测试 (剩余 {event.test_fail_count} 次)")
    # --- 测试钩子结束 ---

    response = await send_message(event.channel_id, "操作进行中...")
    if response and hasattr(response, 'message_id'):
        return str(response.message_id) # 确保返回的是字符串
    return None

# 如何在你的 astrbot 主程序中使用这个插件：
# 1. 将此文件保存为 astrbot_network_error_plugin.py
# 2. 在你的 astrbot 主程序中导入 create_network_error_handler 函数
# 3. 定义你的实际消息处理逻辑（例如 my_actual_send_message_action 或其他异步函数）
# 4. 使用 create_network_error_handler 创建一个处理器，并用 @on_message 装饰它。

# 示例：在你的主程序中
# from astrbot_network_error_plugin import create_network_error_handler, my_actual_send_message_action

# # 定义插件配置 (可选)
# custom_plugin_config = {
#     'retry_attempts': 5,
#     'retry_delay_base': 500, # 500ms
#     'error_message': '服务暂时不可用，请稍后再试。',
#     'log_sensitive_info': True # 在调试时可以开启
# }

# # 创建并注册插件处理器
# # 注意：@on_message 装饰器通常直接应用于函数，
# # 如果 astrbot 框架允许动态注册，你可以直接调用 create_network_error_handler
# # 如果 astrbot 框架要求 @on_message 装饰一个顶层函数，
# # 你可能需要将 create_network_error_handler 的结果赋值给一个全局变量，
# # 然后再用 @on_message 装饰一个调用该全局变量的函数。
# # 或者，直接将 create_network_error_handler 的结果作为 @on_message 的参数（如果支持）。

# # 假设 astrbot 允许直接装饰工厂函数返回的处理器
# # @on_message
# # async def my_bot_entry_point(event):
# #     await create_network_error_handler(my_actual_send_message_action, custom_plugin_config)(event)

# # 更常见的做法是，将 next_action 包装在插件内部，或者直接在 @on_message 函数中调用
# # 为了简化，我们直接将 my_actual_send_message_action 作为 next_action 传入
# # 并将返回的处理器直接注册。
# # 如果 astrbot 不支持这种直接注册，你可能需要手动调用。

# # 推荐的使用方式 (如果 astrbot 允许 @on_message 装饰一个变量)
# # network_error_protected_handler = create_network_error_handler(my_actual_send_message_action, custom_plugin_config)
# # @on_message
# # async def main_message_handler(event):
# #     await network_error_protected_handler(event)

# # 或者，如果你想在 @on_message 内部动态创建和调用
# @on_message
# async def main_message_handler(event):
#     # 每次消息来时都创建一个新的处理器实例，确保配置是最新的
#     handler = create_network_error_handler(my_actual_send_message_action, custom_plugin_config)
#     await handler(event)

# # --------------------------------------------------------------------
# # 测试说明：
# # 1. 在你的 astrbot 主程序中，按照上述示例导入并注册插件。
# # 2. 在 `my_actual_send_message_action` 函数中，取消注释 `--- 测试钩子 ---` 部分的代码。
# # 3. 模拟 `event` 对象，例如：
# #    class MockEvent:
# #        def __init__(self, channel_id, test_fail_count=0):
# #            self.channel_id = channel_id
# #            self.test_fail_count = test_fail_count
# #    
# #    async def test_plugin():
# #        # 模拟一个事件，让 next_action 失败 3 次
# #        event = MockEvent("test_channel_1", test_fail_count=3)
# #        await main_message_handler(event)
# #        # 观察日志输出，看是否进行了重试和撤回
# #    
# #    if __name__ == "__main__":
# #        asyncio.run(test_plugin())
# # --------------------------------------------------------------------