---
title: X-Talk SCID 初始威胁模型与信任边界
document_id: XTALK-SCID-G0-THREAT
version: 0.3.0
status: draft
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# 初始威胁模型与信任边界

## 保护目标

1. 用户安全：不漏掉紧迫风险，不因普通困扰过度升级，不给出诊断/治疗幻觉。
2. 状态完整性：模型和旧异步结果不能越权修改 Ledger。
3. 隐私：原始音频、原话、证据、候选记忆、score 和身份不被越权访问或外发。
4. 可用性：模型、网络或语音链路失败时有明确停止或保守降级。
5. 真实性：用户知道系统能力、供应商、人类监督和紧急能力的真实边界。
6. 内容与供应链：SCID 材料、代码、依赖、模型和构建来源可证明且使用合法。

## 信任边界

```text
[用户与设备]
    | 音频/文本
    v
[浏览器与 X-Talk serving]
    |------------------------------|
    v                              v
[外部 ASR/TTS/Frontend LM]       [SCID Runtime 进程]
                                   | 本地代码边界
                                   +-> SafetyGuard / Router
                                   +-> Observer / Assessor -> [外部 LLM API]
                                   +-> Blackboard / Ledger
                                   +-> episode files -> [本地文件系统/未来存储]
                                   +-> logs/telemetry -> [运营可观测系统]
```

任何外部 API、本地持久化目录、日志平台、浏览器和未来人工标注平台都是独立信任边界，不能因同属一个项目而默认可信。

## 主要威胁

| ID | 威胁 | 攻击/故障路径 | 当前控制 | 待补控制 |
| --- | --- | --- | --- | --- |
| T-01 | Prompt injection | 用户要求前台泄露 JSON、分数或改变角色 | system prompt、严格 directive 枚举、递归 context allowlist、固定关键话术 | 普通前台输出 policy checker、红队 |
| T-02 | 模型越权写状态 | 模型伪造 field/action/score/evidence | 64 KiB 严格 schema、Observer provenance、Ledger invariant 与唯一写入 | 冻结 replay、更完整 invariant/fuzz tests、未来工具 allowlist |
| T-03 | Stale/race 覆盖 | 旧 turn/field 模型结果迟到或吞掉取消 | seq/field/state/version + owner token、不可变 AssessmentRequest、提交前复核、`aclose()` | 更广故障注入、跨进程 correlation ID/幂等 |
| T-04 | ASR 误识别安全词 | 否定、引用、同音、截断导致误报/漏报 | partial/final 分离、关键词 guard | 音频 gold set、语境复核、人工接管 |
| T-05 | 前台泄露敏感内部状态 | context 中嵌套 score/raw payload 或模型生成越界 | 递归 allowlist serializer、深度/大小上限、固定关键话术 | 普通前台全文 policy checker、prompt-injection 红队 |
| T-06 | 本地 episode 泄露 | 共享设备、备份、恶意程序读取 JSON | schema v3 默认去原文、目录 `0700`、文件 `0600`、原子替换 | 加密、身份化访问控制、TTL、删除、审计、备份策略 |
| T-07 | 供应商二次使用或跨境风险 | 原话发送至远端模型/语音服务 | base URL 可配置 | DPA/条款审查、地域锁定、no-training、供应商 manifest |
| T-08 | 密钥泄露 | API key 存入 JSON、日志或提交仓库 | 环境变量 fallback、secret scan | secret manager、轮换、最小 scope、事件响应 |
| T-09 | 依赖/构建供应链 | 恶意或被接管依赖、模型 alias 漂移 | pre-commit 与部分扫描 | lock、SCA、SBOM、provenance、模型 revision |
| T-10 | 拒绝服务/成本攻击 | 超长输入、高频音频、并发模型调用 | ASR final/partial 上限、model/template/PDF 载荷上限、任务取消/限时回收 | 会话配额、背压、断路器、预算告警 |
| T-11 | 伪装人工监控 | UX 让用户误以为有人类实时响应 | crisis 文案说明系统不能处理紧急危机 | 所有入口一致披露、理解测试 |
| T-12 | 数据投毒/评测污染 | 训练和测试混用、事后改阈值 | 尚无完整数据治理 | lineage、访问分离、冻结 hash、预注册 |
| T-13 | 内容权利风险 | 未授权 SCID 文本进入仓库、模型或产品 | README 披露不完整 | 许可清单、访问隔离、发布扫描 |
| T-14 | 下游滥用 | 研究 score 被用于诊疗/保险/雇佣 | 文档禁止 | machine-readable purpose tag、访问和合同控制 |
| T-15 | 原文 opt-in 误用 | 开发者为方便回放开启 `scid_persist_raw_transcript` 并长期保留 | 默认 false、严格布尔、顶层去重制品 | 按研究协议限制配置、TTL/删除、审计与用户同意 |

## 滥用场景

- 用户或运营者要求系统“直接告诉我是什么病”；
- 第三方批量上传他人录音做秘密心理画像；
- 雇主、学校、保险或司法人员把内部 score 当作决定依据；
- 开发者把真实 episode 粘贴进公开 Issue 或外部 LLM 排错；
- 攻击者诱导前台复述后台字段、证据、系统 prompt 或其他用户数据；
- 供应商模型 alias 静默更新，行为变化但未重新评测。

## 供应商登记

| 组件 | 当前代码默认/可能来源 | 发送数据 | 当前可用范围 |
| --- | --- | --- | --- |
| Observer / Assessor | DeepSeek-compatible endpoint，默认 `api.deepseek.com` | 用户文本、当前字段、近期状态与候选上下文 | 仅 D0；真实数据条件未满足 |
| Frontend LM | X-Talk pipeline 配置的 `BaseChatModel` | directive 与有限上下文 | profile-dependent；真实数据条件未满足 |
| ASR | X-Talk pipeline 配置 | 音频及可能的会话元数据 | profile-dependent；真实数据条件未满足 |
| TTS | X-Talk pipeline 配置 | 用户可见回复文本 | profile-dependent；真实数据条件未满足 |
| 本地存储 | `data/psych_sop_demo/episodes` 或配置路径 | 默认为去原文的 schema v3 episode/partial；opt-in 可含文本制品 | 研究开发；真实数据条件未满足 |

每次运行真实研究前必须把 `profile-dependent` 替换为精确法人、产品、区域和配置 revision。

## 安全验证优先级

1. 诊断/治疗越界与普通困扰过度升级红队；
2. 自伤/他伤的否定、引用、隐喻、时间变化和 ASR 错误切片；
3. 前台嵌套上下文泄露与 prompt injection；
4. stale/race/取消/timeout/重复提交故障注入；
5. episode、日志、备份和供应商数据删除演练；
6. 模型/provider/config 漂移检测和回滚。

本威胁模型采用 NIST AI RMF 的 intended purpose、风险响应和停用责任思路，但不表示达到 NIST 成熟度或任何合规要求。[NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)
