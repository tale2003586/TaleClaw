from __future__ import annotations

import re


DEFAULT_SECURITY_KEYWORDS = {
    "sql injection": "SQL injection prevention prepared statements parameterized queries",
    "sqli": "SQL injection prevention prepared statements parameterized queries",
    "xss": "XSS prevention output encoding content security policy sanitization",
    "csrf": "CSRF prevention SameSite anti CSRF token",
    "ssrf": "SSRF prevention metadata service private IP allowlist",
    "rce": "remote code execution command injection prevention",
    "cve": "CVE vulnerability remediation patch advisory",
    "cwe": "CWE vulnerability mitigation secure coding",
    "jwt": "JWT token storage HttpOnly SameSite XSS CSRF",
    "token": "token storage leakage authentication security",
    "deserialization": "unsafe deserialization remote code execution prevention",
    "path traversal": "path traversal canonical path validation file access",
    "command injection": "command injection subprocess shell input validation",
    "file upload": "file upload security validation malware path traversal",
    "权限": "authorization access control bypass privilege escalation",
    "越权": "authorization bypass access control vulnerability",
    "认证": "authentication security session token password",
    "授权": "authorization access control permission check",
    "漏洞": "software vulnerability remediation secure coding",
    "注入": "injection vulnerability SQL command LDAP input validation",
    "路径穿越": "path traversal canonical path validation file access",
    "反序列化": "unsafe deserialization remote code execution prevention",
    "命令执行": "command injection remote code execution prevention",
    "文件上传": "file upload security validation malware path traversal",
}


DEFAULT_TOPIC_EXPANSIONS = [
    (
        re.compile(r"(只用短信验证码|短信验证码|otp|mfa|多因素|二次验证)", re.I),
        "MFA OTP account security authentication login brute force session token",
    ),
    (
        re.compile(r"(第三方.*脚本|统计脚本|前端.*脚本)", re.I),
        "third party JavaScript supply chain XSS CSP subresource integrity sanitization output encoding",
    ),
    (
        re.compile(r"(出站请求|统一走代理|egress|代理)", re.I),
        "egress control SSRF network policy metadata service redirect allowlist",
    ),
    (
        re.compile(r"(压缩包|自动解压|解压|zip|archive)", re.I),
        "archive extraction zip slip file upload path traversal filename canonical path sandbox",
    ),
    (
        re.compile(r"(安全修复\s*PR|修复\s*PR|pull request|\bPR\b)", re.I),
        "security PR code review supply chain dependency advisory vulnerability CVE GHSA package regression",
    ),
    (
        re.compile(r"(密钥轮换|key rotation)", re.I),
        "key rotation secret management availability configuration environment hardcoded secret",
    ),
    (
        re.compile(r"(移动端|抓包)", re.I),
        "mobile API transport security server side control authorization authentication rate limiting TLS",
    ),
    (
        re.compile(r"(鉴权.*SQL|SQL.*鉴权|审查顺序)", re.I),
        "code review authorization SQL injection taint flow threat modeling triage",
    ),
    (
        re.compile(r"(管理接口|admin)", re.I),
        "admin API threat modeling authorization access control code review triage",
    ),
    (
        re.compile(r"(AI.*文件上传|Agent.*文件上传)", re.I),
        "AI coding agent file upload secure design code review secret detection regression path traversal",
    ),
    (
        re.compile(r"(Agent.*认证|认证 helper|破坏边界)", re.I),
        "AI coding agent authentication regression code review authorization boundary tests",
    ),
]


DEFAULT_BLOCK_PATTERNS = [
    (
        "unsafe_request",
        re.compile(
            r"(打别人服务器|绕过.*(2fa|mfa|认证|鉴权|登录)|偷.*token|窃取.*token|"
            r"验证别人.*token|朋友的网站.*测试|批量试密码|自动扫公网|公网管理后台|"
            r"exploit payload|bypass authentication|steal .*token)",
            re.I,
        ),
        "Unsafe or unauthorized security request; do not enter normal RAG.",
    ),
    (
        "deceptive_request",
        re.compile(r"(编.*证明|伪造.*证明|假的安全审计|改得不那么严重|掩盖|隐藏.*漏洞|隐藏提交.*密钥)", re.I),
        "Deceptive security request; do not enter normal RAG.",
    ),
    (
        "privacy_request",
        re.compile(r"(私人邮箱|家庭地址|身份证|手机号.*归属|private key|cookie.*账号|是谁的账号)", re.I),
        "Privacy or secret-seeking request; do not enter normal RAG.",
    ),
    (
        "out_of_scope_business",
        re.compile(r"(哪家报价最低|报价最低|安全产品.*报价|价格最低|采购.*价格)", re.I),
        "Business, pricing, or procurement request; skip the code-security RAG.",
    ),
    (
        "future_or_guarantee",
        re.compile(r"(预测.*(漏洞|补丁|高危)|明天|下周|未来|永远不会|保证|肯定知道|未知变种)", re.I),
        "The question asks for future, absolute, or unknowable facts.",
    ),
    (
        "insufficient_evidence",
        re.compile(
            r"(线上.*(有没有|是否).*(被利用|安全)|外部访问过|真实用户密码泄露|"
            r"线上.*昨天.*被攻击|线上.*已经安全|闭源\s*SDK.*后门|第三方闭源\s*SDK.*后门|"
            r"没贴代码|没有代码|没法提供代码|没有日志|没有运行日志|日志被清理|"
            r"没有架构图|没有威胁模型|没有资产清单|不知道.*权限|内网拓扑.*知识库|"
            r"只看函数名|只凭扫描结果|只凭提交标题|截图很糊|用户说没问题|"
            r"没贴扫描结果|只看\s*README|没有任何证据|能证明系统没有|能确定具体漏洞吗|"
            r"断定.*合规|满足监管要求|满足等保三级|等保三级要求|供应链没有问题|"
            r"供应商没有泄露数据|判断这个告警严重吗|"
            r"AI 生成的安全建议一定|Agent 没发现安全问题|Agent 没看到源码|"
            r"只凭代码判断.*威胁模型|直接判断这个接口安全吗|一定是误报)",
            re.I,
        ),
        "The question needs local evidence, logs, code, or architecture context before RAG can answer.",
    ),
    (
        "out_of_scope_general",
        re.compile(
            r"(天气|润色|快速排序|dataclass|frozen=True|typescript 类型报错|"
            r"先学框架还是数据库|单元测试覆盖率低|项目计划|写周报|"
            r"泛型擦除.*性能|excel.*csv|首屏加载慢|goroutine 泄漏|nginx.*缓存|"
            r"动效卡顿|慢查询.*索引|内存占用高|转化率低|按钮文案|tcp 三次握手|"
            r"readme 怎么写|接口响应慢|缓存 key)",
            re.I,
        ),
        "General non-security request; skip the code-security RAG.",
    ),
]


DEFAULT_SECURITY_INTENTS = [
    "SQL injection prevention prepared statements parameterized queries",
    "XSS prevention output encoding content security policy sanitization",
    "CSRF prevention SameSite anti CSRF token validation",
    "SSRF prevention metadata service private IP allowlist denylist",
    "authorization bypass access control permission check vulnerability",
    "authentication session token password security best practices",
    "JWT token storage HttpOnly Secure SameSite cookie localStorage risk",
    "path traversal file access canonical path validation",
    "file upload security validation malware zip slip path traversal",
    "command injection subprocess shell true input validation escaping",
    "unsafe deserialization pickle yaml load remote code execution",
    "secret leakage credential token API key exposure logs",
    "dependency vulnerability CVE remediation package upgrade",
    "cryptography misuse weak hash encryption random number generation",
    "CORS misconfiguration origin allow credentials security",
    "rate limiting brute force login authentication protection",
    "权限绕过风险 接口越权 访问控制漏洞",
    "用户输入校验是否安全 注入风险 参数处理",
    "token 应该怎么存储 cookie localStorage 安全",
    "安全风险判断 是否有问题 要不要 够不够 算不算漏洞 如何评估影响",
    "修复建议 怎么修 怎么降低风险 安全设计 加固措施 回归测试",
    "代码审查排查 扫描器告警 先查什么 数据流 taint flow false positive",
    "日志 报错 debug stack trace token password pii sensitive data disclosure",
    "配置安全 密钥 环境变量 CORS TLS verify false hardcoded secret",
    "API 安全 rate limit csrf websocket webhook admin api request validation",
    "数据库 ORM raw query order by tenant id least privilege soft delete encryption",
    "云服务 容器 Dockerfile Kubernetes IAM CI CD base image metadata hostPath",
    "AI Coding Agent 生成代码 安全审查 自动修复 回归测试 lockfile Dockerfile",
    "证据不足 无代码 无日志 无架构图 不能确定 需要追问 上下文不足",
]
