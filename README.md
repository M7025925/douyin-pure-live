# douyin-pure-live

抖音直播间视频及弹幕实时采集工具（Python 实现）。支持 GUI 图形界面与 CLI 终端两种运行方式，可实时采集弹幕、礼物、点赞、进入、关注、场观人数等信息。

<img width="1446" height="909" alt="2345截图20260831170332" src="https://github.com/user-attachments/assets/d39ecaa9-7a3f-4e32-b374-aa0fe9f5ffd6" />


## 功能特性

| 功能 | 说明 |
|---|---|
| 弹幕实时采集 | WebSocket 直连抖音弹幕服务器，protobuf 解析（弹幕/礼物/点赞/进入/关注/场观人数） |
| 直播流播放 | GUI 内嵌 HLS 播放（QMediaPlayer）；CLI 支持调用系统播放器（mpv / ffplay / VLC） |
| 主播收益统计 | 实时累计礼物音浪（diamondCount × repeatCount） |
| 多直播间并行 | CLI 空格分隔多个 URL；GUI"新窗口"多开，共享一个签名会话 |
| 心跳保活 | 10 秒一次 hb 帧，长时采集不断线 |
| 断线自动重连 | 异常自动重试，无需人工干预 |
| 消息录制 | 弹幕/礼物保存到时间戳命名的 txt 文档 |
| Cookie 记忆 | 含登录态（sessionid）的 cookie 保存后自动续用；失效自动回退默认游客会话 |
| 历史地址记忆 | 自动记住上次采集的直播间地址 |

> 说明：游客会话（仅 ttwid）收不到礼物消息，需登录态 cookie（程序会检测并提醒，支持浏览器扫码登录或手动粘贴 cookie）。

## 环境要求

| 依赖 | 必需性 | 用途 |
|---|---|---|
| Python 3.10+ | 必需 | 运行环境 |
| Microsoft Edge | 必需 | 真实浏览器环境签名（headless CDP）+ 扫码登录（Windows 10/11 自带） |
| ffmpeg（含 ffplay） | 可选 | CLI 直播流播放（mpv / VLC 亦可） |
| Node.js | 可选 | 仅 Node 兜底签名方案需要（主方案为浏览器签名） |

> 签名必须在真实浏览器环境生成：webmssdk.js 的 `frontierSign` 依赖浏览器指纹，Node 环境生成的签名会被服务端以 `DEVICE_BLOCKED` 拒绝（已实验验证）。因此 Edge 为必需依赖。

## 安装

```bash
cd python
pip install -r requirements.txt
或者直接双击“安装依赖.bat”快速安装
```

## 快速上手

### GUI 模式（推荐）

双击 `运行程序.bat`（或 `python main.py`）：

1. 顶部输入直播间地址（如 `https://live.douyin.com/xxxxxx`），点击**开始采集**
2. 左侧显示主播信息与内嵌直播画面；右侧弹幕列表（类型过滤 / 录制配置）
3. 状态栏实时显示在线观众与主播收益
4. 点击**新窗口**可多开其他直播间并行采集

### CLI 模式

```bash
python main.py --cli
```

按提示依次输入：

```
请输入直播间URL(多个用空格分隔, 回车使用上次: ...):
请粘贴抖音cookie(可选, 直接回车使用默认/已保存):
弹幕将保存到: 260831_112446.txt
是否播放直播画面? (Y/n):
```

输出示例：

```
[danmaku] 已连接 room_id=768002****606553907
[弹幕] 风***: 送了
[礼物] 晞*** 送出 玫瑰 x1  [本场收益 10 音浪]
[点赞] 随*** 赞了 8 次 (本场 5597)
[场观] 675
```

- 多个 URL 空格分隔即可并行采集（每房间独立 txt：`时间戳_房间号.txt`）
- txt 仅记录弹幕与礼物消息，其他类型仅在终端/界面展示

## Cookie 说明（重要）

- **游客会话**（默认，仅 ttwid）：可采集弹幕/点赞/进入/在线，**收不到礼物消息**
- **登录态 cookie**（含 sessionid）：礼物消息与收益统计可用
- **推荐使用小号登录，避免被风控，带来损失**

获取方式（任选其一）：

1. 程序提醒时选择**打开浏览器登录**，用抖音 App 扫码（登录态持久保存在 `local/.edge_profile`，之后无需重复登录）
2. 在自己浏览器中登录抖音后，按 F12 → 网络/应用 → 复制请求 Cookie，粘贴给程序（含 `sessionid` 的会被记住，下次自动续用）

已保存的 cookie 失效时，程序自动回退默认游客会话（不保存失效 cookie），并给出提示。

## 目录结构

```
python/
├── main.py              # 程序入口（GUI 默认 / --cli 终端模式）+ GUI 界面
├── requirements.txt     # Python 依赖
├── 启动弹幕监测.bat      # Windows 双击启动脚本
├── core/                # 核心模块
│   ├── douyin_api.py    #   直播间信息抓取（room_id/主播/FLV/HLS）
│   ├── browser_sign.py  #   Edge headless 签名 + 设备会话（ttwid/unique_id）
│   ├── signature.py     #   Node 兜底签名（X-Bogus）
│   ├── danmaku.py       #   弹幕 WebSocket 连接 + protobuf 解析 + 心跳
│   ├── dy_pb2.py        #   protobuf 消息定义（由 src/proto/dy.proto 生成）
│   └── player.py        #   系统播放器调用（CLI 直播流播放）
├── js/                  # 签名 JS 资源（webmssdk.js / _run_sign.cjs）
├── local/               # 本地状态（自动生成）
│   ├── .last_url        #   上次直播间地址
│   ├── .saved_cookie    #   已保存的登录态 cookie
│   └── .edge_profile/   #   Edge 浏览器会话（含登录态）
└── src/
    └── 222.ico          # 程序图标
```

## 常见问题

**Q: 连接提示 DEVICE_BLOCKED？**
签名环境被风控。本项目已通过真实浏览器签名解决；若仍出现，删除 `local/.edge_profile` 后重试。

**Q: 收不到礼物消息？**
游客会话无礼物推送，按上方 Cookie 说明提供登录态。

**Q: 直播间已连接但没有弹幕？**
部分直播间（如新闻媒体类）未开启评论区，属正常现象，换其他直播间即可。

**Q: local/.edge_profile 目录很大？**
该目录是 Edge 浏览器会话（保存 ttwid/登录态，属于必需数据）。程序已在启动时自动清理其中的纯缓存数据（HTTP 缓存、JS 字节码、AI 组件模型、崩溃转储等），并用启动参数抑制缓存写入，体积可稳定在 ~30 MB。若需彻底重置，删除整个目录即可（登录态会丢失，需重新登录）。

**Q:为什么礼物价值不准确？**
当前版本仅采集程序运行后礼物消息，如果在运行程序前主播已经收到了大量的礼物，程序将不计入，另外，网络传输过程中存在波动，可能会产生丢包，因此，礼物统计属于近似值，不完全准确。

**Q: GUI 播放黑屏 / 无画面？**
内嵌播放依赖系统解码器（Windows 自带 H.264 解码）；可点击"用系统播放器打开(FLV)"兜底。

**Q: 如何修改 protobuf 定义？**
编辑 `../src/dy.proto` 后重新生成：

```bash
python -m grpc_tools.protoc --python_out=python --proto_path=src src/dy.proto
```

## 参考项目

- https://github.com/Sjj1024/LiveBox
- https://github.com/saermart/DouyinLiveWebFetcher
- https://github.com/Remember-the-past/douyin_proto
- webmssdk.js —— 抖音官方前端签名脚本（本项目仅作研究学习用途）

## 免责声明

本项目仅供学习与研究，请勿用于任何商业或非法用途。采集行为请遵守目标网站的服务条款与相关法律法规，使用者需自行承担相应责任。
