# Genie Master 研究纪要

研究日期：2026-10-27，Asia/Shanghai。用途：为独立项目 `opengeniemaster` 确立事实边界和起步范围。
本文只记录公开资料，不代表智元官方解释、合作关系、兼容性认证或技术复现结果。

## 1. 核心结论

- 目标确为智元 AGIBOT 的 **Genie Master**，不是名称相近的仿真平台或世界模型。[官方预告](https://www.agibot.com.cn/article/315/detail/250.html)
- 10 月 22 日预告将其安排在 10 月 27 日，定位为连接 Agent 与物理世界、探索 Physical RSI 的具身框架；预告不能单独证明当天发布已完成。[官方预告](https://www.agibot.com.cn/article/315/detail/250.html)
- 本次未取得当天官方发布全文、专属代码仓库、技术论文或 EEP 协议规范；因此不将媒体中的架构细节认定为已公开技术事实。
- 已找到相关官方公开项目，但许可证并不统一。不能因为名称同属 Genie，就把它们解释为 Genie Master 的实现或直接并入 MIT 代码库。
- 建议本仓库明确定位为独立研究与原型项目，不使用“官方开源版”“完整复刻”“已实现 Physical RSI”等表述。

## 2. 名称辨析

| 名称 | 可核实的定位 | 与本研究目标的边界 |
| --- | --- | --- |
| Genie Master | 官方预告中的具身 Agent 框架 | 本次研究对象；详细实现待官方材料补全。[预告](https://www.agibot.com.cn/article/315/detail/250.html) |
| Genie Studio | 面向数据采集、模型训练、仿真评测与部署的开发平台 | 平台不等于 Master；不能据此推定 Master 的内部组件。[产品页](https://genie.agibot.com/geniestudio) |
| Genie Studio Agent | 官方介绍的零代码任务编排、仿真验证、运行监控平台 | 名称中有 Agent，但不能据此与 Master 合并。[官方介绍](https://agibot.com/article/231/detail/59.html) |
| Genie Sim | 环境重建、场景生成、数据采集和自动评测工具链 | 物理仿真与评测项目，不是 Master 代码。[仓库](https://github.com/AgibotTech/genie_sim) |
| Genie Envisioner | 将策略学习、评测与仿真结合的视频生成式世界模型体系 | 论文包含 GE-Base、GE-Act、GE-Sim；其中 GE-Sim 不等于 Genie Sim。[作者论文](https://arxiv.org/abs/2508.05635) |
| GE-Sim 2.0 | Genie Envisioner 系列的闭环视频世界模拟器 | 有独立代码和文档，不能代替 Master 或其执行协议。[仓库](https://github.com/AgibotTech/GE-Sim-V2) |

## 3. 证据分级

“已确认”只表示资料或制品存在；“官方主张”不等于独立验证；“未知”不表示不存在。

| 议题 | 状态 | 证据及可下结论 |
| --- | --- | --- |
| 产品身份及发布排期 | 已确认 | 官网预告明确出现名称及 10/27 排期。[来源](https://www.agibot.com.cn/article/315/detail/250.html) |
| 真实业务中的 Physical RSI | 官方主张 | 预告提出这一目标/成果主张，但未附本次可复现实验材料。[来源](https://www.agibot.com.cn/article/315/detail/250.html) |
| 10/27 正式发布的完整内容 | 未核验 | 已检查的中英文官网新闻入口中，未取得当日 Master 发布正文。[中文新闻](https://www.agibot.com.cn/News/Company)、[英文新闻](https://agibot.com/news) |
| EEP、Harness RSI、Policy RSI | 未核验 | 这些是调查线索中的术语；本次未取得对应官方协议、架构说明或作者论文，不能据此制定兼容接口。 |
| G2 已完成熟食打包、取货补货及自主改进 | 未核验 | 未取得与 Master 对应的第一方实验日志、完整任务记录或可复现评测；不将媒体叙述升级为实测结论。 |
| 便利店连续 24 小时演示 | 已确认是未来计划 | 官网安排在 11/2 至 11/3，涉及 6 台 G2；截至本报告日期，不是已完成验证。[来源](https://www.agibot.com.cn/article/315/detail/250.html) |
| Master 公开代码、权重、许可证 | 未定位到 | 本次官方组织公开仓库枚举未发现明确以 Master 命名的实现；不是“闭源”判定。[官方组织](https://github.com/AgibotTech) |

## 4. 架构：目前能说到哪里

第一方材料支持的是产品级方向，而不是可实施的 Master 技术规范。
官网将其放在数据、平台、本体、仿真和模型组成的整体技术体系中。[官方预告](https://www.agibot.com.cn/article/315/detail/250.html)

目前仍缺少以下决定实现方式的资料：

- EEP 消息结构、传输方式、版本协商、错误语义和一致性测试。
- 决策、执行、结果判断各组件的模型边界、运行时依赖和调用关系。
- Harness RSI 与 Policy RSI 的官方定义、触发条件、更新对象和更新审核流程。
- 记忆组织、数据筛选、奖励来源、离线训练与线上部署之间的具体关系。
- G2 接口权限、控制频率、安全约束、故障接管与回滚要求。

因此，本文不绘制“官方架构图”，不虚构 SDK、API 路由、JSON 字段或下载地址。
与“从失败积累经验”有关的独立设计可以研究，但必须与上述未知项分开。

## 5. 公开资源与复用边界

| 官方项目 | 实际可见内容 | 许可证证据与使用建议 |
| --- | --- | --- |
| [Genie Sim](https://github.com/AgibotTech/genie_sim) | 源码、README、文档和评测入口 | 根许可证对 `source/geniesim`、`source/data_collection` 指定 MPL-2.0，场景重建部分为多许可证；建议先作为独立可选工具评估。[许可证](https://github.com/AgibotTech/genie_sim/blob/6ca11c7593ecf6b7dae58c28fd19f4a789a0dd46/LICENSE) |
| [Genie Envisioner V1](https://github.com/AgibotTech/Genie-Envisioner-V1) | 论文对应代码、训练/推理说明、权重链接 | README 将部分上游衍生目录列为 Apache-2.0，其余代码和数据列为 CC BY-NC-SA 4.0；不应概括为无限制开源。[许可说明](https://github.com/AgibotTech/Genie-Envisioner-V1#license) |
| [GE-Sim-V2](https://github.com/AgibotTech/GE-Sim-V2) | 视频世界模拟器、策略客户端、示例与文档 | 根文件为 Apache-2.0，但 README 对其余代码/数据列出 CC BY-NC-SA 4.0；范围存在不一致，需维护者澄清。[LICENSE](https://github.com/AgibotTech/GE-Sim-V2/blob/508c60f1a737a54ad2aaa974e63489c7f0c1b1a2/LICENSE)、[README](https://github.com/AgibotTech/GE-Sim-V2/blob/508c60f1a737a54ad2aaa974e63489c7f0c1b1a2/README.md#license) |
| [genie_sim_robot_model](https://github.com/AgibotTech/genie_sim_robot_model) | URDF/xacro、网格、USD 与处理工具 | 智元自有内容标注 Apache-2.0，捆绑第三方机器人资源保留各自条款；不能仅看仓库许可证徽章。[许可说明](https://github.com/AgibotTech/genie_sim_robot_model#license) |
| [agibot_agentsdk_sample_code](https://github.com/AgibotTech/agibot_agentsdk_sample_code) | 基于灵心 AgentSDK 的医院导诊示例 | 示例为 MIT，README 说明依赖官方 SDK 安装包和平台配置；不是 Master SDK，也不证明底层 SDK 同为 MIT。[说明](https://github.com/AgibotTech/agibot_agentsdk_sample_code#readme) |

GE-Sim-V2 的 README 还说明奖励模型未随包提供；已有闭环示例不等于具备自主策略学习闭环。[官方说明](https://github.com/AgibotTech/GE-Sim-V2#quickstart)

本仓库的保守边界建议：

- MIT 仅用于本项目有权授权的原创内容，不给第三方代码、权重或数据重新贴 MIT 标签。
- 暂以文档链接和隔离适配方案为主，不搬运许可证不清晰的代码、视频、模型或数据。
- 后续引入依赖时逐项登记版本、来源、许可证、资源类型与运行条件。
- 官网产品可访问、仓库公开、代码可再分发、权重可商用是不同问题，分别核对。

## 6. 独立 MVP 建议，不是官方设计

以下阶段是 `opengeniemaster` 的提案，本次没有实现或验证其中任何能力。

### 阶段 A：可审计的任务执行骨架

- 只选择一个受限操作任务，将任务、观测、动作结果和失败原因记录为可回放事件。
- 以执行器适配层隔离机器人/仿真后端，接口由本项目独立定义，不命名为 EEP 兼容实现。
- 明确超时、取消、最大重试次数、能力白名单和人工停止入口。
- 验收：可重放固定输入、复现失败路径；没有真实后端时明确标为接口测试。

### 阶段 B：经验检索与固定基线比较

- 先冻结策略参数，只研究经审核的失败经验能否帮助同类任务重试。
- 对照关闭记忆的基线，隔离训练/调试任务与评测任务，记录版本和干预次数。
- 验收：保留成功率、任务耗时、重试次数及失败案例的原始记录；允许结果无提升。
- 不把提示词变化、一次成功演示或回放测试命名为 Physical RSI 复现。

### 阶段 C：单一公开后端集成

- 在许可及运行资源审查后，选择一个现成仿真/评测后端，不在初版重建整套 Studio。
- 先验证一个端到端任务，固定依赖版本；仿真证据和真机证据分开报告。
- 需要策略训练时先采用离线候选更新，经独立评测后人工批准部署，不默认线上自修改。

### 阶段 D：受监督真机试验

- 仅在取得设备、授权接口、安全措施和现场监督条件后启动。
- 保留急停、人工接管、动作范围限制和失败回滚；不以“无人在环”作为初版目标。
- 验收依据为完整执行记录及事先定义的任务标准，不用宣传视频替代测试。

## 7. 方法、时间与待补证据

- 检索日期：2026-10-27（Asia/Shanghai）；结论限定于本次可访问的公开资料。
- 第一方范围：AGIBOT 中英文官网、Genie 产品页、AgibotTech 公开仓库及其 README/LICENSE、作者提交的 arXiv 论文。
- 使用关键词：`Genie Master`、`GenieMaster`、`EEP`、`Harness RSI`、`Policy RSI`，结合官网、GitHub、论文及官方账号域名筛选。
- GitHub 公开组织 API 当次返回 26 个仓库；未发现明确命名的 Master 仓库。[只读 API](https://api.github.com/orgs/AgibotTech/repos?type=public&per_page=100)
- Genie Sim 许可证固定到 `6ca11c7593ecf6b7dae58c28fd19f4a789a0dd46`；GE-Sim-V2 固定到 `508c60f1a737a54ad2aaa974e63489c7f0c1b1a2`，并经只读 GitHub API 核对。
- Genie Studio 产品页可读取到搜索索引正文，但直接抓取超时；没有登录平台验证功能。部分官网正文也未成功抓取。
- 官方社交账号检索未取得 Master 当天可核验正文；没有把访问受限、搜索无结果或索引延迟解释为“未发布”。
- 检索中发现的第三方媒体线索仅用于发现关键词，不作为本文技术事实证据；全文不引用其性能数字或复制宣传文字。
- 官网预告发表于 2026-10-22；Genie Envisioner 论文初版发表于 2025-08-07。其余仓库条款按本次访问快照记录，不保证后续不变。
- 未下载权重、运行模型、复现实验或测试真机；未证实任何 Master 性能、无人工干预能力或跨场景泛化结果。

后续优先补证：Master 正式发布原文及作者归属、EEP 规范与测试样例、代码/模型授权、可复现评测和完整干预日志。
在这些材料出现前，保持“独立研究提案”的定位比宣称兼容或复现更准确。
