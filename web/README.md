# taleclaw 部署教程

这是当前项目的本地 Web 控制台。前端是原生 HTML/CSS/JS，后端用 Python 标准库 HTTP server，聊天逻辑复用 `core.bootstrap.build_runtime()`。

推荐部署方式：Docker Compose + Nginx + HTTPS。

新的首尔 Ubuntu 服务器从零部署见：[首尔服务器完整部署手册](../docs/deployment/SEOUL_SERVER_DEPLOYMENT.md)。

Telegram 私聊接入见：[Telegram Gateway 部署说明](../docs/gateways/TELEGRAM_GATEWAY.md)。

## 0. 功能和快捷键

启动后打开浏览器即可使用：

- Web 聊天会话
- 注册登录页：浏览器使用 Cookie 登录态，会话、长期记忆、文件区和分析记录按账号隔离
- 助手消息支持安全 Markdown 展示，标题、列表和代码块可直接阅读
- 工具请求和工具结果默认折叠，按需展开查看完整内容
- 会话历史列表
- `memory/*.md` 记忆文件浏览
- `storage/` 私有文件区，支持上传、弹窗预览文本、下载、重命名、删除
- Bot 模式可以读取 `storage/`，并将报告、笔记和导出文件新建到 `storage/generated/`
- Bot 模式拥有按会话隔离的 `.task_sandbox/` 临时工作区，可以将选定的最终文件发布到 `storage/generated/`
- 会话侧栏支持删除 Web 会话，删除当前会话后会自动切换或新建会话
- 文本分析页，AI 回复会和原文一起追加保存到 `storage/records/analysis.txt`
- `/hybrid`、`/chat`、`/coding` 模式切换

快捷键：

- `Enter`：发送消息
- `Shift+Enter`：输入换行
- `Ctrl/Cmd+K`：新建会话
- `Ctrl/Cmd+1`：切到 Hybrid
- `Ctrl/Cmd+2`：切到 Chat
- `Ctrl/Cmd+3`：切到 Coding

## 1. 本地先跑通

在项目根目录创建 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
USE_LOCAL_PROXY=0
WEB_USERNAME=agent
WEB_PASSWORD=换成强密码
WEB_ALLOW_REGISTRATION=1
WEB_ALLOW_ANONYMOUS=0
WEB_COOKIE_SECURE=0
WEB_MAX_BODY_BYTES=52428800
```

单账号配置会被视为管理员账号。多人使用时，改成：

```bash
WEB_USERS_JSON={"admin":{"password":"管理员强密码","role":"admin"},"guest":{"password":"普通用户强密码","role":"user"}}
```

`admin` 可以使用 Coding 模式和服务器级定时任务。`user` 账号只使用受限的
Bot/Hybrid 能力，不会获得 `bash`、项目文件写入或服务器级定时任务权限。

浏览器打开站点后会进入 `/login`。`.env` 中的账号可直接登录；开启
`WEB_ALLOW_REGISTRATION=1` 后，其他用户也可以在页面注册普通账号。

本地普通 Python 启动：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python web/server.py --host 127.0.0.1 --port 8000
```

访问：

```text
http://127.0.0.1:8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

runtime 检查：

```bash
curl http://127.0.0.1:8000/api/runtime-health
```

## 2. Docker 部署，本项目推荐

可以用 Docker。这个仓库已经包含：

```text
Dockerfile
docker-compose.yml
.dockerignore
requirements.txt
.env.example
```

### 2.1 云服务器准备

建议使用 Ubuntu 22.04/24.04，至少：

```text
1 核 CPU
1 GB 内存
10 GB 磁盘
```

如果后续让 agent 跑较重的 coding task，建议 2 GB 以上内存。

服务器安全组或防火墙开放：

```text
22/tcp   SSH
80/tcp   HTTP
443/tcp  HTTPS
```

不要开放 `8000/tcp` 到公网。`docker-compose.yml` 已经把服务绑定到 `127.0.0.1:8000`，只允许服务器本机访问，再交给 Nginx 代理。

### 2.2 安装 Docker

按 Docker 官方文档安装 Docker Engine。Ubuntu 可以从官方 apt 仓库安装：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

验证：

```bash
docker --version
docker compose version
```

### 2.3 上传代码

方式 A：用 Git：

```bash
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
git clone <你的仓库地址> /opt/agent-console
cd /opt/agent-console
```

方式 B：从本机同步：

```bash
rsync -av --exclude .git --exclude .venv ./ user@server:/opt/agent-console/
```

### 2.4 配置 `.env`

在服务器项目根目录：

```bash
cd /opt/agent-console
cp .env.example .env
nano .env
```

示例：

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
USE_LOCAL_PROXY=0
WEB_USERNAME=agent
WEB_PASSWORD=一段很长的随机密码
WEB_ALLOW_REGISTRATION=1
WEB_ALLOW_ANONYMOUS=0
WEB_SESSION_TTL_HOURS=168
WEB_COOKIE_SECURE=0
WEB_MAX_BODY_BYTES=52428800
PYTHON_IMAGE=python:3.12-slim
PIP_INDEX_URL=https://pypi.org/simple
PIP_EXTRA_INDEX_URL=
PIP_TRUSTED_HOST=
PIP_DEFAULT_TIMEOUT=180
PIP_RETRIES=10
TASK_SANDBOX_TTL_HOURS=168
```

说明：

- 默认使用 DeepSeek；`DEEPSEEK_API_KEY` 必填。
- 要使用小米 MiMo，改成 `LLM_PROVIDER=mimo`、`MIMO_API_KEY=...`、
  `MIMO_BASE_URL=https://api.xiaomimimo.com/v1`、`MIMO_MODEL=mimo-v2.5-pro`。
- 要同时接入多个模型，可以保留 `LLM_PROVIDER=deepseek` 作为默认模型，再添加
  `MIMO_API_KEY` 并配置 `LLM_ROUTE_*`。详细配置见
  [MODEL_PROVIDER_POOL_ROUTING.md](../docs/runtime/MODEL_PROVIDER_POOL_ROUTING.md)。
- `USE_LOCAL_PROXY=0` 适合云服务器；本地开发如果要走代理，可以设为 `1`。
- 浏览器使用 `/login` 页面登录，不再弹出浏览器原生认证框。
- 多用户部署用 `WEB_USERS_JSON` 替代 `WEB_USERNAME` 和 `WEB_PASSWORD`。不要同时保留两套配置。
- `WEB_ALLOW_REGISTRATION=1` 开放页面注册。公开部署不需要注册时设为 `0`。
- `WEB_ALLOW_ANONYMOUS=0` 禁止绕过登录。云服务器保持为 `0`。
- `WEB_SESSION_TTL_HOURS` 控制登录态时长，默认 `168` 小时。
- Nginx 配置 HTTPS 后，把 `WEB_COOKIE_SECURE=1`，让浏览器只通过 HTTPS 发送 Cookie。
- `WEB_MAX_BODY_BYTES` 控制单次上传大小，默认约 50 MB。
- `PYTHON_IMAGE` 是 Docker 基础镜像。Docker Hub 访问超时时，可以临时换成你服务器可访问的 Python 3.12 slim 镜像源。
- `PIP_INDEX_URL` 是 Python 依赖下载源。`files.pythonhosted.org` 超时时，可以改成你服务器可访问的 PyPI mirror。

### 2.5 启动容器

启动前确认当前目录有 Compose 配置：

```bash
pwd
ls -la docker-compose.yml Dockerfile .env
```

```bash
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
docker compose logs -f
```

本机验证：

```bash
curl http://127.0.0.1:8000/api/health
```

如果设置了 `WEB_PASSWORD`，用：

```bash
curl -u agent:你的密码 http://127.0.0.1:8000/api/health
```

再检查 runtime 是否能启动：

```bash
curl -u agent:你的密码 http://127.0.0.1:8000/api/runtime-health
```

### 2.6 数据持久化

`docker-compose.yml` 已挂载这些目录：

```text
./storage         文件区和分析记录
./memory          长期记忆 Markdown
./.task_sessions 任务会话
./.task_sandbox  会话隔离的临时文件，默认保留 168 小时
./.tasks         task 数据
./.team          team inbox
./.transcripts   transcript
./.users         多用户私有数据，每个账号拥有独立 storage/ 和 memory/
```

注册账号和浏览器登录会话保存在 PostgreSQL。密码以 PBKDF2 哈希保存，不会明文写入数据库。

多用户私有目录结构：

```text
.users/<user-id>/storage
.users/<user-id>/memory
```

升级到多用户版本后，旧的 `storage/` 和 `memory/` 不会自动暴露给任意 Web
账号。需要迁移旧数据时，先停止容器，再手工复制给指定管理员：

```bash
docker compose down
mkdir -p .users/admin/storage .users/admin/memory
cp -a storage/. .users/admin/storage/
cp -a memory/. .users/admin/memory/
docker compose up -d --build
```

升级镜像或重启容器不会丢这些数据。

常用维护命令：

```bash
docker compose restart
docker compose down
docker compose up -d --build
docker compose logs -f --tail=200
```

## 3. 配置 Nginx 反向代理

安装 Nginx：

```bash
sudo apt-get update
sudo apt-get install -y nginx
```

创建配置：

```bash
sudo nano /etc/nginx/sites-available/agent-console
```

写入，把 `your-domain.example` 换成你的域名：

```nginx
server {
    listen 80;
    server_name your-domain.example;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用：

```bash
sudo ln -s /etc/nginx/sites-available/agent-console /etc/nginx/sites-enabled/agent-console
sudo nginx -t
sudo systemctl reload nginx
```

现在可以先用 HTTP 访问：

```text
http://your-domain.example
```

## 4. 配置 HTTPS

域名 A 记录先解析到服务器公网 IP。然后安装 Certbot：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
```

签发并让 Certbot 自动修改 Nginx 配置：

```bash
sudo certbot --nginx -d your-domain.example
```

测试自动续期：

```bash
sudo certbot renew --dry-run
```

之后访问：

```text
https://your-domain.example
```

## 5. 更新部署

Git 方式：

```bash
cd /opt/agent-console
git pull
docker compose up -d --build
```

rsync 方式：

```bash
rsync -av --exclude .git --exclude .venv ./ user@server:/opt/agent-console/
ssh user@server
cd /opt/agent-console
docker compose up -d --build
```

## 6. 备份

最少备份：

```bash
tar czf agent-console-backup-$(date +%F).tar.gz storage memory .sessions .task_sessions .tasks .team .transcripts .env
```

恢复时把这些目录放回项目根目录，再：

```bash
docker compose up -d --build
```

## 7. 不用 Docker 的 systemd 部署

如果你不想用 Docker，也可以直接用 Python + systemd。

```bash
git clone <你的仓库地址> /opt/agent-console
cd /opt/agent-console
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

创建 `/etc/systemd/system/agent-console.service`：

```ini
[Unit]
Description=taleclaw Web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/agent-console
EnvironmentFile=/opt/agent-console/.env
ExecStart=/opt/agent-console/.venv/bin/python web/server.py --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agent-console
sudo systemctl status agent-console
```

日志：

```bash
sudo journalctl -u agent-console -f
```

Nginx 和 HTTPS 步骤仍然使用上面的第 3、4 节。

## 8. 排查清单

### 8.1 判断 503 来自哪里

先看响应头：

```bash
curl -i http://127.0.0.1:8000/api/health
curl -i -u agent:你的密码 http://127.0.0.1:8000/api/runtime-health
curl -i https://your-domain.example/api/health
```

如果 `Server` 里是 `AgentWeb/...`，说明请求已经到了 Python 后端。

如果 `Server` 里是 `nginx`，或者浏览器直接显示 Nginx 的 503/502 页面，说明 Nginx 没有成功连到后端容器。

### 8.2 打开网页就是 503/502

这是 Nginx 或容器层问题，按顺序查：

```bash
cd /opt/agent-console
docker compose ps
docker compose logs -f --tail=200
curl -u agent:你的密码 http://127.0.0.1:8000/api/health
sudo nginx -t
sudo journalctl -u nginx -f
```

常见原因：

- 容器没启动，或者启动后退出。
- `.env` 缺失，`docker compose` 没有读到环境变量。
- Nginx `proxy_pass` 端口不是 `127.0.0.1:8000`。
- 云服务器安全组没有开放 `80`/`443`。

### 8.3 网页能打开，发消息 503

这是 Python 后端能访问，但 agent runtime 或模型请求失败。先查：

```bash
curl -u agent:你的密码 http://127.0.0.1:8000/api/runtime-health
docker compose logs -f --tail=200
docker compose exec agent-console env | grep -E 'LLM|MIMO|DEEPSEEK|USE_LOCAL_PROXY|WEB_'
```

再测一个不会调用模型的命令：

```bash
curl -u agent:你的密码 \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"debug","message":"/status"}' \
  http://127.0.0.1:8000/api/chat
```

如果 `/status` 正常，但普通消息 503，基本就是模型 API 调用失败：

- `DEEPSEEK_API_KEY` 错了或没传进容器。
- `DEEPSEEK_BASE_URL` 写错。
- MiMo 模式下 `MIMO_API_KEY`、`MIMO_BASE_URL` 或 `MIMO_MODEL` 写错。
- 云服务器访问不了当前模型服务地址，例如 `https://api.deepseek.com` 或 `https://api.xiaomimimo.com`。
- `USE_LOCAL_PROXY` 仍是 `1`，导致容器去连不存在的 `127.0.0.1:7897`。

云服务器建议：

```bash
USE_LOCAL_PROXY=0
```

### 8.4 没登录或登录失败

浏览器访问站点会自动跳到：

```text
/login
```

命令行排查仍可用兼容的 Basic Auth：

```bash
curl -u agent:你的密码 http://127.0.0.1:8000/api/health
```

如果页面登录失败，检查 `.env`：

```bash
WEB_USERNAME=agent
WEB_PASSWORD=你的密码
```

改完 `.env` 后重启：

```bash
docker compose restart
```

### 8.5 原始排查命令

容器没起来：

```bash
docker compose ps
docker compose logs -f --tail=200
```

报 `no configuration file provided: not found`：

```bash
pwd
ls -la
ls -la docker-compose.yml Dockerfile
```

这个错误表示当前目录没有 `docker-compose.yml`。解决方式：

- 如果服务器代码来自 Git，先把本地新增的 `Dockerfile`、`docker-compose.yml`、`.dockerignore` 提交并推送，然后服务器执行 `git pull`。
- 如果手动上传代码，把 `Dockerfile`、`docker-compose.yml`、`.dockerignore`、`requirements.txt`、`.env.example` 和 `web/` 目录一起上传到服务器项目根目录。
- 临时指定配置文件路径：`docker compose -f /opt/agent-console/docker-compose.yml up -d --build`。

拉取 `python:3.12-slim` 超时：

```text
failed to resolve source metadata for docker.io/library/python:3.12-slim
i/o timeout
```

这是服务器访问 Docker Hub 超时，不是应用代码问题。先单独测试：

```bash
sudo docker pull python:3.12-slim
```

如果也超时，选一种处理方式：

方式 A：配置 Docker 镜像加速器。使用你的云厂商提供的 Docker Hub mirror，编辑 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": [
    "https://你的镜像加速地址"
  ]
}
```

然后重启 Docker：

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo docker pull python:3.12-slim
docker compose up -d --build
```

方式 B：服务器如果需要代理才能访问 Docker Hub，需要给 Docker daemon 配代理，而不是只给 shell 配 `HTTP_PROXY`。创建目录：

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo nano /etc/systemd/system/docker.service.d/proxy.conf
```

写入：

```ini
[Service]
Environment="HTTP_PROXY=http://代理地址:端口"
Environment="HTTPS_PROXY=http://代理地址:端口"
Environment="NO_PROXY=localhost,127.0.0.1"
```

重启：

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo docker pull python:3.12-slim
docker compose up -d --build
```

方式 C：临时换基础镜像。确认你的替代镜像是 Python 3.12 slim 兼容镜像后，在 `.env` 里改：

```bash
PYTHON_IMAGE=你的镜像源/library/python:3.12-slim
```

再构建：

```bash
docker compose up -d --build
```

安装 Python 依赖超时：

```text
RUN pip install -r requirements.txt
HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out.
```

这是容器构建时访问 PyPI 或 `files.pythonhosted.org` 超时。如果日志里还有：

```text
pip is still looking at multiple versions of httpx
```

说明依赖没有锁死，pip 会反复下载不同版本 metadata。当前仓库的 `requirements.txt` 已经锁定关键版本来减少回溯；请先确认服务器也同步了新版 `requirements.txt`、`Dockerfile` 和 `docker-compose.yml`：

```bash
grep -n 'httpx==0.28.1' requirements.txt
grep -n 'prefer-binary' Dockerfile
grep -n 'PIP_INDEX_URL' docker-compose.yml
```

再在服务器上测网络：

```bash
curl -I https://pypi.org/simple/openai/
curl -I https://files.pythonhosted.org/
```

如果访问慢或超时，改 `.env` 里的 pip 配置。先打开：

```bash
nano .env
```

可选一：使用默认 PyPI，但增加超时和重试：

```bash
PIP_INDEX_URL=https://pypi.org/simple
PIP_EXTRA_INDEX_URL=
PIP_TRUSTED_HOST=
PIP_DEFAULT_TIMEOUT=300
PIP_RETRIES=20
```

可选二：换成你服务器可访问的 PyPI 镜像源：

```bash
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
PIP_EXTRA_INDEX_URL=
PIP_TRUSTED_HOST=mirrors.cloud.tencent.com
PIP_DEFAULT_TIMEOUT=300
PIP_RETRIES=20
```

其他常见可选源：

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
```

```bash
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
PIP_TRUSTED_HOST=mirrors.aliyun.com
```

然后重新构建，不用缓存：

```bash
docker compose build --no-cache
docker compose up -d
```

如果你临时只想在命令行指定，也可以：

```bash
PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
PIP_TRUSTED_HOST=mirrors.cloud.tencent.com \
PIP_DEFAULT_TIMEOUT=300 \
PIP_RETRIES=20 \
docker compose up -d --build
```

API key 没读到：

```bash
docker compose exec agent-console env | grep DEEPSEEK
```

本机端口不通：

```bash
curl -u agent:你的密码 http://127.0.0.1:8000/api/health
```

Nginx 配置错误：

```bash
sudo nginx -t
sudo journalctl -u nginx -f
```

公网打不开：

- 云服务器安全组是否开放 `80`、`443`
- 域名 A 记录是否指向服务器 IP
- Nginx `server_name` 是否填对
- Docker 服务是否只在本机 `127.0.0.1:8000` 正常响应

模型请求失败：

- `.env` 里 `DEEPSEEK_API_KEY` 是否正确
- 云服务器是否能访问 `https://api.deepseek.com`
- 云服务器上 `USE_LOCAL_PROXY` 是否为 `0`

## 9. 参考文档

- Docker Engine on Ubuntu: https://docs.docker.com/engine/install/ubuntu/
- Docker Compose up: https://docs.docker.com/reference/cli/docker/compose/up/
- Dockerfile reference: https://docs.docker.com/reference/builder
- Nginx reverse proxy: https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/
- Certbot Nginx instructions: https://certbot.eff.org/instructions
- Python `venv`: https://docs.python.org/3/library/venv.html
- systemd service: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
