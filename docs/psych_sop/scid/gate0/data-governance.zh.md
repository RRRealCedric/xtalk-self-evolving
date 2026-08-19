---
title: X-Talk SCID 数据清单与治理基线
document_id: XTALK-SCID-G0-DATA
version: 0.3.0
status: draft-real-data-prohibited
created: 2026-07-23
updated: 2026-07-23
deployment_scope: research-only
---

# 数据清单与治理基线

## 当前总规则

在真实数据的来源与用途获得证明、满足适用的法律/伦理条件，并实现下述控制前：

- 不得主动收集真实患者或真实用户健康数据；
- 只允许合成数据、开发者明确知情的自测数据，或来源、用途和处理条件均可证明的去标识化离线数据；
- 不得因为文件位于本地磁盘，就假设它不构成敏感数据；
- 不得把启动免责声明当成数据处理同意。

## 当前数据清单

| 数据类别 | 示例 | 来源 | 当前去向/存储 | 敏感度 | 当前缺口 |
| --- | --- | --- | --- | --- | --- |
| 原始音频 | 麦克风流 | 用户/浏览器 | 上游 ASR 链路；SCID runtime 不直接保存 | 高 | 供应商、保留和传输随 profile 变化，未统一盘点 |
| ASR partial/final | 用户逐字文本 | ASR | Runtime 内存；schema v3 默认 episode 不写原文 | 极高 | 仍会被本地进程和配置的模型链处理；原文 opt-in 可开启 |
| 用户互动历史 | 原话、前台回复、route | Runtime | 内存；默认 snapshot 只存结构事件，opt-in 才存文本引用 | 极高 | 无 TTL、用户删除 API 或访问审计 |
| 正式研究状态 | field、score、confidence、evidence | Assessor + Ledger | episode JSON；evidence 引文默认移除 | 极高 | 结构化 score 仍易被误当临床结论；缺访问控制 |
| 候选状态 | candidate evidence、contextual memory、partial plan | Observer/Blackboard | 内存；snapshot 默认只存非自由文本元数据 | 极高 | 运行时候选与原话仍可能包含额外生活信息 |
| 模型输入 | 当前字段、近期轮次、原话、候选上下文 | Runtime | 发送到配置的远端 LLM endpoint | 极高 | 供应商条款、地区、保留和训练用途条件尚未核验 |
| 模型输出 | route、observer interpretation、decision | 远端/本地模型 | 内存、episode 结构投影；`raw_payload` 不持久化 | 高 | 结构结果仍可能包含不准确推断 |
| 运行遥测 | 时间戳、模型名、状态、错误、延迟 | Runtime | 日志和 episode | 中至高 | 可能与 episode/user ID 关联；未做分级保留 |
| 标识符 | user_id、episode_id、experiment_id、session_id | 配置/Runtime | 日志和 episode 的不同部分 | 高 | 默认 demo ID 不等于匿名；缺 pseudonymization 规范 |
| API 密钥与配置 | DeepSeek/ASR/TTS key、base URL | 环境或本地配置 | 进程内存/本地配置 | Secret | 开发式配置，不满足生产 secret governance |
| SCID 内容 | 题目、qid、目标字段、未来 rubric | 本地模板/授权材料 | 仓库或受控资料库 | 受许可约束 | 来源、许可、改编和分发清单未完成 |

## 当前实际数据流

```text
用户语音
  -> 浏览器 / X-Talk 语音链路
  -> 配置的 ASR 服务：音频 -> 文本
  -> SCID Runtime
       -> 本地 SafetyGuard / Control
       -> 配置的 Observer/Assessor LLM：原话 + 结构化上下文
       -> 配置的 Frontend LM：安全 directive + 有限上下文
       -> 本地 episode / partial JSON
  -> 配置的 TTS 服务：用户可见文本 -> 音频
  -> 用户
```

具体 ASR、Frontend LM 和 TTS 供应商由运行 profile 决定；不能用“X-Talk”代替供应商披露。每个研究/部署必须生成精确 provider manifest。

### Episode schema v3 投影

- `scid_persist_raw_transcript=false` 是严格布尔默认值。默认 snapshot 不写用户原话、evidence 引文、上下文记忆、模型 `raw_payload` 和用户派生自由文本；`text_artifacts` 与 `transcript` 为空。
- 显式开启原文模式后，每段唯一文本只在顶层 `text_artifacts` 保存一次；有序 transcript 和嵌套记录使用 `text_ref` 或 `*_ref/*_refs`。
- partial/final 写入使用同目录临时文件、`flush`、`fsync` 和 `os.replace`。目录权限为 `0700`，文件权限为 `0600`；final 成功后清理同 episode partial。
- 这个投影不是加密，不是用户同意，也不改变配置的远端服务在请求期间会处理必要文本的事实。

## 用途限制

当前允许的数据用途：确定性单元测试、离线回放、错误分析、延迟测量和满足既定数据条件的研究评测。

未经新同意和审查，禁止：

- 用会话原文训练、微调、蒸馏或做 RL；
- 广告、画像、保险、就业、教育、司法或信用用途；
- 将内部 score 作为临床诊断、分诊或治疗依据；
- 与身份、账户或第三方数据集重新关联；
- 向未列明供应商传输；
- 将原始对话直接放入普通开发日志、Issue、聊天工具或公开 benchmark。

## 最小化与分离目标

- 音频、身份、对话、结构化状态和遥测使用不同数据域与访问策略；
- 普通 telemetry 默认只保留计数、时间、错误类别和不可逆关联标识，不保存原话；
- 远端模型只接收完成当前任务必需的最小上下文；
- Candidate cache 不持久化到长期产品档案；当前 episode snapshot 包含其结构元数据，默认移除候选话术文本；
- 内部 score 必须带 `research_state_non_diagnostic` 用途标签；
- 人工审查界面默认隐藏直接身份信息，并记录访问审计。

## 留存、删除和更正

当前实现：episode 与 `.partial.json` 已具备默认文本最小化、原子替换和本地私有权限，但没有 TTL、加密、身份化访问控制、删除 API、访问审计或 session resume。供应商真实数据条件也未核验。因此真实数据路径仍为 `NO-GO`。

进入真实数据范围前必须定义并实现：

| 对象 | 目标留存 | 删除触发 | 问题域 |
| --- | --- | --- | --- |
| 未完成 partial episode | 最短技术恢复窗口；若不支持恢复则会话后立即删除 | 会话结束、撤回、失败清理 | DATA / SYSTEM |
| 完整研究 episode | 协议明确的固定期限 | 到期、撤回适用、项目终止 | DATA |
| 原始音频 | 默认不保存；确需保存必须单独同意 | 处理完成或协议到期 | DATA / BOUNDARY |
| 供应商副本/日志 | 以合同中最短期限为准 | 合同/API 删除机制 | DATA / RELEASE |
| 聚合指标 | 证明无法合理重新识别后另定 | 项目政策 | DATA / EVAL |

删除必须覆盖主存储、partial、缓存、备份、导出、标注平台和供应商副本，并生成不含敏感原文的删除审计记录。无法删除的备份必须有隔离和自然过期说明。

## 同意最小内容

真实研究同意至少说明：

- 系统是非诊断研究工具，不替代专业人员；
- 会收集哪些音频、文本、结构化推断和技术数据；
- 数据目的、使用者、供应商、处理地区和保留期限；
- 退出、跳过、暂停、更正和删除方式；
- 哪些已聚合或法定数据可能无法撤回；
- 危机时系统能做什么、不能做什么，是否有人类实时监控；
- 研究联系人、隐私联系人和投诉渠道。

## 供应商准入

每个供应商必须记录：法人主体、服务、数据类别、地区、传输、保留、训练/二次使用、分包商、删除、加密、事件通知、退出方案和合同依据。任何一项未知时不得传输真实健康数据。
