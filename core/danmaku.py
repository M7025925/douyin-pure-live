"""抖音弹幕 WebSocket 连接与 protobuf 解析模块。

对应 Tauri 版 src/utils/RustSocket.ts + src/App.vue 的 creatSokcet/onMessage/handleMessage：
连接 wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/，
PushFrame -> gzip 解压 -> Response -> needAck 回 ack -> 按 method 分发消息。
"""
import asyncio
import gzip
from typing import Awaitable, Callable, Optional

import websockets

from core import dy_pb2

WS_URL_TMPL = (
    "wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/"
    "?room_id={room_id}&compress=gzip&version_code=180800"
    "&webcast_sdk_version=1.0.14-beta.0&live_id=1&did_rule=3"
    "&user_unique_id={unique_id}&identity=audience&signature={sign}"
    "&aid=6383&device_platform=web&browser_language=zh-CN"
    "&browser_platform=Win32&browser_name=Mozilla"
    "&browser_version=5.0+%28Windows+NT+10.0%3B+Win64%3B+x64%29"
    "+AppleWebKit%2F537.36+%28KHTML%2C+like+Gecko%29+Chrome%2F126.0.0.0"
    "+Safari%2F537.36+Edg%2F126.0.0.0"
)

# 与 App.vue 中 ConnectionConfig.headers 一致（UA 需与 URL browser_version 及签名环境一致）
WS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

MessageType = str
Handler = Callable[[MessageType, dict], Optional[Awaitable[None]]]
SignFn = Callable[[str, str], str]  # (room_id, unique_id) -> signature


def _default_sign(room_id: str, unique_id: str) -> str:
    from signature import get_signature

    return get_signature(room_id, unique_id)


HEARTBEAT_INTERVAL = 10.0  # 秒，对应 RustSocket.ts 的 heartbeatInterval


async def _heartbeat(ws, interval: float = HEARTBEAT_INTERVAL) -> None:
    """定期发送 hb 帧，保活连接（对应 RustSocket.startHeartbeat）。"""
    hb = dy_pb2.PushFrame(payloadType="hb").SerializeToString()
    while True:
        await asyncio.sleep(interval)
        await ws.send(hb)


def decode_message(msg) -> tuple[MessageType, dict]:
    """按 method 分发并解码 payload，返回 (类型, 结构化数据)。"""
    method = msg.method
    payload = msg.payload
    if method == "WebcastChatMessage":
        m = dy_pb2.ChatMessage.FromString(payload)
        return "chat", {
            "user": m.user.nickName,
            "content": m.content,
        }
    if method == "WebcastGiftMessage":
        m = dy_pb2.GiftMessage.FromString(payload)
        return "gift", {
            "user": m.user.nickName,
            "gift": m.gift.name,
            "count": m.repeatCount,
            "diamond": m.gift.diamondCount,
        }
    if method == "WebcastLikeMessage":
        m = dy_pb2.LikeMessage.FromString(payload)
        return "like", {
            "user": m.user.nickName,
            "count": m.count,
            "total": m.total,
        }
    if method == "WebcastMemberMessage":
        m = dy_pb2.MemberMessage.FromString(payload)
        return "comein", {"user": m.user.nickName}
    if method == "WebcastSocialMessage":
        m = dy_pb2.SocialMessage.FromString(payload)
        return "follow", {"user": m.user.nickName, "action": m.action}
    if method == "WebcastRoomUserSeqMessage":
        m = dy_pb2.RoomUserSeqMessage.FromString(payload)
        return "stats", {"total": m.totalUserStr or str(m.totalUser)}
    return method, {}  # 未解析类型：原样返回 method，payload 不做处理


async def _recv_loop(ws, handler: Handler, tag: str = "") -> None:
    """接收循环：解析 PushFrame -> Response -> 分发消息。"""
    prefix = f"[danmaku][{tag}] " if tag else "[danmaku] "
    async for raw in ws:
        frame = dy_pb2.PushFrame.FromString(raw)
        # 心跳等控制帧的 payload 可能不是 gzip，跳过
        try:
            payload = gzip.decompress(frame.payload)
        except (gzip.BadGzipFile, EOFError, OSError):
            continue
        resp = dy_pb2.Response.FromString(payload)
        if resp.needAck:
            ack = dy_pb2.PushFrame(
                payloadType="ack", logId=frame.logId
            ).SerializeToString()
            await ws.send(ack)
        for msg in resp.messagesList:
            try:
                mtype, data = decode_message(msg)
            except Exception as e:
                print(f"{prefix}解码失败 {msg.method}: {e}")
                continue
            result = handler(mtype, data)
            if asyncio.iscoroutine(result):
                await result


async def probe(
    room_id: str,
    unique_id: str,
    ttwid: str,
    cookie_str: str = "",
    user_agent: str = WS_UA,
    sign_fn: SignFn | None = None,
    timeout: float = 12.0,
) -> bool:
    """快速试连弹幕服务器，验证 cookie（含登录态）是否仍有效。

    握手成功并收到首帧视为有效；握手被拒/超时/立即断开视为失效。
    """
    sign = (sign_fn or _default_sign)(room_id, unique_id)
    if asyncio.iscoroutine(sign):
        sign = await sign
    url = WS_URL_TMPL.format(room_id=room_id, unique_id=unique_id, sign=sign)
    headers = {
        "cookie": cookie_str or f"ttwid={ttwid}",
        "user-agent": user_agent,
    }
    try:
        async with websockets.connect(
            url, additional_headers=headers, max_size=None, open_timeout=timeout
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
            return True
    except Exception:
        return False


async def listen(
    room_id: str,
    unique_id: str,
    ttwid: str,
    handler: Handler,
    reconnect_delay: float = 3.0,
    sign_fn: Callable[[], str] | None = None,
    user_agent: str | None = None,
    tag: str = "",
    cookie_str: str | None = None,
) -> None:
    """连接弹幕服务器并持续分发消息，断线自动重连。

    sign_fn: 签名生成函数 (room_id, unique_id) -> str，支持同步或 async。
    每次建连时调用。默认用 Node 版 signature.get_signature，但 Node 环境签名
    会被 DEVICE_BLOCKED 拒绝，生产应传入 browser_sign.BrowserSigner.sign。
    user_agent: 握手 UA，需与签名环境的 UA 一致。
    cookie_str: 完整握手 cookie（实测需含登录态 cookie 才能收到礼物消息）；
    为空时仅用 ttwid。
    """
    headers = {
        "cookie": cookie_str or f"ttwid={ttwid}",
        "user-agent": user_agent or WS_UA,
    }
    prefix = f"[danmaku][{tag}] " if tag else "[danmaku] "
    while True:
        try:
            sign = (sign_fn or _default_sign)(room_id, unique_id)
            if asyncio.iscoroutine(sign):
                sign = await sign
            url = WS_URL_TMPL.format(room_id=room_id, unique_id=unique_id, sign=sign)
            async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
                print(f"{prefix}已连接 room_id={room_id}")
                hb_task = asyncio.create_task(_heartbeat(ws))
                try:
                    await _recv_loop(ws, handler, tag)
                finally:
                    hb_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            detail = str(e)
            # websockets 16.x: 握手被拒时响应体可从异常对象取出
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    body = resp.body
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", "replace")
                    detail += f" | 响应体: {body[:500]}"
                except Exception:
                    pass
            print(f"{prefix}连接异常: {detail}，{reconnect_delay}s 后重连...")
            await asyncio.sleep(reconnect_delay)
