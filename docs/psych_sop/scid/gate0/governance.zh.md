---
title: X-Talk SCID 问题域与发布检查
document_id: XTALK-SCID-G0-GOV
version: 0.2.0
status: active-lightweight
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# 问题域与发布检查

## 为什么采用轻量治理

本项目规模较小，不建立模拟大组织的 RACI、永久岗位、层层签字或内部追责链。治理的对象是**系统中的问题及其证据**，不是“谁应该背锅”。一个人可以同时完成产品、工程、评测和文档工作，也可以随着项目阶段调整分工。

轻量不等于随意。每个可能影响行为或安全的问题，都必须能够回答：

1. 问题发生在哪个问题域；
2. 观察到了什么现象，影响什么范围；
3. 如何复现，当前有哪些证据；
4. 已有哪些控制，还缺什么；
5. 下一步动作和关闭条件是什么；
6. 当前状态是否允许进入更高风险环境。

内部研究以版本化记录、测试和可回放证据作为判断依据。只有范围扩大到真实用户、真实健康数据、临床研究或产品部署时，才根据地区和用途引入必要的法律、伦理、隐私、安全或临床专业核验。这些是外部准入条件，不是给小项目增加内部职位。

## 问题域

| 问题域 | 主要定位问题 | 典型证据 | 阻塞示例 |
| --- | --- | --- | --- |
| `BOUNDARY` 产品边界 | 系统允许做什么、明确不做什么，用户是否能理解 | intended use、启动说明、理解度测试 | 暗示诊断或替代专业人员 |
| `DOMAIN` 领域结构 | SCID 来源、字段、证据槽和跳转是否准确 | 来源清单、field graph、rubric、gold fixture | 来源/许可不清或规则错误 |
| `SAFETY` 用户安全 | 是否过度病理化、漏掉紧迫风险或错误升级 | 风险切片、危机 replay、停止演练 | 紧迫风险路径未验证 |
| `DATA` 数据与隐私 | 收集什么、发给谁、保存多久、如何删除 | data inventory、provider manifest、删除测试 | 真实数据缺少合法依据或删除路径 |
| `SYSTEM` 系统可靠性 | 状态、并发、超时、权限和故障降级是否正确 | 单元测试、fault injection、trace、SLO | 非法 Ledger 写入或无法停止 |
| `EVAL` 评测 | 数据、指标、阈值和结论是否可复现 | 冻结数据集、eval spec、报告、失败样例 | 看完结果后无记录地改阈值 |
| `RELEASE` 发布与恢复 | 发布的是哪个版本，能否停止和回滚 | behavior manifest、health check、rollback 演练 | 制品不一致或不可回滚 |

问题域用于快速定位，不代表组织部门，也不要求每个域配一个人。

## 问题记录最小格式

发现问题时，Issue、风险登记或实验记录至少包含：

```yaml
issue_id:
domain: BOUNDARY | DOMAIN | SAFETY | DATA | SYSTEM | EVAL | RELEASE
symptom:
impact_and_scope:
reproduction:
evidence:
current_control:
evidence_gap:
next_action:
status: open | mitigating | evidence-ready | closed | blocked
linked_test_or_replay:
closure_condition:
```

可以临时记录 `current_driver` 方便协作，但它不是永久责任归属，也不是关闭问题的证据。问题关闭依赖可检查的结果，而不是姓名或签字。

## 部署等级与证据条件

| 等级 | 范围 | 必须具备的证据/外部条件 | 当前状态 |
| --- | --- | --- | --- |
| D0 | 本地、合成数据、开发者知情自测 | 产品边界已写明；测试可运行；不接触未经许可的真实健康数据 | 当前允许 |
| D1 | 合法获得的去标识化离线数据 | 来源与用途可证明；最小化、访问、留存和删除条件明确；满足适用法律/伦理要求 | BLOCKED |
| D2 | 不影响真实决策的 shadow study | D1 + 冻结评测、危机路径、供应商数据流和停止机制 | BLOCKED |
| D3 | 有人工监督、可立即接管的限定试点 | D2 + 适用的临床、安全、隐私与伦理外部核验；试点和退出协议 | BLOCKED |
| D4 | 生产或临床相关用途 | 独立前瞻证据、适用监管/质量要求、持续监测和事故响应 | BLOCKED |

## 硬性停止条件

以下任一项成立，不得进入对应的更高风险等级：

- intended use、目标人群、排除范围或用户说明不明确；
- SCID 来源、许可或改编范围无法核验；
- 真实数据的合法依据、同意、供应商、留存或删除条件缺失；
- 危机资源、人工接管、停止机制或演练未完成；
- 普通困扰过度升级与严重风险漏报没有冻结评测；
- 部署行为无法绑定 code、model、prompt、template、config 和 eval 版本；
- 存在未关闭的 critical 安全或隐私问题；
- 用户无法停止，或不能理解系统不是诊断/治疗服务；
- 评测集、阈值或报告在看到结果后被无记录地修改。

这些条件根据证据判断，不通过增加一个“责任人”来解除。

## 范围变更记录

D1 以上或任何实质行为变更至少记录：

```yaml
decision_id:
date:
requested_deployment_level:
exact_commit_sha:
behavior_manifest_id:
intended_use_version:
dataset_and_eval_report:
open_issues_and_domains:
hard_gate_checklist:
decision: proceed | proceed-with-limits | stop
limits_and_expiry:
rollback_condition:
external_review_or_legal_basis_if_applicable:
```

`proceed-with-limits` 必须写明范围、到期时间和自动停止条件。记录的目的，是以后能够复原“当时依据什么做了什么决定”，而不是追究具体个人。
