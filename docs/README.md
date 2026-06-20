# Docs Index

本目录按主题整理 taleclaw 的设计说明、阶段记录和部署文档。建议面试或代码走读时先看 `overview/`，再按主题深入。

## Overview

- [当前代码目录总结](overview/CODEBASE_SUMMARY.md)
- [Project Structure](overview/PROJECT_STRUCTURE.md)
- [课堂代码演示详稿](overview/CLASS_DEMO_SCRIPT.md)
- [taleclaw Runtime Platform 与 Coding Agent](overview/PLATFORM_AND_CODING_AGENT.md)
- [taleclaw Coding Agent](overview/CODING_AGENT.md)

## System Design

- [系统设计专题文档](system-design/README.md)

## Runtime

- [Agent Runtime 解耦改造记录](runtime/AGENT_RUNTIME_DECOUPLING_REFACTOR.md)
- [Agent Loop 不可见工具循环保护修复记录](runtime/AGENT_LOOP_UNAVAILABLE_TOOL_GUARD.md)
- [Hybrid 模式：关键词预筛选与 LLM 路由改动记录](runtime/HYBRID_MODE_LLM_ROUTING.md)
- [Model Provider Pool and Route Selection](runtime/MODEL_PROVIDER_POOL_ROUTING.md)
- [Model Provider 健康检查与自动切换完成记录](workplan/2026-06-11_MODEL_PROVIDER_HEALTH_FAILOVER_COMPLETION.md)

## Sessions And Memory

- [Phase 17：Memory Lifecycle 说明](sessions-memory/PHASE17_MEMORY_LIFECYCLE.md)
- [Task Session Isolation](sessions-memory/TASK_SESSION_ISOLATION.md)
- [TaskSession Memory Scope 修复记录](sessions-memory/TASK_SESSION_MEMORY_SCOPE_FIX.md)
- [Task Memory 混合提取与日志隔离改动记录](sessions-memory/TASK_MEMORY_HYBRID_PROMOTION.md)

## Tools And Plugins

- [Phase 18：Tool Search / Deferred Tools 说明](tools-plugins/PHASE18_TOOL_SEARCH_DEFERRED_TOOLS.md)
- [Phase 19：Plugin MVP 说明](tools-plugins/PHASE19_PLUGIN_MVP.md)
- [Bot 模式受限文件区第一阶段改动记录](tools-plugins/BOT_STORAGE_ARTIFACT_TOOLS.md)
- [Bot 临时沙盒与显式发布改动记录](tools-plugins/BOT_TASK_SANDBOX_AND_PUBLISH.md)
- [Markdown 转 PDF 插件改动记录](tools-plugins/MARKDOWN_PDF_PLUGIN.md)

## Web

- [Web Login And Registration](web/WEB_LOGIN_AND_REGISTRATION.md)
- [Web Multi-User Isolation](web/WEB_MULTI_USER_ISOLATION.md)
- [Web 会话删除改动记录](web/WEB_SESSION_DELETION.md)
- [taleclaw Web 流式输出改动记录](web/WEB_STREAMING_OUTPUT.md)
- [taleclaw 文件预览弹窗改动记录](web/WEB_FILE_PREVIEW_MODAL.md)
- [taleclaw Web UI 暗色改版与验证记录](web/WEB_UI_DARK_REFRESH_VERIFICATION.md)

## Gateways

- [Telegram Gateway](gateways/TELEGRAM_GATEWAY.md)
- [Feishu Gateway](gateways/FEISHU_GATEWAY.md)

## Deployment

- [taleclaw 首尔服务器完整部署手册](deployment/SEOUL_SERVER_DEPLOYMENT.md)

## Roadmap And Research

- [mytry 改造成 Nanobot + Coding Assistant 的计划](roadmap-research/REFACTOR_PLAN.md)
- [Function Strength Roadmap](roadmap-research/FUNCTION_STRENGTH_ROADMAP.md)
- [Akashic Agent 学习与后续改进路线](roadmap-research/AKASHIC_AGENT_LEARNING_PLAN.md)
- [Pico Feature Integration Plan](roadmap-research/PICO_FEATURE_INTEGRATION_PLAN.md)
- [Security Review RAG Persona Plan](roadmap-research/SECURITY_REVIEW_RAG_PERSONA_PLAN.md)
- [taleclaw UI 改进与同类 Agent 调研报告](roadmap-research/AGENT_PRODUCT_RESEARCH_AND_UI_REFRESH.md)
- [mytry 项目结构优化建议](roadmap-research/STRUCTURE_SUGGESTION.md)

## Workplan

- [Context Section Budget V1 完成记录](workplan/2026-06-11_CONTEXT_SECTION_BUDGET_V1_COMPLETION.md)
- [Context 逻辑分层与消息装配完成记录](workplan/2026-06-11_CONTEXT_LOGICAL_SECTION_ASSEMBLY_COMPLETION.md)
- [Context Section Report 第一阶段完成记录](workplan/2026-06-11_CONTEXT_SECTION_REPORT_COMPLETION.md)
- [按模式拆分 Assistant / Coding Instructions 完成记录](workplan/2026-06-11_MODE_INSTRUCTIONS_COMPLETION.md)
- [2026-06-10 工作总结](workplan/2026-06-10_DAILY_WORK_SUMMARY.md)
- [Runtime 平台化收束改进计划](workplan/2026-06-09_RUNTIME_PLATFORM_REFOCUS_PLAN.md)
- [任务完成文档留痕约定](workplan/2026-06-09_TASK_COMPLETION_DOCUMENTATION_RULE.md)
- [Runtime 第一阶段完成记录：RunState + TraceStore](workplan/2026-06-09_RUNTIME_PHASE1_RUNSTATE_TRACESTORE_COMPLETION.md)
- [Runtime 第二至第五阶段完成记录](workplan/2026-06-09_RUNTIME_PHASE2_TO_5_COMPLETION.md)
- [Runtime 目录结构重组完成记录](workplan/2026-06-10_RUNTIME_DIRECTORY_RESTRUCTURE_COMPLETION.md)
- [Legacy 兼容目录清理完成记录](workplan/2026-06-10_LEGACY_COMPAT_CLEANUP_COMPLETION.md)
- [Scheduler Agent 移除完成记录](workplan/2026-06-10_SCHEDULER_AGENT_REMOVAL_COMPLETION.md)
- [RuntimeKernel 移除完成记录](workplan/2026-06-10_RUNTIME_KERNEL_REMOVAL_COMPLETION.md)
