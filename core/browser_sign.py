"""真实浏览器签名模块：通过系统 Edge（headless CDP）生成弹幕 WebSocket 签名。

为什么必须用真实浏览器环境（已实验验证）：
- webmssdk.js 的 frontierSign 依赖真实浏览器环境指纹，Node stub 生成的
  签名会被服务端拒绝（握手返回 DEVICE_BLOCKED），即使 UA 与握手头一致也不行。
- 服务端还会校验 (ttwid, user_unique_id, signature) 的配套关系，因此本模块
  在同一次浏览器会话中同时获取 ttwid 与页面渲染的 user_unique_id。

实现：启动 Edge headless（--remote-debugging-port=0），经 CDP 在直播间页面
上下文中 eval webmssdk.js 并调用 frontierSign，无新增第三方依赖。
"""
import asyncio
import json
import os
import shutil
import subprocess
import time

import requests
import websockets

from core.signature import WEBMSSDK_PATH

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]

_PARAM_TMPL = (
    "live_id=1,aid=6383,version_code=180800,"
    "webcast_sdk_version=1.0.14-beta.0,room_id={room_id},"
    "sub_room_id=,sub_channel_id=,did_rule=3,"
    "user_unique_id={unique_id},device_platform=web,device_type=,ac=,"
    "identity=audience"
)

# 抖音登录态关键 cookie（实测：仅 ttwid 的游客会话收不到礼物消息，
# 需包含登录凭证才能收到 WebcastGiftMessage）
_LOGIN_COOKIE_NAMES = {
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
    "passport_assist_user", "uid_tt", "uid_tt_ss",
}


class BrowserSigner:
    """Edge headless 签名器：签名 + 配套 ttwid / unique_id 获取。"""

    # 默认 Edge profile 放 local/（与 .last_url/.saved_cookie 等状态文件同目录）
    _DEFAULT_PROFILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "local",
        ".edge_profile",
    )

    def __init__(self, profile_dir: str | None = None):
        self._proc: subprocess.Popen | None = None
        self._ws = None
        self._rpc_id = 0
        self._rpc_lock = asyncio.Lock()  # 多房间共享时保证 CDP RPC 不错配
        self._sdk_loaded = False
        self.user_agent = UA
        self.profile_dir = os.path.abspath(profile_dir or self._DEFAULT_PROFILE)

    # profile 内纯缓存目录/文件（删除不影响 cookies 与登录态），防止体积无限增长
    _SLIM_DIRS = frozenset({
        "Cache", "Code Cache", "GrShaderCache", "ShaderCache", "GPUCache",
        "GraphiteDawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
        "component_crx_cache", "Crashpad", "Edge Entity Extraction",
        "EdgeLanguageDetectionModel", "Speech Recognition", "hyphen-data",
        "ZxcvbnData", "Edge Signal Triggers", "Edge Sidebar",
    })
    _SLIM_FILES = ("BrowserMetrics-spare.pma",)

    def _slim_profile(self) -> None:
        """清理 profile 内纯缓存数据（Edge 未运行时调用），控制磁盘占用。"""
        for dirpath, dirnames, _ in os.walk(self.profile_dir):
            for name in list(dirnames):
                if name in self._SLIM_DIRS:
                    shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                    dirnames.remove(name)
        for name in self._SLIM_FILES:
            try:
                os.remove(os.path.join(self.profile_dir, name))
            except OSError:
                pass

    def _edge_args(self, headless: bool) -> list[str]:
        """Edge 启动参数：headless 签名实例与扫码登录实例共用，抑制缓存写入。"""
        args = [
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            f"--user-agent={UA}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-component-update",       # 阻止下载内置组件（AI 模型等）
            "--disable-background-networking",  # 阻止后台流量
            "--disable-crash-reporter",         # 不写崩溃转储
            "--disable-gpu",                    # 减少 GPU 着色器缓存
            "--disk-cache-size=1",              # HTTP 缓存压到最小
        ]
        if headless:
            args += ["--headless=new", "--window-size=1920,1080"]
        return args

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        edge = self._find_edge()
        os.makedirs(self.profile_dir, exist_ok=True)
        self._slim_profile()  # 清理上次运行产生的缓存，控制 profile 体积
        self._proc = subprocess.Popen(
            [edge, *self._edge_args(headless=True), "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._sdk_loaded = False  # 新实例页面为空白，SDK 需重新注入
        port = self._wait_devtools_port()
        target = self._wait_page_target(port)
        self._ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._sdk_loaded = False
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None

    # ---------- 业务接口 ----------

    @staticmethod
    def parse_cookie_str(cookie_str: str) -> "dict[str, str] | None":
        """解析用户输入的 cookie 字符串（形如 k=v; k2=v2）。

        格式错误（存在无 = 的片段，或缺少 ttwid——弹幕握手必需）返回 None。
        """
        pairs: dict[str, str] = {}
        for part in cookie_str.replace("\n", ";").split(";"):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                return None
            k, _, v = part.partition("=")
            pairs[k.strip()] = v.strip()
        if "ttwid" not in pairs:
            return None
        return pairs

    async def apply_user_cookie(self, cookie_str: str) -> bool:
        """把用户手动提供的 cookie 注入 Edge profile（覆盖同名项）。

        注入后需重新 open_room 建立配套的签名会话（user_unique_id 与
        ttwid 必须配套，否则签名会被服务端拒绝）。cookie 无效返回 False。
        """
        pairs = self.parse_cookie_str(cookie_str)
        if not pairs:
            return False
        cookies = [
            {
                "name": k,
                "value": v,
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
            }
            for k, v in pairs.items()
        ]
        await self._rpc("Network.setCookies", {"cookies": cookies})
        self._session = None  # 会话按新 cookie 重建
        return True

    async def reset_to_default(self) -> None:
        """清除全部 cookie（含失效的用户 cookie），回到默认游客会话。

        清除后需重新 open_room，将重新生成仅含 ttwid 的默认会话。
        """
        await self._rpc("Network.clearBrowserCookies", {})
        self._session = None

    async def has_login(self) -> bool:
        """检测当前 Edge profile 是否已登录抖音（含登录凭证 cookie）。

        实测：游客会话（仅 ttwid）收不到礼物消息，需登录态才能收到。
        """
        res = await self._rpc(
            "Network.getCookies",
            {"urls": ["https://www.douyin.com", "https://live.douyin.com"]},
        )
        names = {c["name"] for c in res.get("cookies", [])}
        return bool(names & _LOGIN_COOKIE_NAMES)

    async def login_interactive(self, timeout: float = 300.0) -> bool:
        """弹出有头 Edge 供用户扫码登录抖音，完成后恢复 headless 会话。

        期间签名服务暂停（profile 被有头实例占用）；返回是否登录成功。
        """
        await self.close()
        self._slim_profile()
        proc = subprocess.Popen(
            [self._find_edge(), *self._edge_args(headless=False), "https://live.douyin.com/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port = self._wait_devtools_port()
            target = self._wait_page_target(port)
            self._ws = await websockets.connect(
                target["webSocketDebuggerUrl"], max_size=None
            )
            deadline = time.time() + timeout
            while time.time() < deadline:
                if await self.has_login():
                    return True
                await asyncio.sleep(2.0)
            return False
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except Exception:
                proc.kill()
            if self._ws is not None:
                await self._ws.close()
                self._ws = None
            self._sdk_loaded = False
            await self.start()  # 恢复 headless 签名会话

    async def open_room(self, room_url: str, timeout: float = 40.0) -> tuple[str, str]:
        """打开直播间页面，等待风控挑战完成，返回配套的 (ttwid, unique_id)。"""
        await self._rpc("Page.enable")
        await self._rpc("Page.navigate", {"url": room_url})

        deadline = time.time() + timeout
        while time.time() < deadline:
            cookies = await self._rpc(
                "Network.getCookies", {"urls": ["https://live.douyin.com"]}
            )
            jar = {c["name"]: c["value"] for c in cookies["cookies"]}
            ttwid = jar.get("ttwid", "")
            uid = await self._eval(
                "(document.documentElement.innerHTML.match("
                "/user_unique_id\\\\\":\\\\\"(\\d+)/)||[])[1]||''"
            )
            if ttwid and uid:
                await self._ensure_sdk_async()
                return ttwid, uid
            await asyncio.sleep(1.0)
        raise TimeoutError("等待 ttwid/user_unique_id 超时（页面可能被风控拦截）")

    async def sign(self, room_id: str, unique_id: str) -> str:
        """在真实页面上下文中生成 X-Bogus 签名。"""
        import hashlib

        stub = hashlib.md5(
            _PARAM_TMPL.format(room_id=room_id, unique_id=unique_id).encode()
        ).hexdigest()
        await self._ensure_sdk_async()
        bogus = await self._eval(
            "(window.byted_acrawler||{}).frontierSign"
            "({'X-MS-STUB':'" + stub + "'})['X-Bogus']"
        )
        if not bogus:
            raise RuntimeError("frontierSign 未返回签名（SDK 注入失败？）")
        return bogus

    # ---------- 内部实现 ----------

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        # 响应按 id 匹配，并发调用会互相消费响应，必须互斥
        async with self._rpc_lock:
            return await self._rpc_locked(method, params)

    async def _rpc_locked(self, method: str, params: dict | None = None) -> dict:
        self._rpc_id += 1
        rid = self._rpc_id
        await self._ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=deadline - time.time()))
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} 响应超时")

    async def _eval(self, expression: str) -> str:
        res = await self._rpc(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        return str((res.get("result") or {}).get("value") or "")

    async def _ensure_sdk_async(self) -> None:
        """把 webmssdk.js 注入页面上下文；页面被导航后自动重新注入。"""
        if self._sdk_loaded:
            # 导航会清空页面上下文，验证 SDK 是否仍在
            alive = await self._eval(
                "!!(window.byted_acrawler&&window.byted_acrawler.frontierSign)"
            )
            if alive == "true":
                return
            self._sdk_loaded = False
        with open(WEBMSSDK_PATH, "r", encoding="utf-8") as f:
            sdk = f.read()
        expr = (
            "(function(){try{eval(" + json.dumps(sdk) + ")}"
            "catch(e){return 'EVAL_ERR:'+e.message}"
            "return (window.byted_acrawler||{}).frontierSign?'ok':'NO_FRONTIERSIGN'})()"
        )
        result = await self._eval(expr)
        if result != "ok":
            raise RuntimeError(f"webmssdk.js 注入失败: {result}")
        self._sdk_loaded = True

    def _find_edge(self) -> str:
        for p in _EDGE_CANDIDATES:
            if os.path.exists(p):
                return p
        raise RuntimeError(
            "未找到 Edge 浏览器。签名依赖真实浏览器环境（Node 生成的签名会被"
            " DEVICE_BLOCKED 拒绝），请安装 Microsoft Edge 后重试。"
        )

    def _wait_devtools_port(self, timeout: float = 30.0) -> int:
        port_file = os.path.join(self.profile_dir, "DevToolsActivePort")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(port_file):
                try:
                    with open(port_file, "r", encoding="utf-8") as f:
                        port = int(f.readline().strip())
                    requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    return port
                except Exception:
                    pass
            time.sleep(0.3)
        raise TimeoutError("等待 Edge DevTools 端口超时")

    def _wait_page_target(self, port: int) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    return pages[0]
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError("未找到 Edge 页面 target")
