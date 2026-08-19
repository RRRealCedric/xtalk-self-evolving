---
title: X-Talk SCID 风险登记与 Safety Case 骨架
document_id: XTALK-SCID-G0-RISK
version: 0.3.0
status: draft-open-risks
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# 风险登记与 Safety Case 骨架

## 评定规则

- 影响：`Catastrophic / Major / Moderate / Minor`。
- 可能性：`Likely / Possible / Unlikely / Unknown`。当前没有可靠发生率时必须写 `Unknown`。
- 状态：`OPEN / MITIGATING / EVIDENCE-READY / CLOSED / BLOCKED`。
- 法律、伦理、知情同意、危机接管和真实数据治理等缺口必须由证据关闭，不能用主观接受风险来豁免。
- 每项风险绑定问题域、证据缺口和关闭条件；不要求为内部研究设置永久具名责任人。

## 风险登记

| ID | 危害与触发 | 潜在影响 | 当前控制 | 仍需证据/控制 | 问题域 | Gate | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| H-01 | 系统直接或暗示性给出诊断标签 | 污名、焦虑、错误自我认知或错误决定 | Frontend prompt 禁止诊断；内部状态不直接展示 | 输出分类器、红队集、人工盲审、营销/UX 审查 | Clinical + Product | 0/3 | OPEN |
| H-02 | 对明显或持续问题错误安慰，暗示“不需要帮助” | 延误人类评估或支持 | 当前无明确“无需就医”模板 | false-safe gold set、分级响应评测、用户纠错入口 | Clinical + Safety | 0/3 | OPEN |
| H-03 | 把普通烦躁、压力或短暂情绪自动病理化/转诊 | 不必要恐慌、医疗化、信任损害 | intended-use 明确分级；普通困扰不自动转诊 | 过度升级率、普通压力切片、独立盲审 | Clinical + Human Factors | 0/3 | OPEN |
| H-04 | 未识别自伤、他伤或迫近危险 | 严重伤害 | 同步关键词 SafetyGuard；Assessor crisis action | 地区化协议、人工接管、语境分类器、召回评测与演练 | Safety | 0/5 | OPEN |
| H-05 | 危机误报或过度反应 | 惊吓、退出、失去信任、资源滥用 | high 关键词抢占；危机回应为固定边界话术 | 否定/引用/澄清/复合语境分类、分层措辞、false-positive 阈值 | Safety + Clinical | 0/3 | OPEN |
| H-06 | 模型把候选状态当成正式事实 | 错误追问、错误推进 | Blackboard/Ledger 分层；Ledger 唯一写入；严格 schema/provenance；冻结 AssessmentRequest + owner token | 冻结 replay 和更广 fault injection | Engineering | 1 | MITIGATING |
| H-07 | speculative next question 与正式结果冲突 | 用户困惑、遗漏证据 | 最大深度 1、repair、字段/版本校验 | wrong-advance 冻结回放为零、repair UX 盲审 | Engineering + Clinical | 1/3 | MITIGATING |
| H-08 | SCID 结构不完整或转换错误 | 伪专业感、错误证据链或路径 | README 披露仅 30+21 入口 | 合法来源、版本化 field graph、受训审核、transition tests | Domain | 0/2 | OPEN |
| H-09 | SCID 内容来源、许可或改编范围不合法 | 权利侵害、项目无法发布 | 当前仅代码库局部材料 | 书面来源/许可清单、允许用途与分发边界 | Legal + Domain | 0 | OPEN |
| H-10 | 原话、证据或身份数据泄露给日志/供应商 | 隐私伤害、歧视或合规风险 | schema v3 默认去原文；opt-in 去重引用；原子写；`0700/0600`；API key 不写 episode | 加密、TTL、访问/删除/审计、供应商协议、DPIA、opt-in 约束 | Privacy + Security | 0/3 | MITIGATING |
| H-11 | ASR 错误、提示注入或模型幻觉改变问题含义 | 错误理解、越界陈述 | NFC/输入上限、严格 schema/provenance、递归 frontend allowlist、fallback、owner-token stale 校验 | adversarial audio/prompt injection 测试、普通输出 guard | Security + Engineering | 0/3 | OPEN |
| H-12 | 模型/网络超时造成沉默、重复或错误恢复 | 体验损害；危机场景下更严重 | 零 chunk fallback、中途失败截断记录、owner-token 失效、`aclose()` 限时回收、telemetry | 请求级 deadline/断路器、容量和故障演练 | SRE + Engineering | 1/5 | MITIGATING |
| H-13 | 未成年人或无法同意者使用 | 同意无效、脆弱人群伤害 | 文档暂时排除 | 年龄/能力协议、监护与伦理审查、不可仅靠自报 | Ethics + Privacy | 0/5 | OPEN |
| H-14 | 内部 score 被下游当成医学结论或高影响决策依据 | 不当医疗、保险、就业或司法决定 | 文档定义为研究状态 | 导出标识、访问控制、用途绑定、下游合同与审计 | Product + Legal | 0/5 | OPEN |
| H-15 | 用户误以为有人类实时监控或系统会自动求救 | 危机时依赖不存在的能力 | 当前 crisis 文案说明系统不能处理紧急危机 | 全入口一致披露、理解度测试、地区化人工能力声明 | Safety + Human Factors | 0/5 | OPEN |

## Safety case 主张—论据—证据骨架

### C1：产品不会被设计为精神科医生或治疗师的替代品

论据：用户说明、Frontend 权限和输出策略共同禁止诊断、治疗、用药和高影响决策。

现有证据：

- [intended-use.zh.md](intended-use.zh.md) 已冻结允许/禁止用途；
- runtime 启动时提供非诊断、停止权和紧迫风险说明；
- Frontend prompt 禁止评分、诊断和内部字段泄露。

缺失证据：全路径输出扫描、提示注入红队、用户理解度测试、营销与 UI 审查。结论：`NOT YET SUPPORTED FOR REAL USERS`。

### C2：普通情绪困扰不会自动触发诊断或精神科转诊

论据：响应按普通困扰、持续/显著困扰、紧迫风险分层；升级依据是持续性、功能影响和安全风险，而不是“出现负面情绪”本身。

现有证据：产品契约已经定义该规则。

缺失证据：运行时尚无完整分级支持策略；SafetyGuard 仍是关键词规则；没有普通烦躁/压力的过度升级评测。结论：`CLAIM DEFINED, IMPLEMENTATION INCOMPLETE`。

### C3：模型不能直接提交正式结构化状态

论据：Observer 只写候选 Blackboard，Assessor 只返回 proposal，Ledger 校验字段、版本、动作和证据后提交。

现有证据：代码和现有 `test_psych_scid.py` 状态机/并发测试，包括不可变 AssessmentRequest、严格 Ledger invariant、owner-token 迟到结果、重复 seq/apply 和 shutdown 回收路径。

缺失证据：冻结 replay、完整 fault injection、行为 manifest 和系统性复查。结论：`PARTIALLY SUPPORTED`。

### C4：紧迫安全风险优先于普通访谈

论据：SafetyGuard 位于控制与 Assessor 路径之前，危机动作终止普通字段流程。

现有证据：关键词规则、固定危机话术、抢占/失效与危机单元测试。

缺失证据：当前未重做关键词的上下文否定、引用、澄清和隐喻语义；ASR 错误、多语言、地区资源、人工接管和真实演练也缺失。结论：`RESEARCH CONTROL ONLY`。

### C5：敏感数据只在明确同意和最小必要范围内处理

论据目标：逐字段数据清单、用途限制、最短留存、可删除、供应商透明和访问审计。

现有证据：数据流已盘点；schema v3 默认不持久化用户原话、证据引文、记忆与 model raw payload；原文 opt-in 使用去重制品/引用；partial/final 原子写入且使用私有本地权限。

缺失证据：当前仍无加密、TTL、用户删除、访问审计和原文 opt-in 的研究级强制约束，也未证明供应商满足真实数据处理条件。结论：`NOT SATISFIED`。

## 事故触发条件

以下任一事件必须冻结相关真实研究/部署并启动复盘：

- 用户可见诊断、用药或“无需帮助”的确定性结论；
- 紧迫风险未升级或系统错误宣称已经联系人类/报警；
- 普通压力切片出现系统性过度转诊；
- 非法 Ledger 写入、跨轮覆盖或多层 speculative advance；
- 未授权真实健康数据进入 episode、日志或外部供应商；
- 评测制品与实际部署的模型、prompt、模板或配置不一致；
- SCID 来源/许可无法证明或被权利方质疑。
