<div align="center">

# 🎬 MediaHub

**个人影音追更中心 · MP 订阅下载 + Emby 观影记录 + 小爱语音控制 + 本地有声书推流**

[![CI](https://github.com/nshzswz-c/mediahub/actions/workflows/ci.yml/badge.svg)](https://github.com/nshzswz-c/mediahub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED.svg)](https://docs.docker.com/compose/)

一个自托管 (Self-hosted) 的媒体浏览与追更网站。在网页上点 **想看** 自动通知 [MoviePilot](https://github.com/jxxghp/MoviePilot)（下称 MP）订阅下载；在 Emby 看完后回来点 **看完** 并评分；
对小爱音箱说一句 **「帮我订阅xxx」** 或 **「播放xxx有声书」**，全程免动手。

</div>

---

## ✨ 功能一览

### 影音追更
- 🔍 媒体搜索与热门浏览（数据来自 MP 的 TMDB / 豆瓣源）
- ➕ **想看**：本地记录 + 调用 MP API 添加订阅，自动下载整理入库
- ✔ **看完**：看完后标记完成，支持 1~10 分评分 + 评论
- ⬇ 一键从 Emby 拉取"已看"记录导入本地，支持定时自动同步
- 📡 查看 MP 当前订阅状态
- 🌐 PWA：手机添加到主屏幕，像原生 App 一样用

### 🎙 语音控制（配合 [migpt-next](https://github.com/idootop/migpt-next)）
| 口令 | 效果 |
|---|---|
| 「帮我订阅xxx」 | 搜索 TMDB/豆瓣 → 建立订阅 → 自动下载 |
| 「取消订阅xxx」 | 语音取消订阅 |
| 「播放xxx有声书」 | 本地书库选书 → 推流到小爱音箱 |
| （自动） | 下载开始 / 完成时 TTS 播报到音箱 |

- 语音入口走 `/api/hook/mpdl?k=<hook_key>`，**无需暴露站点账号密码**
- 小米云 TTS 支持 passToken 免密登录

### 🎧 有声书
- 自动扫描 NAS 上的有声书目录，支持 `<书>/<音频>` 与 `<书>/<卷>/<音频>` 两种结构
- 按文件名智能解析书名 / 作者 / 演播者，自动清洗水印与序号，章节自然排序（第 9 集在前、第 10 集在后）
- 网页浏览书库、点章节直接推送到小爱音箱播放
- 音频走本地直链（支持 Range 断点拖动），播放进度自动保存，「继续听」无缝续播
- 零第三方依赖，也可选配 Leelaa Reader 作为书库后端

## 📋 目录

- [功能一览](#-功能一览)
- [快速开始](#-快速开始)
  - [本机运行](#本机运行)
  - [NAS 部署（推荐）](#nas-部署docker推荐-docker-compose)
  - [首次配置](#首次配置)
- [目录结构](#-目录结构)
- [安全说明](#-安全说明)
- [常见说明](#-常见说明)
- [致谢](#-致谢)

## 🚀 快速开始

### 本机运行

```bash
git clone https://github.com/nshzswz-c/mediahub.git
cd mediahub
pip install -r server/requirements.txt
python server/main.py
# 打开 http://localhost:8899
```

Windows 可直接双击 `start.bat`。

### NAS 部署（Docker，推荐 docker compose）

```bash
cd /volume1/docker/mediahub
docker compose up -d --build
```

- **群晖**：套件中心装 Container Manager → 项目 → 新增 → 选择上传的文件夹 → 构建
- **威联通**：Container Station → 应用程序 → 创建应用程序，粘贴 docker-compose.yml
- **飞牛 fnOS / UNRAID / 其他**：Docker 界面"项目/Compose"导入，或 SSH 执行上面的命令

访问 `http://NAS_IP:8899`。`data/` 目录就是配置 + 数据库，更新代码时保留它即可。

> 默认 compose 使用 `network_mode: host` 直连本机服务并让音箱可访问音频流；
> 若你的环境不支持 host 网络，可改为端口映射，并额外挂载有声书目录：
> ```yaml
>     ports:
>       - "8899:8899"
>     volumes:
>       - ./server/data:/app/server/data
>       - /volume1/Download/video/有声书:/audiobooks:ro
> ```

### 首次配置

打开网站 → 右上角 ⚙️ 设置：

1. **MP**：填 MP 地址（如 `http://192.168.1.10:3000`）、用户名、密码
   （或直接填个人中心里的 API Token）。API 版本建议保持"自动探测"：
   - **V3**：实际使用 `/api/v1` 路径，响应为 `{success, message, data}` 包装，
     订阅使用 `media_source + media_id` 身份模型，且**支持音乐订阅下载**
   - V2：`/api/v2`；V1：`/api/v1`（老版）
   探测成功后版本会自动锁定并显示在设置页。
2. **Emby**：填 Emby 地址、API Key（Emby 控制台 → 高级 → API 密钥）。
   用户 ID 可留空，会自动获取（默认取管理员账号）；多用户时也可手动指定。
3. **有声书**：填 NAS 上有声书目录的容器内路径（需在 compose 中挂载进容器）。
4. **小爱播报（可选）**：填小米账号，或按 [migpt-next](https://github.com/idootop/migpt-next)
   的方式把 passToken 放到 `server/data/mi.token` 实现免密登录；填音箱的 did。

> 也可以复制 `server/data/config.example.json` 为 `config.json` 后手动填写，
> 首次启动会自动生成默认配置。

## 📁 目录结构

```
mediahub/
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/ci.yml   # CI：Python 3.11~3.13 + Docker 构建冒烟测试
├── start.bat
├── server/
│   ├── main.py           # FastAPI 后端（MP/Emby/语音/音箱对接 + SQLite）
│   ├── books_local.py    # 本地有声书扫描 / 进度 / 直链
│   ├── requirements.txt
│   └── data/             # 运行时生成：config.json + mediahub.db（已 gitignore）
└── web/
    ├── index.html
    ├── style.css
    ├── app.js
    └── sw.js / manifest  # PWA
```

## 🔒 安全说明

- `server/data/` 已在 `.gitignore` 中，**其中的 config.json 含密码与 token，切勿提交或分享**
- 站点登录密码只存 PBKDF2 哈希；`auth_secret` 用于签发 Cookie
- 语音 hook 通过 `hook_key` 鉴权，泄露后可在设置页重置

## 📝 常见说明

- **电影**点"想看"直接触发 MP 订阅下载；**电视剧/短剧/纪录片**默认第 1 季订阅，可在 MP 中调整。
- **音乐**：MP V3 起支持音乐搜索、订阅与下载整理，点"想看"即可发起（V2/V1 不支持）。
- **图书**：MP 不负责图书下载，点"想看"仅记录到本地清单。
  如需自动化，可后续接入 Calibre-Web 等工具。
- MP 订阅状态、下载进度以 MP 侧为准，本站提供入口查看。

## 🙏 致谢

- [MoviePilot](https://github.com/jxxghp/MoviePilot) — 自动化媒体库管理核心
- [migpt-next](https://github.com/idootop/migpt-next) — 小爱音箱接入大模型 / 自定义应答
- [Emby](https://emby.media/) — 家庭影音服务器
- [Leelaa Reader](https://rdoc.leelaa.cn/) — 有声书阅读器（可选书库后端）
- [FastAPI](https://github.com/tiangolo/fastapi) — 后端框架

## ⚠️ 免责声明

本项目为个人自用工具，仅供学习交流，请勿用于商业用途。
使用本项目产生的任何问题由使用者自行承担。

<div align="center">

**如果对你有帮助，欢迎点个 ⭐ Star**

</div>
