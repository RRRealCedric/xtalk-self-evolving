---
title: X-Talk SCID Model-and-System Change Log
document_id: XTALK-SCID-G0-CHANGELOG
version: 0.3.0
status: active-template
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# Model-and-System Change Log

## 规则

以下任一变化都必须登记，不因“只改 prompt/config”而跳过：

- intended use、用户人群、排除场景、产品或营销声明；
- model、provider、endpoint、不可变 revision 或推理参数；
- Frontend、Router、Observer、Assessor prompt 或 JSON schema；
- SCID template、field graph、rubric、evidence slot 或 transition；
- Broker priority、timeout、confidence threshold、speculation/repair policy；
- SafetyGuard、危机资源、人工接管或停止条件；
- 数据来源、同意、留存、供应商、脱敏或删除；
- evaluator、dataset、阈值、统计方法或发布 profile。

## 变更记录

| 日期 | Change ID | 版本/Commit | 风险级别 | 摘要 | 证据 | 决策 | 问题域 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-23 | G0-BASELINE-001 | 工作区未提交 | R2/R3 governance | 冻结“SCID-informed 非诊断支持、非替代、普通困扰不自动转诊”的 Gate 0 基线；加入启动披露和治理制品 | PsychSOP 测试 `117 passed`；文档基线已建立 | D0 baseline / stop above D0 | BOUNDARY / SAFETY / SYSTEM |
| 2026-07-23 | G0-GOV-002 | 工作区未提交 | R0 governance | 将大组织式 RACI、具名责任和内部签字链改为问题域、证据、状态和关闭条件 | README 与 Gate 0 文档一致性检查 | lightweight governance adopted | BOUNDARY / RELEASE |
| 2026-07-23 | SCID-HARDEN-003 | 工作区未提交 | R2 system/data | Episode 升级为 schema v3 / realtime_v2，原文默认关闭且 opt-in 去重引用；加入严格输入/模型/template 校验、AssessmentRequest + owner-token 并发边界、`aclose()`、原子私有写入和固定关键话术 | 代码、回归测试与 README/Gate 0 事实更新；不触碰危机否定/引用语义或普通前台全文审查 | D0 hardening / D1+ remains blocked | SYSTEM / DATA / SAFETY / BOUNDARY |

## 新记录模板

```yaml
change_id:
date:
current_driver_optional:
risk_level: R0 | R1 | R2 | R3
motivation:
intended_behavior_change:
non_goals:
files_and_artifacts:
before_manifest:
after_manifest:
data_impact:
clinical_safety_impact:
privacy_security_impact:
evaluation_plan:
evaluation_result:
evidence_check:
rollout_level:
health_gates:
rollback_plan:
decision: draft | proceed | proceed-with-limits | stop | rolled-back
linked_incident_or_adr:
```

不得把同一模型 alias 下的供应商静默更新当成“无变更”。若无法获得不可变 revision，应把 alias 漂移作为 material change 风险并重新评测。
