# Web Login And Registration

## 目标

taleclaw Web 控制台现在拥有独立的注册登录页面：

```text
/login
```

浏览器不再依赖原生 Basic Auth 弹窗。认证由以下部分组成：

```text
web/static/login.html   登录注册页面
web/static/login.js     页面交互
web/static/auth.css     页面样式
web/auth_store.py       PostgreSQL 账号和登录会话存储
web/server.py           认证 API、Cookie 鉴权和页面跳转
```

## 页面行为

未登录访问主页面时，服务端返回跳转：

```text
303 See Other
Location: /login
```

登录页提供：

- 登录
- 注册
- 注册开关控制
- 已登录用户自动返回主页面

主应用侧栏底部新增固定的账号区和“退出登录”按钮，不需要先切换到状态面板。移动端打开
左上角侧栏后也可以直接退出。浏览器登录态过期后，任意 API 请求收到 `401`，前端会返回
登录页。

## API

公开 API：

```text
GET  /api/auth/config
POST /api/auth/login
POST /api/auth/register
POST /api/auth/logout
```

需要登录：

```text
GET /api/auth/me
```

登录成功和注册成功后，后端返回：

```text
Set-Cookie: taleclaw_session=<random-token>; Path=/; HttpOnly; SameSite=Lax
```

启用 `WEB_COOKIE_SECURE=1` 时，还会附加：

```text
Secure
```

## 密码和登录态存储

认证数据保存在 PostgreSQL：

```text
WEB_AUTH_DATABASE_URL -> DATABASE_URL
```

注册密码使用：

```text
PBKDF2-HMAC-SHA256
310000 iterations
random 16-byte salt
```

数据库不会保存明文密码。浏览器 Cookie 中只保存随机 token；数据库保存 token 的
SHA-256 哈希，因此数据库记录也不能直接当作浏览器 Cookie 使用。

## 账号来源

管理员仍然由 `.env` 提供：

```bash
WEB_USERS_JSON={"admin":{"password":"change-admin","role":"admin"}}
```

兼容单管理员写法：

```bash
WEB_USERNAME=agent
WEB_PASSWORD=replace-with-a-strong-password
```

这些账号在页面登录时同步到 PostgreSQL。网页注册只能创建：

```text
role=user
```

网页注册无法创建管理员，避免普通访客获得 Coding、`bash` 或 scheduler 权限。

## 环境变量

```bash
WEB_ALLOW_REGISTRATION=1
WEB_ALLOW_ANONYMOUS=0
WEB_SESSION_TTL_HOURS=168
WEB_COOKIE_SECURE=0
```

含义：

| 变量 | 说明 |
| --- | --- |
| `WEB_ALLOW_REGISTRATION` | 是否展示并开放注册 API，默认关闭 |
| `WEB_ALLOW_ANONYMOUS` | 是否允许无登录访问，默认关闭 |
| `WEB_SESSION_TTL_HOURS` | Cookie 登录态有效期，默认 168 小时 |
| `WEB_COOKIE_SECURE` | 是否只允许 HTTPS 发送 Cookie，配置 HTTPS 后设为 `1` |

## 运维命令

更新代码后重新构建：

```bash
sudo docker compose up -d --build
```

浏览器访问：

```text
http://服务器地址/
```

使用 HTTPS 时推荐：

```bash
WEB_COOKIE_SECURE=1
```

命令行健康检查仍兼容 Basic Auth：

```bash
curl -u admin:管理员密码 http://127.0.0.1:8000/api/health
```

## 验证

新增测试：

```text
tests/test_web_auth.py
```

覆盖：

- 注册密码只保存哈希
- 注册账号固定为普通用户
- 环境变量管理员同步
- 密码错误拒绝
- Cookie 登录态鉴权
- 退出登录注销 Cookie token
- 注册开关关闭时拒绝注册
- 登录 API 返回 `HttpOnly` 和 `SameSite=Lax`

完整验证命令：

```bash
python -m unittest discover -s tests -v
python -m py_compile web/auth_store.py web/server.py
node --check web/static/app.js
node --check web/static/login.js
docker compose config --quiet
git diff --check
```

本次改动完成后，完整单元测试结果为：

```text
Ran 103 tests
OK
```

真实 HTTP 冒烟也已覆盖：

- 未登录访问 `/` 跳转到 `/login`
- 注册普通账号
- Cookie 登录后访问 `/api/auth/me`
- 退出登录后原 Cookie 再次访问返回 `401`
- 管理员继续使用 Basic Auth 访问 `/api/health`

## 当前边界

这是适合自用和小规模部署的账号系统。公网部署时建议使用 HTTPS，并在不需要开放注册时
设置 `WEB_ALLOW_REGISTRATION=0`。当前版本尚未实现登录限流、找回密码、邮箱验证和更严格
的 CSRF 防护；如果要开放给不受信任的用户，应把这些能力列入下一阶段。
