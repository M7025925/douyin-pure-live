"""抖音 WebSocket 签名模块（生成 URL 中的 signature 参数，即 X-Bogus）。

与前端 vFun.js 中 window.creatSignature 逻辑一致：
1. 拼接固定参数串（含 room_id / user_unique_id）
2. md5 后得到 X-MS-STUB
3. 调 webmssdk.js 的 frontierSign 生成 X-Bogus

webmssdk.js 为 UMD 混淆包：在 Node CJS 中 direct eval 后，
frontierSign 同步挂载到 module.exports 顶层，故通过 Node 子进程调用最可靠。
"""
import hashlib
import os
import shutil
import subprocess

_DIR = os.path.dirname(os.path.abspath(__file__))  # core/
_ROOT = os.path.dirname(_DIR)  # python/
WEBMSSDK_PATH = os.path.join(_ROOT, "js", "webmssdk.js")

# 与 vFun.js creatSignature 中 o.substring(1) 一致
_PARAM_TMPL = (
    "live_id=1,aid=6383,version_code=180800,"
    "webcast_sdk_version=1.0.14-beta.0,room_id={room_id},"
    "sub_room_id=,sub_channel_id=,did_rule=3,"
    "user_unique_id={unique_id},device_platform=web,device_type=,ac=,"
    "identity=audience"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)

# 浏览器环境 stub：webmssdk.js 内部会访问 document/location 等（用 globalThis 显式挂载）
_ENV_JS = """
globalThis.window = globalThis;
globalThis.navigator = { userAgent: '%(ua)s', appName: 'Netscape', platform: 'Win32',
  language: 'zh-CN', cookieEnabled: true, plugins: [], mimeTypes: [] };
globalThis.location = { href: 'https://live.douyin.com/', protocol: 'https:',
  host: 'live.douyin.com', hostname: 'live.douyin.com', port: '',
  pathname: '/', search: '', hash: '', origin: 'https://live.douyin.com' };
globalThis.screen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
  colorDepth: 24, pixelDepth: 24 };
globalThis.history = { pushState: function(){}, replaceState: function(){},
  back: function(){}, forward: function(){}, go: function(){} };
globalThis.performance = { now: function(){ return Date.now(); }, timing: {},
  getEntries: function(){ return []; } };
var __stubStorage = { getItem: function(){ return null; }, setItem: function(){},
  removeItem: function(){}, clear: function(){} };
globalThis.localStorage = __stubStorage;
globalThis.sessionStorage = __stubStorage;
var document = {
  readyState: 'complete', cookie: '', referrer: '', title: '',
  createElement: function(tag) { return { tag: tag, style: {}, href: '', rel: '',
    type: '', charset: '', async: true, readyState: 'complete',
    sheet: { cssRules: [], insertRule: function(){}, addRule: function(){} },
    appendChild: function(){}, removeChild: function(){}, insertBefore: function(){},
    setAttribute: function(){}, getAttribute: function(){ return null; },
    addEventListener: function(){}, attachEvent: function(){},
    contentWindow: { postMessage: function(){} },
    parentNode: { removeChild: function(){} } }; },
  createTextNode: function() { return {}; },
  getElementsByTagName: function() { return [document.head]; },
  documentElement: { style: {}, readyState: 'complete',
    addEventListener: function(){}, attachEvent: function(){},
    getAttribute: function(){ return null; }, setAttribute: function(){} },
  head: { appendChild: function(){}, removeChild: function(){}, insertBefore: function(){} },
  body: { appendChild: function(){}, removeChild: function(){}, insertBefore: function(){} },
  addEventListener: function(){}, attachEvent: function(){},
  removeEventListener: function(){}, detachEvent: function(){},
  getElementById: function(){ return null; },
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; },
  createEvent: function() { return { initEvent: function(){} }; },
  createObjectURL: function(){ return ''; }
};
globalThis.document = document;
window.addEventListener = function(){}; window.attachEvent = function(){};
window.removeEventListener = function(){}; window.detachEvent = function(){};
window.requestAnimationFrame = function(cb){ return 0; };
window.getComputedStyle = function(){ return { getPropertyValue: function(){ return ''; } }; };
""" % {"ua": _UA}

# Node 兜底脚本：注入浏览器环境后加载 webmssdk.js（.cjs 避免被根 package.json 当作 ESM）
_NODE_RUNNER = os.path.join(_ROOT, "js", "_run_sign.cjs")


def _ensure_node_runner():
    if os.path.exists(_NODE_RUNNER):
        return
    script = (
        "const fs = require('fs');\n"
        + _ENV_JS % {"ua": _UA}  # 先格式化占位符再拼接
        + "\neval(fs.readFileSync(process.argv[2], 'utf8'));\n"
        "const r = module.exports.frontierSign("
        "{'X-MS-STUB': process.argv[3]});\n"
        "process.stdout.write(r['X-Bogus']);\n"
        "process.exit(0);\n"
    )
    with open(_NODE_RUNNER, "w", encoding="utf-8") as f:
        f.write(script)


def _sign_with_node(stub: str) -> str:
    if not shutil.which("node"):
        raise RuntimeError("未找到 Node.js，无法计算签名（签名依赖 webmssdk.js 的 V8 运行时）")
    _ensure_node_runner()
    out = subprocess.run(
        ["node", _NODE_RUNNER, WEBMSSDK_PATH, stub],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"Node 签名失败: {out.stderr.strip()[:800]}")
    return out.stdout.strip()


def get_signature(room_id: str, unique_id: str) -> str:
    """生成直播间弹幕 WebSocket URL 的 signature（X-Bogus）。"""
    stub = hashlib.md5(
        _PARAM_TMPL.format(room_id=room_id, unique_id=unique_id).encode()
    ).hexdigest()
    return _sign_with_node(stub)


if __name__ == "__main__":
    print(get_signature("7362491920259713818", "test_unique_id"))
