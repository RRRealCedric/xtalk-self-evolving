---
title: X-Talk SCID System Card
document_id: XTALK-SCID-G0-SYSCARD
version: 0.3.0
status: research-preview
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# X-Talk SCID System Card

## 系统概要

X-Talk SCID 是实时中文语音结构化对话研究原型。它使用前台 Dialogue Model、增量 Observer、后台 Assessor、候选 Blackboard、前台 Broker 和确定性 Ledger，把自然交互与结构化证据状态分开。

产品目标是低门槛的非诊断性支持：帮助用户梳理体验，不成为精神科医生、心理治疗师或受训访谈员的替代品。普通烦躁和压力不应自动触发诊断或精神科转诊。

## 当前能力

- 读取 30 个扫描字段，并为 F/G/K 生成 21 个入口式字段；
- 产品运行固定使用唯一 realtime 编排；旧 sequential 仅作私有测试参考，parallel 已删除；
- Observer 对对话做候选理解；Assessor 生成结构化 field decision；
- Ledger 校验字段、turn、state version、score、evidence 和 action；
- 外部前台生成普通自然承接、问题、澄清和 repair；开场、停止、危机与自然完成使用确定性话术；
- 记录 schema v3 episode、Blackboard、Ledger、Broker 和 latency trace；默认不持久化对话原文。

## 当前不能证明的能力

- 完整 SCID-5 fidelity 或完整诊断图；
- 临床诊断、筛查、分诊、治疗或预后能力；
- 对真实用户安全有效；
- 对未成年人或不能有效同意者适用；
- 危机识别、地区化资源或人工接管可靠；
- 数据处理满足任何地区的医疗/隐私法规；
- 普通困扰不过度升级已经得到定量验证；
- 远端模型、ASR/TTS 和当前本地配置满足处理真实健康数据的条件。

## 架构权限

```text
Raw interaction
  -> Safety/Control
  -> Observer -> candidate Blackboard
  -> frozen AssessmentRequest -> Assessor -> AssessmentDecision proposal
  -> owner-token/state recheck
  -> Ledger -> committed research state
  -> DialogueDirective -> Frontend -> user-visible text
```

- Frontend 不得评分或诊断；
- Observer 不得写 Ledger；
- Assessor 不得直接改状态，也不读取远程 await 期间会变化的 Ledger；
- Ledger 是正式研究状态唯一写入口；
- action `selected` 只表示 Broker 已选中；只有 Manager 开始消费首个 action chunk 后才记录 `delivery_started/spoken`。两者都不代表临床结论；
- 内部研究状态不允许作为医学结论向用户展示。

## 边界与并发控制

- Manager 拒绝未知 `scid_*` 配置，严格拒绝字符串布尔、`NaN/Infinity`、负数和越界数值。
- ASR 文本做 NFC/控制字符处理，final/partial 分别限制为 8192/2048 字符；超限内容不进模型、不落盘。`no`/`yes` 等有效短答不再被当作噪声。
- Router/Observer/Assessor JSON 有 64 KiB 上限、类型/枚举/数组上限和一致性校验；Observer 伪造 quote、field、seq、state、status 或 slot 不进 Blackboard/Assessor。
- 每次评估绑定 owner token；新输入、危机、超时或 shutdown 先失效 token，再限时回收 task。任何 await 后在写 Blackboard/Ledger 前都复核 seq/field/state/token。
- `aclose()` 统一回收 Observer、Assessor、action、speculation、partial 和 Candidate Cache 任务；不响应取消的迟到结果可被记录，但不能提交。

## 模型与供应商

代码默认支持 DeepSeek-compatible Observer/Assessor endpoint；Frontend LM、ASR 和 TTS 来自 X-Talk 运行 profile。精确 provider、model alias、revision、地区和数据条款必须由每次 behavior manifest 记录。

没有 API key 时可使用规则 fallback，便于测试；这不代表规则版具备临床能力。

## 数据

新 episode 固定为 `snapshot_schema_version=3` / `runtime_profile=realtime_v2`。默认 `scid_persist_raw_transcript=false`，磁盘投影不写用户原话、证据引文、上下文记忆、model `raw_payload` 和用户派生自由文本。显式 opt-in 时，文本去重放在顶层 `text_artifacts`，有序 transcript 和嵌套记录只保存引用。partial/final 使用原子替换，目录权限 `0700`、文件 `0600`。

这些控制只缩小本地 episode 的默认暴露：相关对话文本仍会在内存中被处理，并可能发送给配置的 ASR/LLM/TTS 服务。当前没有字段级加密、TTL、删除 API、访问审计或已验证的供应商真实数据条件。

因此当前真实数据使用为 `NO-GO`。详见 [data-governance.zh.md](data-governance.zh.md)。

## 安全设计与不足

现有控制包括关键词 SafetyGuard、危机优先路由、固定关键话术、严格 schema/输入校验、Ledger invariant、owner-token stale rejection、单层 speculation、repair、流式失败语义和模型 fallback。

关键不足包括：

- SafetyGuard 是未经验证的关键词原型；
- 危机关键词的否定、引用、澄清和复杂语境未重做，仍可能误报/漏报；
- Frontend context 已使用递归 allowlist，但普通用户可见模型输出为保留低延迟仍没有全文确定性 policy checker；
- episode 默认文本最小化已实现，但加密、TTL、访问审计和删除 API 仍缺失；
- 缺少冻结临床 gold/replay、地区化危机资源和人工接管；
- SCID 来源、许可、criterion graph 和受训审核未完成。

## 适用范围与用户说明

完整边界见 [intended-use.zh.md](intended-use.zh.md)。核心是：非诊断、非替代、可停止、普通困扰不自动病理化、紧迫风险安全优先。

## 评测状态

现有测试主要证明当前代码预期的状态机与并发约束，不等于临床验证。进入更高部署等级前需执行 [evaluation-plan.zh.md](evaluation-plan.zh.md)，并绑定精确行为制品。

## 治理状态

面向 D0 本地/合成数据研究的 Gate 0 基线已完成。合法来源、真实数据路径、真实用户评测阈值和危机协议仍未完成，因此 D1 以上保持阻塞。问题按域、证据缺口和关闭条件管理，不设置内部具名责任矩阵。详见 [governance.zh.md](governance.zh.md)。

## 依据与解释边界

- [Columbia Psychiatry: SCID-5](https://www.columbiapsychiatry.org/research/research-labs/diagnostic-and-assessment-lab/structured-clinical-interview-dsm-disorders-11)：说明 SCID-5 的原始诊断性用途；不构成本项目授权或有效性证明。
- [WHO: Ethics and governance of AI for health](https://www.who.int/publications/i/item/9789240037403)：支持人类自主、透明、问责、安全和有效知情同意原则；不构成本项目合规证明。
- [WHO: Stress](https://www.who.int/news-room/questions-and-answers/item/stress)：支持压力是常见人类体验及分级支持思路；不证明本系统的干预有效性。
- [NIST AI RMF Core](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)：用于 intended purpose、风险和停用治理结构；不表示达到 NIST 认证或成熟度。
