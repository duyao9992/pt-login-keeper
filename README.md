# PT Login Keeper

PT 站 Cookie 登录保活与失效提醒工具。

它不会保存 PT 账号密码，也不会绕过验证码或二步验证。工作方式是：你手动登录 PT 站后复制 Cookie，工具定时访问站点检测页，确认 Cookie 是否仍有效；失效或接近保号期限时发送通知。

## 功能

- 多 PT 站点管理
- Cookie 持久化保存
- 定时检测登录状态
- 手动立即检测
- 成功/失效关键词自定义
- 30 天保号倒计时提醒
- Webhook、Server 酱、PushPlus 通知
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
2. 打开浏览器开发者工具，复制该站请求里的 `Cookie` 请求头。
3. 在 PT Login Keeper 添加站点。
4. 填写检测地址，建议填用户中心或控制面板地址。
5. 填写成功关键词，例如：

```text
退出
用户中心
控制面板
上传量
下载量
魔力
积分
```

6. 填写失效关键词，例如：

```text
登录
登入
注册
login
password
```

7. 保存后点击“立即检测全部”。

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
