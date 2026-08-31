"""直播流播放模块。

对应 Tauri 版的 DPlayer 播放功能（App.vue loadVideo）。
CLI 场景下调用系统已有播放器子进程播放 FLV 流：
优先 mpv（低延迟最佳），其次 ffplay（ffmpeg 自带），最后 VLC。
"""
import shutil
import subprocess

_TITLE = "LiveBox 直播"

# 播放器候选: (可执行文件名, 低延迟参数)
_CANDIDATES = [
    ("mpv", ["--profile=low-latency", f"--title={_TITLE}"]),
    (
        "ffplay",
        [
            "-window_title", _TITLE,
            "-loglevel", "quiet",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-framedrop",
            "-alwaysontop",
            "-sync", "ext",  # 以外部时钟同步，避免直播延迟累积
        ],
    ),
    ("vlc", ["--no-video-title", "--live-caching=300"]),
]


def find_player() -> list[str]:
    """查找可用的播放器，返回命令前缀（含低延迟参数）；找不到返回空列表。"""
    for name, args in _CANDIDATES:
        path = shutil.which(name)
        if path:
            return [path, *args]
    return []


def open_stream(cmd: list[str], url: str) -> subprocess.Popen:
    """启动播放器子进程播放直播流。"""
    return subprocess.Popen(
        [*cmd, url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
