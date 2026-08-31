"""LiveBox Python 版（核心采集链路 + GUI 界面）。

用法:
    双击 main.py（或 `python main.py`） -> 启动 GUI 图形界面
    python main.py --cli               -> 终端交互模式（可多房间并行采集）

GUI: 顶部输入直播间地址 -> 开始采集；左侧主播信息与内嵌 HLS 播放，
     右侧弹幕消息列表（类型过滤/录制配置）；"新窗口"按钮多开房间
     （对应 Tauri 版 open_window，所有窗口共享一个签名会话）。

弹幕默认保存到当前目录下以启动时间戳命名的 txt 文档（GUI 可配置录制项）。

链路: 房间信息抓取(douyin_api) -> 真实浏览器签名与设备会话(browser_sign)
      -> 弹幕连接解析(danmaku)

签名依赖系统 Edge（headless）在真实浏览器环境生成：Node 环境签名会被
服务端 DEVICE_BLOCKED 拒绝（已实验验证）。礼物消息需要登录态 cookie，
程序会在启动采集前检测并提醒（游客会话仅含 ttwid，收不到礼物消息）。
"""
import asyncio
import os
import re
import sys
import threading
import time

import requests

from core import danmaku
from core.browser_sign import BrowserSigner
from core.douyin_api import UA, get_flv_url, get_live_info
from core.player import find_player, open_stream

try:  # GUI 依赖可选，未安装时回退终端模式
    from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
    from PyQt6.QtGui import QColor, QIcon, QPixmap
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QVBoxLayout,
        QWidget,
    )
    _HAS_PYQT = True
except ImportError:
    _HAS_PYQT = False

MAX_MSG_ITEMS = 500  # GUI 消息列表上限，超出丢弃最旧的（对应虚拟滚动）

# GUI 窗口图标（左上角 logo，src/222.ico 相对 main.py 所在目录）
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "222.ico")


def _app_icon() -> "QIcon | None":
    if os.path.exists(ICON_PATH):
        return QIcon(ICON_PATH)
    return None


TYPE_COLORS = {
    "chat": QColor("#1f2328") if _HAS_PYQT else None,
    "gift": QColor("#e65100") if _HAS_PYQT else None,
    "like": QColor("#8a8f98") if _HAS_PYQT else None,
    "comein": QColor("#5b7fd4") if _HAS_PYQT else None,
    "follow": QColor("#1a7f37") if _HAS_PYQT else None,
}


# =================================================================
# 核心采集链路（CLI 与 GUI 共用）
# =================================================================

def format_msg(mtype: str, data: dict) -> str:
    if mtype == "chat":
        return f"[弹幕] {data['user']}: {data['content']}"
    if mtype == "gift":
        return f"[礼物] {data['user']} 送出 {data['gift']} x{data['count']}"
    if mtype == "like":
        return f"[点赞] {data['user']} 赞了 {data['count']} 次 (本场 {data['total']})"
    if mtype == "comein":
        return f"[进入] {data['user']} 来了"
    if mtype == "follow":
        return f"[关注] {data['user']} 关注了主播"
    if mtype == "stats":
        return f"[场观] {data['total']}"
    return None  # 未解析的消息类型不展示


async def ensure_login(signer, refresh_url: str) -> tuple[str, str] | None:
    """检测登录态并提醒用户决策。

    返回刷新后的 (ttwid, unique_id)（登录成功时），继续用旧会话则返回 None。
    实测：游客会话（仅 ttwid）收不到礼物消息，需登录态。
    """
    if await signer.has_login():
        return None
    print("⚠ 当前为游客会话(仅 ttwid)，抖音不推送礼物消息，收益统计将缺失。")
    choice = input("   是否打开浏览器登录抖音? (y/N): ").strip().lower() == "y"
    if not choice:
        print("   将以游客会话继续采集（无礼物消息）。")
        return None
    print("   已弹出 Edge 窗口，请扫码登录抖音（5 分钟内完成）...")
    if await signer.login_interactive():
        print("   登录成功，刷新设备会话...")
        return await signer.open_room(refresh_url)
    print("   登录超时/未完成，将以游客会话继续。")
    return None


async def run(
    url: str,
    save_path: str,
    play: bool = False,
    tag: str = "",
    signer: "BrowserSigner | None" = None,
    shared: "tuple[str, str] | None" = None,
    cookie: str = "",
    cookie_str: str = "",
) -> None:
    """采集单个直播间。

    tag: 房间标识（多房间时输出前缀）；signer/shared: 多房间模式下传入
    共享签名器与设备会话 (ttwid, unique_id)，避免每个房间起独立 Edge。
    cookie: 用户手动输入的 cookie（多房间模式由外部统一处理，置空即可）。
    cookie_str: 已验证有效的完整握手 cookie（shared 模式由外部传入）。
    """
    prefix = f"[{tag}] " if tag else ""
    print(f"{prefix}抓取直播间信息: {url}")
    info = get_live_info(url)
    owner = info.room_info.get("owner", {})
    print(
        f"{prefix}      room_id={info.room_id} 主播={owner.get('nickname')} "
        f"状态={'直播中' if info.is_live else '已下播'}"
    )
    flv = get_flv_url(info)
    if flv:
        print(f"{prefix}      FLV流: {flv[:80]}...")

    # 启动直播流播放（对应 Tauri 版 DPlayer；多房间 = 多播放窗口）
    play_proc = None
    if play:
        cmd = find_player()
        if not flv:
            print(f"{prefix}      [播放] 未获取到 FLV 流地址，跳过播放")
        elif not cmd:
            print(f"{prefix}      [播放] 未找到播放器（需安装 mpv / ffmpeg / VLC 之一），跳过播放")
        else:
            play_proc = open_stream(cmd, flv)
            print(f"{prefix}      [播放] 已用 {cmd[0].split(chr(92))[-1]} 打开直播画面")

    if not info.is_live:
        print(f"{prefix}该直播间已下播，无弹幕可采集。")
        return

    if signer is None:
        signer = BrowserSigner()
    try:
        if shared:
            ttwid, unique_id = shared
        else:
            await signer.start()
            session = await setup_cookie(signer, info, url, cookie)
            if session:
                ttwid, unique_id, cookie_str = session
            else:
                ttwid, unique_id = await signer.open_room(url)
            print(f"{prefix}      ttwid={ttwid[:20]}... unique_id={unique_id}")
            if not cookie_str:
                # 默认游客会话才提醒登录（礼物消息需要登录态）
                refreshed = await ensure_login(signer, url)
                if refreshed:
                    ttwid, unique_id = refreshed

        save_fp = open(save_path, "a", encoding="utf-8") if save_path else None
        income = {"diamond": 0}  # 主播收益（音浪），对应 Tauri 版 diamond 统计

        def handler(mtype: str, data: dict):
            line = format_msg(mtype, data)
            if line is None:
                return
            if mtype == "gift":
                income["diamond"] += data.get("diamond", 0) * int(data.get("count") or 1)
                line += f"  [本场收益 {income['diamond']} 音浪]"
            print(f"{prefix}{line}")
            # 仅保存礼物与文字聊天消息
            if save_fp and mtype in ("chat", "gift"):
                save_fp.write(line + "\n")
                save_fp.flush()

        try:
            await danmaku.listen(
                info.room_id,
                unique_id,
                ttwid,
                handler,
                sign_fn=signer.sign,
                user_agent=signer.user_agent,
                tag=tag,
                cookie_str=cookie_str or None,
            )
        finally:
            if save_fp:
                save_fp.close()
            if play_proc:
                play_proc.terminate()
                print(f"{prefix}[播放] 已关闭直播画面")
    finally:
        if shared is None:  # 多房间模式下签名器由外部统一关闭
            await signer.close()


async def run_multi(urls: "list[str]", stamp: str, play: bool, cookie: str = "") -> None:
    """多直播间并行采集（对应 Tauri 版多窗口）：共享一个 Edge 设备会话，
    每个房间独立弹幕流、独立保存文件、独立播放窗口。"""
    signer = BrowserSigner()
    print(f"启动 Edge headless 会话（{len(urls)} 个直播间共享设备指纹）...")
    try:
        await signer.start()
        # 提取第一个房间信息用于 cookie 握手验证（probe 需要 room_id）
        info0 = get_live_info(urls[0])
        used_cookie = ""
        if cookie:
            session = await setup_cookie(signer, info0, urls[0], cookie)
            if session:
                ttwid, uid, used_cookie = session
            else:
                ttwid, uid = await signer.open_room(urls[0])
        else:
            # ttwid / user_unique_id 是设备级会话，多房间共用一组即可
            ttwid, uid = await signer.open_room(urls[0])
        print(f"      设备会话: ttwid={ttwid[:20]}... unique_id={uid}")
        if not used_cookie:
            refreshed = await ensure_login(signer, urls[0])
            if refreshed:
                ttwid, uid = refreshed

        async def one(url: str) -> None:
            tag = url.rstrip("/").rsplit("/", 1)[-1] or url
            try:
                await run(
                    url,
                    f"{stamp}_{tag}.txt",
                    play,
                    tag=tag,
                    signer=signer,
                    shared=(ttwid, uid),
                    cookie_str=used_cookie,
                )
            except Exception as e:
                print(f"[{tag}] 启动失败: {e}")

        await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    finally:
        await signer.close()


_ROOT = os.path.dirname(os.path.abspath(__file__))  # python/

_HISTORY_FILE = os.path.join(_ROOT, "local", ".last_url")

_SAVED_COOKIE_FILE = os.path.join(_ROOT, "local", ".saved_cookie")


def _load_saved_cookie() -> str:
    """读取上次保存的用户 cookie（仅含登录态 sessionid 的才保存）。"""
    try:
        with open(_SAVED_COOKIE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_saved_cookie(cookie_str: str) -> None:
    try:
        with open(_SAVED_COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)
    except OSError:
        pass


def _clear_saved_cookie() -> None:
    try:
        os.remove(_SAVED_COOKIE_FILE)
    except OSError:
        pass


async def setup_cookie(
    signer: BrowserSigner,
    info,
    url: str,
    cookie_input: str,
) -> "tuple[str, str, str] | None":
    """应用用户/已保存 cookie 并握手验证有效性。

    返回 (ttwid, unique_id, cookie_str)；cookie 无效/验证失败时自动回退
    默认（清除注入的 cookie，重新生成仅 ttwid 的游客会话）并返回 None，
    由调用方重新 open_room。
    """
    if not cookie_input:
        return None
    if not await signer.apply_user_cookie(cookie_input):
        print("⚠ cookie格式无效(需形如 k=v; k2=v2 且含 ttwid)，将使用默认cookie")
        return None
    ttwid, uid = await signer.open_room(url)
    ok = await danmaku.probe(
        info.room_id,
        uid,
        ttwid,
        cookie_str=cookie_input,
        user_agent=signer.user_agent,
        sign_fn=signer.sign,
    )
    if not ok:
        # 失效：清除注入并回退默认游客会话（不保存失效 cookie）
        print("⚠ cookie已失效，已自动改用默认cookie（仅含ttwid，不保存）")
        _clear_saved_cookie()
        await signer.reset_to_default()
        return None
    pairs = BrowserSigner.parse_cookie_str(cookie_input) or {}
    if "sessionid" in pairs:
        _save_saved_cookie(cookie_input)  # 含登录态才保存，下次自动续用
        print("      已使用用户提供的cookie(含登录态，下次运行自动续用)")
    else:
        print("      已使用用户提供的cookie")
    return ttwid, uid, cookie_input


def _load_last_url() -> str:
    """读取上次输入的直播间地址（对应 Tauri 版 localStorage 记忆）。"""
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_last_url(url: str) -> None:
    try:
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(url)
    except OSError:
        pass


# =================================================================
# GUI 界面（对应 Tauri 版 src/App.vue）
# =================================================================

if _HAS_PYQT:

    class SignerBridge:
        """专用线程中运行 BrowserSigner，供所有采集线程共享。

        BrowserSigner 的 CDP WebSocket 绑定创建它的 asyncio loop，因此放在
        常驻签名线程中，采集线程通过 run_coroutine_threadsafe 调用。
        ttwid / user_unique_id 是设备级会话，全部窗口共用一组。
        """

        def __init__(self):
            self._loop = None
            self._signer: BrowserSigner | None = None
            self._session = None

        def start(self) -> None:
            threading.Thread(target=self._run, daemon=True, name="signer").start()

        def _run(self) -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        def _submit(self, coro, timeout: float = 120):
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

        def get_session(self, url: str) -> tuple[BrowserSigner, tuple[str, str]]:
            """获取共享签名器与设备会话（首次会打开直播间页面）。"""
            return self._submit(self._get_session(url))

        def get_session_with_cookie(self, url: str, cookie_str: str):
            """用用户提供的 cookie 建立签名会话。

            返回 (signer, (ttwid, uid), applied)；cookie 无效时 applied=False
            并回退默认会话。
            """
            return self._submit(self._get_session_cookie(url, cookie_str))

        async def _get_session_cookie(self, url, cookie_str):
            if self._signer is None:
                self._signer = BrowserSigner()
                await self._signer.start()
            applied = await self._signer.apply_user_cookie(cookie_str)
            if applied:
                # 按新 cookie 重建会话（user_unique_id 与 ttwid 需配套）
                self._session = await self._signer.open_room(url)
            elif self._session is None:
                self._session = await self._signer.open_room(url)
            return self._signer, self._session, applied

        async def _get_session(self, url):
            if self._signer is None:
                self._signer = BrowserSigner()
                await self._signer.start()
            if self._session is None:
                self._session = await self._signer.open_room(url)
            return self._signer, self._session

        def sign(self, room_id: str, unique_id: str) -> str:
            """同步签名接口，供 danmaku.listen 的 sign_fn 使用。"""
            return self._submit(self._signer.sign(room_id, unique_id), timeout=30)

        def has_login(self) -> bool:
            """检测共享 Edge profile 是否已登录抖音。"""
            return self._submit(self._signer.has_login(), timeout=15)

        def login(self) -> bool:
            """弹出有头 Edge 供用户扫码登录，完成后刷新设备会话。"""
            ok = self._submit(self._signer.login_interactive(), timeout=420)
            self._session = None  # 登录可能刷新 ttwid/user_unique_id，强制重建
            return ok

        def reset(self) -> None:
            """清除失效的用户 cookie，回到默认游客会话。"""
            self._submit(self._signer.reset_to_default(), timeout=15)
            self._session = None

        def stop(self) -> None:
            if self._loop is None:
                return

            async def _close():
                if self._signer is not None:
                    await self._signer.close()
                    self._signer = None

            try:
                asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    class CollectWorker(QThread):
        """单房间采集线程：抓房间信息 -> 连接弹幕 -> 信号分发消息。"""

        info_ready = pyqtSignal(dict)   # 房间信息（含头像 bytes / HLS / FLV）
        message = pyqtSignal(str, object)
        status = pyqtSignal(str)
        login_prompt = pyqtSignal()     # 游客会话提醒（礼物消息需登录态）
        login_result = pyqtSignal(bool)
        stopped = pyqtSignal()

        def __init__(self, url: str, bridge: "SignerBridge", parent=None, cookie: str = ""):
            super().__init__(parent)
            self.url = url
            self.bridge = bridge
            self.cookie = cookie
            self.used_cookie = ""  # 验证有效并实际用于握手的 cookie 串
            self._loop = None
            self._task = None
            self.login_decided = threading.Event()
            self.login_wanted = False

        def decide_login(self, want: bool) -> None:
            """主线程回传用户决定（是否登录）。"""
            self.login_wanted = want
            self.login_decided.set()

        def run(self) -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._main())
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.status.emit(f"异常退出: {e}")
            finally:
                self._loop.close()
                self.stopped.emit()

        def stop(self) -> None:
            self.login_decided.set()  # 唤醒可能等待登录决策的线程
            if self._loop and self._task:
                self._loop.call_soon_threadsafe(self._task.cancel)

        async def _main(self) -> None:
            self.status.emit("启动签名会话...")
            loop = asyncio.get_running_loop()
            used_cookie = ""
            if self.cookie:
                signer, session, applied = await loop.run_in_executor(
                    None, self.bridge.get_session_with_cookie, self.url, self.cookie
                )
                if not applied:
                    ttwid, uid = session
                    self.status.emit("⚠ cookie格式无效，已改用默认cookie（无礼物消息）")
                else:
                    ttwid, uid = session
                    self.status.emit("验证 cookie 有效性...")
                    # 握手试连验证；失效则自动回退默认（不保存失效 cookie）
                    info = get_live_info(self.url)
                    ok = await danmaku.probe(
                        info.room_id,
                        uid,
                        ttwid,
                        cookie_str=self.cookie,
                        user_agent=signer.user_agent,
                        sign_fn=self.bridge.sign,
                    )
                    if ok:
                        pairs = BrowserSigner.parse_cookie_str(self.cookie) or {}
                        if "sessionid" in pairs:
                            _save_saved_cookie(self.cookie)  # 下次自动续用
                        used_cookie = self.cookie
                        self.status.emit("已使用用户提供的cookie")
                    else:
                        _clear_saved_cookie()
                        await loop.run_in_executor(None, self.bridge.reset)
                        signer, (ttwid, uid) = await loop.run_in_executor(
                            None, self.bridge.get_session, self.url
                        )
                        self.status.emit(
                            "⚠ cookie已失效，已改用默认cookie（仅含ttwid，不保存）"
                        )
            else:
                signer, (ttwid, uid) = await loop.run_in_executor(
                    None, self.bridge.get_session, self.url
                )
                self.status.emit("未输入cookie，使用默认cookie（无礼物消息）")
            self.used_cookie = used_cookie

            # 礼物消息需要登录态 cookie：游客会话提醒用户决策（实测验证）
            if not await loop.run_in_executor(None, self.bridge.has_login):
                self.login_prompt.emit()
                await loop.run_in_executor(None, self.login_decided.wait)
                if self.login_wanted:
                    self.status.emit("等待浏览器登录（最长 5 分钟）...")
                    ok = await loop.run_in_executor(None, self.bridge.login)
                    self.login_result.emit(ok)
                    if ok:
                        signer, (ttwid, uid) = await loop.run_in_executor(
                            None, self.bridge.get_session, self.url
                        )

            self.status.emit("抓取直播间信息...")
            info = get_live_info(self.url)
            owner = info.room_info.get("owner", {})
            avatar_bytes = b""
            avatar_url = (owner.get("avatar_thumb") or {}).get("url_list", [""])[0]
            if avatar_url:
                try:
                    avatar_bytes = requests.get(
                        avatar_url, headers={"user-agent": UA}, timeout=10
                    ).content
                except Exception:
                    pass

            stream = info.room_info.get("stream_url", {})
            hls = stream.get("hls_pull_url") or next(
                iter(stream.get("hls_pull_url_map", {}).values()), ""
            )
            self.info_ready.emit(
                {
                    "room_id": info.room_id,
                    "nickname": owner.get("nickname") or "未知主播",
                    "avatar": avatar_bytes,
                    "is_live": info.is_live,
                    "online": info.room_info.get("user_count_str", "0"),
                    "total_like": (info.room_info.get("stats") or {}).get(
                        "total_user_str", "0"
                    ),
                    "hls": hls,
                    "flv": get_flv_url(info),
                }
            )
            if not info.is_live:
                self.status.emit("直播已结束")
                return

            self.status.emit("已连接弹幕服务器")
            self._task = asyncio.ensure_future(
                danmaku.listen(
                    info.room_id,
                    uid,
                    ttwid,
                    self._on_msg,
                    sign_fn=self.bridge.sign,
                    user_agent=signer.user_agent,
                    cookie_str=self.used_cookie or None,
                )
            )
            try:
                await self._task
            except asyncio.CancelledError:
                raise

        def _on_msg(self, mtype: str, data: dict):
            self.message.emit(mtype, data)

    class RoomWindow(QMainWindow):
        _windows: "list[RoomWindow]" = []

        def __init__(self, bridge: "SignerBridge"):
            super().__init__()
            self.bridge = bridge
            self.worker: "CollectWorker | None" = None
            self.room_id = ""
            self.save_fp = None
            self.income = 0
            self.total_like = ""
            self.player: "QMediaPlayer | None" = None
            self.audio: "QAudioOutput | None" = None
            self._flv_url = ""
            self._init_ui()
            RoomWindow._windows.append(self)

        # ---------- UI ----------
        def _init_ui(self) -> None:
            self.setWindowTitle("LiveBox")
            icon = _app_icon()  # 左上角 logo
            if icon:
                self.setWindowIcon(icon)
            self.resize(1160, 700)

            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(10, 10, 10, 6)
            root.setSpacing(8)

            # 顶部：地址 + 按钮（对应 App.vue liveUrl 区）
            top = QHBoxLayout()
            self.url_edit = QLineEdit()
            self.url_edit.setPlaceholderText("请输入直播间地址")
            last_url = _load_last_url()  # 记住上次采集的直播间地址
            if last_url:
                self.url_edit.setText(last_url)
            self.start_btn = QPushButton("开始采集")
            self.start_btn.setFixedWidth(90)
            self.stop_btn = QPushButton("停止")
            self.stop_btn.setFixedWidth(64)
            self.stop_btn.setEnabled(False)
            self.new_window_btn = QPushButton("新窗口")
            self.new_window_btn.setFixedWidth(72)
            top.addWidget(self.url_edit, 1)
            top.addWidget(self.start_btn)
            top.addWidget(self.stop_btn)
            top.addWidget(self.new_window_btn)
            root.addLayout(top)

            # 第二行：用户手动输入 cookie（可选；空/无效回退默认并提醒）
            cookie_row = QHBoxLayout()
            cookie_row.addWidget(QLabel("Cookie:"))
            self.cookie_edit = QLineEdit()
            self.cookie_edit.setPlaceholderText(
                "粘贴抖音cookie(浏览器F12复制, 可选)；留空或无效将使用默认cookie"
            )
            saved_cookie = _load_saved_cookie()
            if saved_cookie:
                self.cookie_edit.setText(saved_cookie)  # 上次保存的登录 cookie 预填
            cookie_row.addWidget(self.cookie_edit, 1)
            root.addLayout(cookie_row)

            # 主体左右分栏（对应 App.vue liveBox 区）
            splitter = QSplitter(Qt.Orientation.Horizontal)
            root.addWidget(splitter, 1)

            # 左侧：主播信息 + 视频
            left = QWidget()
            left_v = QVBoxLayout(left)
            left_v.setContentsMargins(0, 0, 0, 0)
            left_v.setSpacing(6)

            owner = QHBoxLayout()
            self.avatar_label = QLabel("📺")
            self.avatar_label.setFixedSize(52, 52)
            self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.avatar_label.setStyleSheet(
                "border-radius:26px;background:#e8eaed;font-size:24px;"
            )
            nick_v = QVBoxLayout()
            self.nick_label = QLabel("未连接")
            self.nick_label.setStyleSheet("font-weight:600;font-size:15px;")
            self.like_label = QLabel("本场点赞 0")
            self.like_label.setStyleSheet("color:#8a8f98;font-size:12px;")
            nick_v.addWidget(self.nick_label)
            nick_v.addWidget(self.like_label)
            owner.addWidget(self.avatar_label)
            owner.addLayout(nick_v)
            owner.addStretch(1)
            left_v.addLayout(owner)

            self.video = QVideoWidget()
            self.video.setStyleSheet("background:black;border-radius:6px;")
            left_v.addWidget(self.video, 1)

            play_row = QHBoxLayout()
            self.external_btn = QPushButton("用系统播放器打开(FLV)")
            self.external_btn.setEnabled(False)
            self.external_btn.clicked.connect(self.open_external)
            play_row.addWidget(self.external_btn)
            play_row.addStretch(1)
            left_v.addLayout(play_row)

            # 右侧：消息列表 + 过滤/录制设置
            right = QWidget()
            right_v = QVBoxLayout(right)
            right_v.setContentsMargins(0, 0, 0, 0)
            right_v.setSpacing(6)

            self.msg_list = QListWidget()
            self.msg_list.setAlternatingRowColors(True)
            self.msg_list.setStyleSheet("QListWidget::item{padding:4px 6px;}")
            right_v.addWidget(self.msg_list, 1)

            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("显示:"))
            self.show_filters: dict[str, QCheckBox] = {}
            for mtype, text in (
                ("chat", "聊天"), ("gift", "礼物"), ("like", "点赞"),
                ("follow", "关注"), ("comein", "进来"),
            ):
                cb = QCheckBox(text)
                cb.setChecked(True)
                self.show_filters[mtype] = cb
                filter_row.addWidget(cb)
            filter_row.addStretch(1)
            right_v.addLayout(filter_row)

            record_row = QHBoxLayout()
            record_row.addWidget(QLabel("录制:"))
            self.record_chat = QCheckBox("录制弹幕")
            self.record_chat.setChecked(True)
            self.record_gift = QCheckBox("录制礼物")
            self.record_gift.setChecked(True)
            record_row.addWidget(self.record_chat)
            record_row.addWidget(self.record_gift)
            record_row.addStretch(1)
            right_v.addLayout(record_row)

            splitter.addWidget(left)
            splitter.addWidget(right)
            splitter.setSizes([640, 480])

            # 状态栏（对应 likeInfo 区的在线观众 / 主播收益）
            self.statusBar().showMessage("就绪")

            # 信号
            self.start_btn.clicked.connect(self.start_collect)
            self.stop_btn.clicked.connect(self.stop_collect)
            self.new_window_btn.clicked.connect(self.open_new_window)

        # ---------- 采集控制 ----------
        def start_collect(self) -> None:
            url = self.url_edit.text().strip()
            if not url:
                self.statusBar().showMessage("请输入直播间地址")
                return
            _save_last_url(url)  # 记住本次地址，下次启动自动回填
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.worker = CollectWorker(
                url, self.bridge, cookie=self.cookie_edit.text().strip()
            )
            self.worker.info_ready.connect(self.on_info_ready)
            self.worker.message.connect(self.on_message)
            self.worker.status.connect(lambda s: self.statusBar().showMessage(s))
            self.worker.login_prompt.connect(self.on_login_prompt)
            self.worker.login_result.connect(self.on_login_result)
            self.worker.stopped.connect(self.on_stopped)
            self.worker.start()

        def stop_collect(self) -> None:
            if self.worker:
                self.worker.stop()

        def on_stopped(self) -> None:
            self.stop_media()
            if self.save_fp:
                self.save_fp.close()
                self.save_fp = None
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("已停止")

        def on_login_prompt(self) -> None:
            """游客会话提醒：礼物消息需要登录态 cookie（实测验证）。"""
            ret = QMessageBox.question(
                self,
                "登录提醒",
                "当前为游客会话（仅 ttwid），抖音不会推送礼物消息，\n"
                "主播收益统计将缺失。\n\n"
                "是否打开浏览器登录抖音账号？（扫码登录，最长等待 5 分钟）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            want = ret == QMessageBox.StandardButton.Yes
            self.statusBar().showMessage(
                "等待浏览器登录..." if want else "游客会话采集中（无礼物消息）"
            )
            if self.worker:
                self.worker.decide_login(want)

        def on_login_result(self, ok: bool) -> None:
            self.statusBar().showMessage(
                "登录成功：礼物消息与收益统计可用"
                if ok
                else "登录未完成：以游客会话继续（无礼物消息）"
            )

        def on_info_ready(self, payload: dict) -> None:
            self.room_id = payload["room_id"]
            self.nick_label.setText(payload["nickname"])
            self.total_like = payload["total_like"]
            self.like_label.setText(f"本场点赞 {self.total_like}")
            if payload["avatar"]:
                pm = QPixmap()
                if pm.loadFromData(payload["avatar"]):
                    self.avatar_label.setPixmap(
                        pm.scaled(
                            52, 52,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
            if not payload["is_live"]:
                self.statusBar().showMessage("直播已结束")
                self.stop_collect()
                return

            stamp = time.strftime("%y%m%d_%H%M%S")
            self.save_fp = open(f"{stamp}_{self.room_id}.txt", "a", encoding="utf-8")
            self.setWindowTitle(f"LiveBox - {payload['nickname']}")
            self.play_media(payload["hls"])
            self.external_btn.setEnabled(bool(payload["flv"]))
            self._flv_url = payload["flv"]

        def on_message(self, mtype: str, data: dict) -> None:
            line = None
            if mtype == "chat":
                line = f"{data['user']}：{data['content']}"
            elif mtype == "gift":
                self.income += data.get("diamond", 0) * int(data.get("count") or 1)
                line = f"{data['user']} 送出 {data['gift']} x{data['count']}"
            elif mtype == "like":
                self.total_like = str(data.get("total", ""))
                self.like_label.setText(f"本场点赞 {self.total_like}")
                line = f"{data['user']} 赞了 {data.get('count', 1)} 次"
            elif mtype == "comein":
                line = f"{data['user']} 来了"
            elif mtype == "follow":
                line = f"{data['user']} 关注了主播"
            elif mtype == "stats":
                self.statusBar().showMessage(
                    f"本场场观: {data.get('total', '-')}    主播收益: {self.income} 音浪"
                )
                return

            # 录制（对应 recordVideo 配置；格式与 CLI 版一致）
            if self.save_fp and (
                (mtype == "chat" and self.record_chat.isChecked())
                or (mtype == "gift" and self.record_gift.isChecked())
            ):
                tag = {"chat": "弹幕", "gift": "礼物"}.get(mtype, mtype)
                self.save_fp.write(f"[{tag}] {line}\n")
                self.save_fp.flush()

            # 显示过滤（对应 checkList）
            if line and self.show_filters.get(mtype) and self.show_filters[mtype].isChecked():
                item = QListWidgetItem(line)
                color = TYPE_COLORS.get(mtype)
                if color:
                    item.setForeground(color)
                self.msg_list.addItem(item)
                if self.msg_list.count() > MAX_MSG_ITEMS:
                    self.msg_list.takeItem(0)
                self.msg_list.scrollToBottom()
            if mtype == "gift":
                self.statusBar().showMessage(
                    f"主播收益: {self.income} 音浪"
                )

        # ---------- 播放 ----------
        def play_media(self, hls_url: str) -> None:
            """内嵌播放 HLS 流（对应 DPlayer）。"""
            if not hls_url:
                return
            self.audio = QAudioOutput(self)
            self.audio.setVolume(0.6)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.setSource(QUrl(hls_url))
            self.player.play()

        def stop_media(self) -> None:
            if self.player:
                self.player.stop()
                self.player = None
            self.audio = None

        def open_external(self) -> None:
            cmd = find_player()
            if cmd and self._flv_url:
                open_stream(cmd, self._flv_url)

        def open_new_window(self) -> None:
            win = RoomWindow(self.bridge)
            win.show()
            # 继承当前地址便于快速采集
            win.url_edit.setText(self.url_edit.text())

        def closeEvent(self, event) -> None:
            if self.worker:
                self.worker.stop()
                self.worker.wait(3000)
            self.stop_media()
            if self.save_fp:
                self.save_fp.close()
                self.save_fp = None
            if self in RoomWindow._windows:
                RoomWindow._windows.remove(self)
            # 最后一个窗口关闭时退出整个应用（并停签名线程）
            if not RoomWindow._windows:
                QApplication.instance().quit()
            event.accept()


def gui_main() -> None:
    """GUI 入口：双击 main.py 默认启动。"""
    if not _HAS_PYQT:
        print("未安装 PyQt6，无法启动图形界面。请先执行: pip install PyQt6")
        print("回退到终端模式。\n")
        cli_main()
        return
    app = QApplication([])
    icon = _app_icon()
    if icon:
        app.setWindowIcon(icon)  # 全局默认图标（含对话框/新窗口）
    bridge = SignerBridge()
    bridge.start()
    app.aboutToQuit.connect(bridge.stop)
    win = RoomWindow(bridge)
    win.show()
    app.exec()


# =================================================================
# CLI 终端模式（python main.py --cli）
# =================================================================

def cli_main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    last = _load_last_url()
    if last:
        url_input = input(f"请输入直播间URL(多个用空格分隔, 回车使用上次: {last}): ").strip()
        if not url_input:
            url_input = last
    else:
        url_input = input("请输入直播间URL(多个用空格分隔): ").strip()
    urls = [u for u in re.split(r"[\s,，]+", url_input) if u]
    if not urls:
        print("URL 不能为空。")
        return
    _save_last_url(" ".join(urls))
    # 用户手动输入 cookie（浏览器 F12 复制；空/无效则回退默认）
    saved_cookie = _load_saved_cookie()
    cookie_input = input("请粘贴抖音cookie(可选, 直接回车使用默认/已保存): ").strip()
    if not cookie_input and saved_cookie:
        print("      将使用已保存的登录cookie（失效时自动回退默认cookie）。")
        cookie_input = saved_cookie
    elif not cookie_input:
        print("未输入cookie，将使用默认cookie（游客会话，无礼物消息）。")
    stamp = time.strftime("%y%m%d_%H%M%S")
    if len(urls) > 1:
        print(f"弹幕将保存到: {stamp}_<房间号>.txt（每个房间一个文件）")
    else:
        print(f"弹幕将保存到: {stamp}.txt")
    play = input("是否播放直播画面? (Y/n): ").strip().lower() != "n"
    try:
        if len(urls) == 1:
            asyncio.run(run(urls[0], stamp + ".txt", play, cookie=cookie_input))
        else:
            asyncio.run(run_multi(urls, stamp, play, cookie=cookie_input))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    if "--cli" in sys.argv or "-c" in sys.argv:
        cli_main()
    else:
        gui_main()  # 双击 main.py 默认启动 GUI
