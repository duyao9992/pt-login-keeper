# PT Login Keeper

PT 站 Cookie 登录保活与失效提醒工具。

它不会保存 PT 账号密码，也不会绕过验证码或二步验证。工作方式是：你手动登录 PT 站后复制 Cookie，工具定时访问站点检测页，确认 Cookie 是否仍有效；失效或接近保号期限时发送通知。

## 功能

- 多 PT 站点管理
- Cookie 持久化保存
- 定时检测登录状态
- 手动立即检测
- 成功/失效关键词自定义
- GET / POST 检测方式
- M-Team API 签名检测
- 上传量、下载量、分享率读取与定期通知
- 默认 25 天自动检测一次
- 默认 30 天提醒手动网页登录一次
- Webhook、企业微信机器人/微信转发、Server 酱、PushPlus 通知
- Docker 部署
- GitHub Actions 自动发布 GHCR 镜像

## 一键部署到 NAS

镜像发布到 GitHub Container Registry 后，在飞牛、极空间、群晖等 NAS 的 SSH 里执行：

```bash
curl -fsSL https://raw.githubusercontent.com/duyao9992/pt-login-keeper/main/deploy.sh | APP_DIR=/vol1/1000/docker/pt-login-keeper IMAGE=ghcr.io/duyao9992/pt-login-keeper:latest sh
```

常用参数：

```bash
APP_DIR=/vol1/1000/docker/pt-login-keeper
IMAGE=ghcr.io/duyao9992/pt-login-keeper:latest
PORT=9199
WEB_USER=vip
WEB_PASSWORD='your_password'
```

不同 NAS 只需要改 `APP_DIR`，例如群晖可用 `/volume1/docker/pt-login-keeper`。

## Docker Compose

```yaml
services:
  pt-login-keeper:
    image: ghcr.io/duyao9992/pt-login-keeper:latest
    container_name: pt-login-keeper
    restart: unless-stopped
    environment:
      CONFIG_DIR: /config
      APP_HOST: 0.0.0.0
      APP_PORT: "9199"
      CHECK_INTERVAL_SECONDS: "300"
      WEB_USER: ""
      WEB_PASSWORD: ""
    ports:
      - "9199:9199"
    volumes:
      - ./config:/config
```

如果不使用 GHCR，也可以本地构建：

```bash
docker compose up -d --build
```

访问：

```text
http://NAS_IP:9199
```

## 使用方法

1. 浏览器正常登录 PT 站。
2. 打开浏览器开发者工具，进入 `Network`，选择一个登录后页面或接口请求。
3. 推荐右键请求，选择 `Copy as cURL`，直接粘贴到“请求头或 cURL”输入框。
4. 工具会自动提取 `Cookie` / `Authorization` / `User-Agent` / `Referer`。
5. 如果只手动复制请求头，也可以直接粘贴 `Request Headers` 内容。
6. 在 PT Login Keeper 添加站点。
7. 填写检测地址，建议填用户中心或控制面板地址。
8. 填写成功关键词，例如：

```text
退出
用户中心
控制面板
上传量
下载量
魔力
积分
```

9. 填写失效关键词，例如：

```text
登录
登入
注册
login
password
```

10. 推荐把“检测间隔小时”设为 `600`，也就是每 25 天自动检测一次。
11. 开启“上传下载数据通知”，数据通知间隔填 `25`。
12. 开启“手动网页登录提醒”，提醒天数填 `30`。
13. 保存后点击“检测”或“立即检测全部”。

说明：容器检测只能确认当前 Cookie / Authorization 还可用，不一定等同于站点规则里的“真实网页登录”。收到 30 天提醒后，建议用浏览器打开站点手动登录/刷新一次，然后回到首页点该站点的“已手动登录”。

## M-Team / 馒头配置

M-Team 新版页面是前端应用，首页 `https://kp.m-team.cc/index` 直接请求只能拿到空壳 HTML，里面没有“退出、魔力值、上傳量”等登录文字。因此不要用首页做检测地址。

推荐配置：

```text
检测地址：https://api.m-team.cc/api/member/profile
检测方法：POST
M-Team API 签名：启用
成功关键词：你的用户名
失效关键词：Full authentication
失效关键词：非法用戶端
失效关键词："code":401
```

在浏览器已经登录 M-Team 后，打开开发者工具 `Network`，找一个 `api.m-team.cc/api/...` 请求，复制 `Request Headers` 或 `Copy as cURL` 粘贴到“请求头或 cURL”。工具会尽量自动提取：

```text
authorization
did
visitorId
version
webVersion
user-agent
referer
accept-language
```

如果检测结果是 `非法用戶端`，通常是缺少 `did` 或 `visitorId`，需要重新从当前浏览器的 API 请求头里复制完整请求头。

## 微信通知

如果已经有企业微信机器人或微信转发服务器，在首页“通知设置”里填写：

```text
企业微信机器人 / 微信转发 Webhook
```

如果 MoviePilot 已经配置好了企业微信通知，可以把 MoviePilot 里同一个企业微信机器人 Webhook 复制到这里共用。保存后点击“发送测试通知”，手机能收到就说明配置正确。

工具会按企业微信机器人格式发送：

```json
{"msgtype":"text","text":{"content":"通知内容"}}
```

如果 MoviePilot 使用的是“企业微信应用”通知，而不是机器人 Webhook，则填写“企业微信应用通知”区域：

```text
企业 ID / CorpID：MoviePilot 的 WECHAT_CORPID
应用 AgentId：MoviePilot 的 WECHAT_APP_ID
应用 Secret：MoviePilot 的 WECHAT_APP_SECRET
接收用户：默认 @all
企业微信代理：MoviePilot 的 WECHAT_PROXY，可选；它是企业微信 API 转发基础地址，不是浏览器 HTTP 代理
```

如果你的转发服务器只接收普通 JSON，则使用“通用 Webhook URL”，工具会发送：

```json
{"title":"标题","text":"正文"}
```

## 安全边界

- 不自动输入账号密码。
- 不破解验证码。
- 不绕过 Cloudflare、二步验证或站点风控。
- Cookie 属于登录凭据，建议仅内网访问，并设置 `WEB_USER` / `WEB_PASSWORD`。

## GHCR 镜像发布

项目已包含 `.github/workflows/docker-publish.yml`。推送到 GitHub 后，会自动构建：

```text
ghcr.io/<你的GitHub用户名>/<仓库名>:latest
```

在其它 NAS 使用该镜像即可。
