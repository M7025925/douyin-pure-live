"""抖音直播间信息抓取模块。

对应 Tauri 版 src-tauri/src/command/runner.rs 的 get_room_info：
请求直播间页面 HTML，正则提取 room_info、ttwid、unique_id。
"""
import json
import re

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "max-age=0",
    "priority": "u=0, i",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": UA,
}

# 与 runner.rs 相同的正则：HTML 里的转义 JSON
_RE_ROOM_INFO = re.compile(r'roomInfo\\":\{\\"room\\":(.*?),\\"toolbar_data')
_RE_ANCHOR = re.compile(r'anchor\\":(.*?),\\"open_id_str')  # 停播时
_RE_UNIQUE_ID = re.compile(r'user_unique_id\\":\\"(.*?)\\"}')


class LiveInfo:
    def __init__(self, room_info: dict, ttwid: str, unique_id: str, raw_html: str = ""):
        self.room_info = room_info
        self.ttwid = ttwid
        self.unique_id = unique_id
        self.raw_html = raw_html

    @property
    def is_live(self) -> bool:
        # 停播间走 anchor 正则分支，room_info 中没有 status 字段 → 视为停播
        return bool(self.room_info.get("status"))

    @property
    def room_id(self) -> str:
        return str(self.room_info.get("id_str", ""))

    def __repr__(self):
        owner = self.room_info.get("owner", {})
        return (
            f"LiveInfo(room_id={self.room_id}, name={owner.get('nickname')!r}, "
            f"status={self.room_info.get('status')}, ttwid={self.ttwid[:12]}..., "
            f"unique_id={self.unique_id!r})"
        )


def _fetch_ttwid(session: requests.Session) -> str:
    """首次访问无 ttwid 时，请求一次主页触发 Set-Cookie。"""
    session.get("https://live.douyin.com/", timeout=10)
    return session.cookies.get("ttwid", "")


def get_live_info(url: str) -> LiveInfo:
    """抓取直播间信息。url 形如 https://live.douyin.com/<roomId>"""
    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(url, timeout=10)
    ttwid = session.cookies.get("ttwid", "")
    if not ttwid:
        ttwid = _fetch_ttwid(session)
        resp = session.get(url, timeout=10)

    body = resp.text

    unique_id = ""
    m = _RE_UNIQUE_ID.search(body)
    if m:
        unique_id = m.group(1)

    # 停播直播间：仅包含主播基础信息（结构不同）
    if 'status\\"' in body and re.search(r'status\\":4', body):
        m = _RE_ANCHOR.search(body)
        if not m:
            raise RuntimeError("页面中未找到主播信息（可能触发风控验证）")
        room_info = json.loads((m.group(1) + "}").replace('\\"', '"'))
        return LiveInfo(room_info, ttwid, unique_id, body)

    m = _RE_ROOM_INFO.search(body)
    if not m:
        raise RuntimeError("页面中未找到 room_info（可能触发风控验证或地址无效）")
    room_info = json.loads((m.group(1) + "}").replace('\\"', '"'))
    return LiveInfo(room_info, ttwid, unique_id, body)


def get_flv_url(info: LiveInfo) -> str:
    """取默认清晰度的 FLV 播放地址（https）。"""
    stream = info.room_info.get("stream_url", {})
    pull = stream.get("flv_pull_url", {})
    key = stream.get("default_resolution", "")
    url = pull.get(key) or next(iter(pull.values()), "")
    return url.replace("http://", "https://") if url else ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python douyin_api.py <直播间URL>")
        sys.exit(1)
    info = get_live_info(sys.argv[1])
    print(info)
    print("FLV:", get_flv_url(info))
