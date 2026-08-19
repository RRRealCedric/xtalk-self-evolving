---
title: X-Talk SCID Gate 0 治理基线
document_id: XTALK-SCID-G0-INDEX
version: 0.3.0
status: gate0-complete-for-d0
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# X-Talk SCID Gate 0 治理基线

## 当前结论

本目录已完成面向 **D0 本地研究**的 Gate 0 基线。当前只允许开发者本地、合成数据或另行满足使用条件的测试数据研究；这不代表已经允许采集真实患者数据、开展真实用户试点、宣称诊断能力或临床有效性。

本项目不设置 RACI、具名责任矩阵或内部签字链。后续范围是否可以扩大，根据对应问题域的证据和外部准入条件判断。目前 D1 以上仍被以下问题阻塞：

- SCID 内容来源、许可、改编和当前本地材料的合法使用范围尚未完成书面核验；
- 真实数据同意、最小化、供应商传输、留存、更正和删除流程尚未满足适用要求并实现；
- 危机路径、分级建议、普通困扰不过度转诊等关键行为尚无冻结评测集和独立评审证据；
- schema v3 已默认不把对话原文、证据引文和派生自由文本写入 episode，但原文 opt-in 仍存在，且运行时相关文本仍可能发送给配置的 ASR、LLM 和 TTS 供应商。因此默认存储最小化不会自动解锁真实数据范围。

## 当前工程基线

- 新 episode 固定为 `snapshot_schema_version=3` 和 `runtime_profile=realtime_v2`；历史 episode 不迁移。
- partial/final snapshot 使用同目录临时文件、`flush/fsync/os.replace`，目录权限 `0700`、文件权限 `0600`。
- 开场说明、用户停止、危机回应和自然完成使用版本化确定性文本，不交给外部 Dialogue Model 改写。
- Assessor 只接收不可变 `AssessmentRequest`；远端调用移出 Ledger lock，以 seq/field/state/owner token 复核阻止迟到提交。shutdown 统一通过 `aclose()` 限时回收 runtime-owned task。
- 配置、ASR、模型 JSON、Observer provenance、template 和 PDF 已增加严格类型、大小与一致性校验；malformed 边界 fail closed。

这些是工程缓解，不是临床验证。危机关键词的否定/引用/澄清语义与普通前台全文输出审查仍是明确的剩余风险。

## 已冻结的产品初心

> 用 SCID 的结构化提问纪律帮助用户更清楚地描述和理解自己的情绪、体验、时间过程与生活影响，提供低门槛、专业、可停止的非诊断性支持；不把系统包装成精神科医生、心理治疗师或受训 SCID 访谈员的替代品，也不因普通烦躁、压力或短暂情绪波动就自动给用户贴标签或要求就医。

分级原则：

1. 普通、短暂、可应对的情绪困扰：继续倾听、结构化梳理和提供选择，不自动转诊。
2. 持续、加重、反复或明显影响生活功能的困扰：说明系统局限，并提供由用户决定的专业支持选项；不宣布诊断。
3. 自伤、他伤或其他紧迫安全风险：中断普通流程，优先安全和地区化人工/紧急资源；当前地区化协议尚未完成，因此真实用户使用仍被阻塞。

## 制品清单

| Gate 0 交付物 | 仓库制品 | 状态 |
| --- | --- | --- |
| intended use、适用人群、排除场景、非目标、用户说明、停止条件 | [intended-use.zh.md](intended-use.zh.md) | D0 已生效；真实用户范围待专业核验 |
| 风险登记与 safety case 骨架 | [risk-and-safety-case.zh.md](risk-and-safety-case.zh.md) | 首版已写；D1+ Blocker 尚未关闭 |
| 问题域与发布检查 | [governance.zh.md](governance.zh.md) | 轻量问题定位机制已定义；D0 可用 |
| 数据清单与生命周期 | [data-governance.zh.md](data-governance.zh.md) | 当前数据流已盘点；真实数据路径仍阻塞 |
| 初始 threat model、供应商与信任边界 | [threat-model.zh.md](threat-model.zh.md) | 首版已写；部署 profile 供应商待锁定 |
| 预注册评测计划 | [evaluation-plan.zh.md](evaluation-plan.zh.md) | 指标和规则已定义；数值阈值待盲审冻结 |
| system card | [system-card.zh.md](system-card.zh.md) | 首版已写 |
| model-and-system change log | [change-log.zh.md](change-log.zh.md) | 模板已写；后续行为变更必须登记 |

## 按部署等级检查 Gate 0 证据

| 检查项 | 当前证据 | 结论 |
| --- | --- | --- |
| Blocker 风险有问题域、证据缺口、下一步和关闭/降级条件 | 已建立首版记录 | PASS FOR D0 |
| 真实数据条件与删除路径通过伦理、隐私、法律审查 | 仅有草案；实现不完整 | BLOCKED |
| 关键指标在查看结果前定义接受规则 | 有规则框架；真实研究阈值和样本量仍需冻结 | PASS FOR D0 / BLOCKED FOR D1+ |
| 用户可见非诊断说明与停止权 | 已接入 runtime 启动文案并有测试 | PARTIAL PASS |
| 当前部署范围受限 | 文档冻结为 research-only；尚无部署级强制开关 | PARTIAL PASS |

## 变更规则

- 本目录版本采用语义化版本。修改产品人群、允许输出、升级阈值、数据用途或供应商数据流属于实质行为变更，至少提升 minor 版本，并完成 R2/R3 对应的证据检查。
- 任何真实用户研究必须引用本目录的精确 commit SHA 和文档版本，而不是笼统引用“最新 README”。
- 扩大部署等级前，必须按 [governance.zh.md](governance.zh.md) 关闭相应证据缺口并记录范围决定；不以内部签字代替证据。
- “非诊断”不是免责措辞；系统能力、营销表述、真实使用方式和适用地区要求仍需独立法律、伦理和专业判断。
