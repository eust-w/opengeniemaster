# Genie Master 研究纪要

研究日期：2026-10-27，Asia/Shanghai。用途：为独立项目 `opengeniemaster` 确立事实边界和实现范围。
本文只记录公开资料，不代表智元官方解释、合作关系、兼容性认证或技术复现结果。

> **当日晚间更新**：初版纪要写于发布前，当时尚未取得发布内容。当晚，官方项目主页及其源码仓库已经可以访问，主页给出了 EEP 四类契约、架构图和实验数据，多家媒体也报道了发布会。下文按新材料修订；初版中已被新材料推翻的判断，均在原处注明。

## 1. 核心结论

- 研究对象确为智元 AGIBOT 的 **Genie Master（GenieMaster）**。它于 2026-10-27 作为 “AI WAVE” 系列的第 4 项发布，定位为具身 Agent 框架。[官方预告](https://www.agibot.com.cn/article/315/detail/250.html)、[界面新闻](https://www.jiemian.com/article/15247047.html)、[IT之家](https://www.ithome.com/1/018/423.htm)
- **第一方技术材料已公开到“设计层”**。项目主页 [genie-master.github.io](https://genie-master.github.io/)（源码仓库 `genie-master/genie-master.github.io`）的论文题为 *GenieMaster: An Embodied Continual Harness for Persistent Multi-Robot Work*，主页包含：
  - 架构图；
  - EEP 四类契约及部分字段名；
  - 持续 Harness 演进（临时记忆 → 反思 → 长期记忆）；
  - 6 个真实任务的实验表与 4 组实验图。
- **代码、权重、论文全文仍未公开**。主页的 “Paper PDF” 链接指向 arXiv 首页（占位），“Code” 标注为 “Coming soon”。主页 `main` 分支署名为 “Anonymous Authors · ICLR 2027 submission”；`dev` 分支署名为 “AgiBot Research / 2026.09.28”，技术内容更完整。
- EEP 的契约与字段是公开的，但线格式、SDK、错误语义和版本协商都没有公开。因此本仓库按公开描述实现了一个开放协议 `open-eep/0.1`，**不声称与官方实现兼容**。
- 性能与 “Physical RSI” 相关结论仍属官方主张。本次未取得可复现的实验材料，也没有独立验证。
- 官方其他 Genie 系列项目的许可证并不统一，不能因名称相近就当作 Master 的实现，也不能直接并入 MIT 代码库（见第 5 节）。
- 本仓库的定位：依据公开设计做的**独立开源复刻**，在语义仿真中可以运行和评测，配有面向真机的执行服务契约。它不是官方开源版，不包含策略训练，没有经过真机验证，也不宣称“已实现 Physical RSI”。

## 2. 名称辨析

| 名称 | 可核实的定位 | 与本研究目标的边界 |
| --- | --- | --- |
| Genie Master | 具身 Agent 框架（Embodied Continual Harness），10/27 发布 | 本次研究对象。[项目主页](https://genie-master.github.io/) |
| Genie Studio | 面向数据采集、模型训练、仿真评测与部署的开发平台 | 平台不等于 Master，不能据此推定 Master 的内部组件。[产品页](https://genie.agibot.com/geniestudio) |
| Genie Studio Agent | 官方介绍的零代码任务编排、仿真验证、运行监控平台 | 名称中有 Agent，但不能据此与 Master 合并。[官方介绍](https://agibot.com/article/231/detail/59.html) |
| Genie Sim | 环境重建、场景生成、数据采集和自动评测工具链 | 物理仿真与评测项目，不是 Master 代码。[仓库](https://github.com/AgibotTech/genie_sim) |
| Genie Envisioner | 将策略学习、评测与仿真结合的视频生成式世界模型体系 | 论文包含 GE-Base、GE-Act、GE-Sim；其中 GE-Sim 不等于 Genie Sim。[作者论文](https://arxiv.org/abs/2508.05635) |
| GE-Sim 2.0 | Genie Envisioner 系列的闭环视频世界模拟器 | 有独立代码和文档，不能代替 Master 或其执行协议。[仓库](https://github.com/AgibotTech/GE-Sim-V2) |

检索中还出现了几篇名称相近、但与智元无关的工作：*Continual Harness*（arXiv 2605.09998，Pokémon 环境中的自我改进 Harness）、*RobotUse*（arXiv 2610.04929）、*EmbodiedRSI*（arXiv 2610.10498），以及 GitHub 上的 `RSI-Harness`、`harness-rsi`。它们只能作为相关工作参考，不能当作 GenieMaster 的资料。

## 3. 证据分级

“已确认”只表示资料或制品存在；“官方主张”不等于独立验证；“未知”不表示不存在。

| 议题 | 状态 | 证据及可下结论 |
| --- | --- | --- |
| 产品身份及发布 | 已确认 | 官网预告明确了 10/27 排期，当日有多家媒体报道发布。[预告](https://www.agibot.com.cn/article/315/detail/250.html)、[界面新闻](https://www.jiemian.com/article/15247047.html)、[投资界](https://news.pedaily.cn/202610/570923.shtml) |
| 项目主页与作者归属 | 已确认（作者匿名） | `main` 分支为匿名 ICLR 2027 投稿版；`dev` 分支标注 “AgiBot Research”。论文 PDF 未公开（链接为占位）。 |
| EEP 四类契约及字段 | 已确认到契约层 | Capability / Guidance / Feedback / Continuity；公开字段：`observation`、`action_result`、`failure_reason`；`task_id`、`completed_steps`、`assigned_robot`、`pending_work`。主页说明 EEP “受 MCP 工具抽象启发”。线格式未公开。 |
| Harness RSI 与 Policy RSI | 已确认到概念层（媒体转述官方发布） | Harness RSI：模型权重和工具接口不变，通过 Prompt、Memory、执行规则改进，每日异步反思。Policy RSI：把自主执行、人工接管、异常恢复的数据经筛选、加权、目标重标后，通过监督 / 模仿 / 强化学习更新动作策略参数。[量子位（网易转载）](https://www.163.com/dy/article/LA8SUILJ0511DSSR.html) |
| 6 个真实任务的端到端结果 | 官方主张 | 主页实验表（每方法每任务 100 次）：SR / TC 平均值为 vanilla VLA 18.83 / 55.68、双系统 38.83 / 61.31、GenieMaster 78.67 / 89.71、人工编排 81.00 / 91.17。未附可复现材料。 |
| 真实业务中的 Physical RSI | 官方主张 | 发布称 G2 在便利店“无人在环”中自主诊断失败、调整策略、沉淀经验；缺少完整干预日志与可复现评测。 |
| 便利店连续 24 小时演示 | 已确认是未来计划 | 11/2 起直播，6 台 G2 四班倒。[新浪财经](https://finance.sina.com.cn/jjxw/2026-10-27/doc-iniwrzht4610342.shtml)；截至本文日期尚未发生。 |
| Master 公开代码、权重、许可证 | 未发布 | 主页标注 “Coming soon”；AgibotTech 组织下按更新时间排序的仓库中，未见 Master、EEP 或 harness 相关仓库。 |

## 4. 公开的架构

### 4.1 架构图（FIG. 01，`assets/geniemaster-architecture.png`）

- **External Interfaces**：Bounding Boxes、Speech、Merchant Database、Goal Image。
- **GenieMaster**：
  - **Domain**：Task Semantics、Composed Commands、Materials & Business Records → Task Criteria。
  - **Embodiment**：Robot Instance、Robot Bindings、Calibration。
  - **Shared Admission**：Validate · admit。
  - **Memory**：Domain Memory、Embodiment Memory。
  - **Coding Agent**（图标为 CC、Codex、Kimi K3、DeepSeek Harness）：Task Context（domain goal & limits）→ Orchestrate → Execution Feedback（criteria & evidence；成功回到 Orchestrate，失败进入 Recovery）→ Reflection（reflect & revise）→ Memory。
- **Agent—Robot 双向通信**：
  - Execution Requests：Language、Affordance、Action、Primitives；
  - Evidence：Execution、Success、Progress、Observation。
- **Robots**：
  - Execution Services：Policy Inference、Robot Control；
  - Execution Adapters；
  - 支持异构机器人：G1、G2、ARX；
  - Replay · Offline tests。

### 4.2 概览图（FIG. 02）

- 架构演进分三代：Action Policy（端到端）→ Dual-System（VLM 规划 + Action Policy）→ GenieMaster（Harness = Domain + Embodiment + Coding Agent，经 EEP 连接 Action Policy、WBC Control、Motion Primitives）。
- 四项能力：
  - Capability & Continuity：机器人能力、任务分配、进度与依赖、中断与恢复；
  - Bidirectional Agent–Policy Interface：Subtask → Guidance → Policy；Observation ← Success / error evidence；
  - Co-Designed Policy Guidance：接口 + 标注 + 策略训练；Spatial、Language、Object reference；
  - Continual Harness Adaptation：模型参数固定，临时记忆 ↔ 长期记忆，经反思更新。

### 4.3 EEP 契约（主页 `dev` 分支 “EEP contracts” 一节）

- **Capability**：领域工具组合出导航、抓取、放置等工作流（示例 `inspect_shelf` + `restock`）；具身工具把它们绑定到选定的机器人、标定和执行服务（示例 `robot=G1` + `calibration` + `execute`）。同一个领域请求（“补上缺的那瓶”）可以由 G1、G2 或 ARX 各自的实现完成。
- **Guidance**：在当前观测和子任务指令之外，提供空间提示（区域、框、点）、视觉目标条件（参考图）和细粒度语言（例如“握瓶颈，标签朝外，放前排”）。
- **Feedback**：抓取失败时返回最新帧、尝试过的动作和失败原因；Agent 可以换一个点重试、调用另一台机器人，或推迟到之后恢复。字段：`observation` + `action_result` + `failure_reason`。
- **Continuity**：机器人取物后停下时，下一次调用拿到任务状态、已完成步骤、手持物和未满足依赖，其他空闲机器人可以不重启计划直接接手。字段：`task_id` + `completed_steps` + `assigned_robot` + `pending_work`。

### 4.4 持续 Harness 演进

- **更新的对象**：引导、判断标准、恢复流程、协作规则，以可复用的长期说明形式保存。
- **不变的对象**：模型参数（部署的策略权重）。
- **流程**：临时记忆（执行记录）→ 反思（复盘与整合）→ 长期记忆（可复用说明）→ 次日规划。

主页给出的办公用品案例：反思前，长期记忆是三条通用规则。临时记忆里记录了两次失败：把桌边的相似物误当成耳机盒（耳机盒其实在吧台）；回撤时夹爪张开，物体在交付前掉落。反思后新增：

- 近距离核验边缘、被遮挡或不清楚的目标；
- 耳机盒是银白色圆形，附近杂物不能替代；
- 有人或手进入工作区时暂停，复查后再继续；
- 回撤和行进时保持夹爪闭合，到位才释放。

### 4.5 实验（主页 `experiments.js` 与图表；均为官方真机结果）

| 任务 | Vanilla VLA | Dual-system | GenieMaster | 人工编排 |
| --- | --- | --- | --- | --- |
| 平均（6 项） | 18.83 / 55.68 | 38.83 / 61.31 | 78.67 / 89.71 | 81.00 / 91.17 |
| 招待客人 | 0.00 / 48.20 | 18.00 / 68.20 | 45.00 / 84.54 | 50.00 / 86.60 |
| 铺桌布 | 15.00 / 58.33 | 25.00 / 52.00 | 60.00 / 76.75 | 55.00 / 76.45 |
| 办公用品搜寻 | 0.00 / 18.05 | 32.00 / 21.05 | 84.00 / 89.80 | 95.00 / 92.11 |
| 取货 | 20.00 / 81.16 | 40.00 / 80.55 | 93.00 / 96.67 | 95.00 / 98.74 |
| 补货 | 53.00 / 62.50 | 58.00 / 67.76 | 95.00 / 96.05 | 96.00 / 98.33 |
| 清洁 | 25.00 / 65.83 | 60.00 / 78.30 | 95.00 / 94.44 | 95.00 / 94.79 |

（单元格为 SR / TC，%；每方法每任务 100 次，随机布局。）

- **能力组合（固定位置取货，每配置 100 次）**：C1 机械臂 IK 25%，C2 + 抓取规划 40%，C3 + 引导训练的策略 82.5%（`experiments.js` 中记为 83），C4 + 物体参考图 94%，C5 + 头部 / 腰部控制 97%。
- **新品泛化（每格 25 次，无导航）**：
  - 训练 SKU 为 10 / 30 / 1,000 时，纯文本策略对新品的成功率为 0 / 12 / 20%，EEP 引导（参考图）为 12 / 56 / 92%；
  - 1,000 SKU 策略：已见商品 72% vs 96%，新品 20% vs 92%。
- **从经验中学习（办公用品搜寻，每轮 7 次）**：
  - 成功率 28.6 → 57.1 → 71.4 → 42.9 → 85.7%；
  - 成功任务的平均工具调用次数为 54.5、62.5、60.8、60.0、65.0，大致不变；
  - 反思让 7 个检测器中的 6 个完成边界更准确。
- **持续部署**：便利店 24 小时部署窗口；定时巡检触发补货，顾客订单触发取货，派给空闲且有能力的机器人。

仍未公开、会决定实现方式的内容：EEP 的消息封装、传输、版本协商与一致性测试；各组件的模型与运行时；Policy RSI 的触发条件与审核流程；G2 的接口权限、控制频率与安全约束。

## 5. 公开资源与复用边界

| 官方项目 | 实际可见内容 | 许可证证据与使用建议 |
| --- | --- | --- |
| [Genie Sim](https://github.com/AgibotTech/genie_sim) | 源码、README、文档和评测入口 | 根许可证对 `source/geniesim`、`source/data_collection` 指定 MPL-2.0，场景重建部分为多许可证；建议先作为独立可选工具评估。[许可证](https://github.com/AgibotTech/genie_sim/blob/6ca11c7593ecf6b7dae58c28fd19f4a789a0dd46/LICENSE) |
| [Genie Envisioner V1](https://github.com/AgibotTech/Genie-Envisioner-V1) | 论文对应代码、训练 / 推理说明、权重链接 | README 将部分上游衍生目录列为 Apache-2.0，其余代码和数据列为 CC BY-NC-SA 4.0；不应概括为无限制开源。[许可说明](https://github.com/AgibotTech/Genie-Envisioner-V1#license) |
| [GE-Sim-V2](https://github.com/AgibotTech/GE-Sim-V2) | 视频世界模拟器、策略客户端、示例与文档 | 根文件为 Apache-2.0，但 README 对其余代码 / 数据列出 CC BY-NC-SA 4.0；范围存在不一致，需维护者澄清。[LICENSE](https://github.com/AgibotTech/GE-Sim-V2/blob/508c60f1a737a54ad2aaa974e63489c7f0c1b1a2/LICENSE)、[README](https://github.com/AgibotTech/GE-Sim-V2/blob/508c60f1a737a54ad2aaa974e63489c7f0c1b1a2/README.md#license) |
| [genie_sim_robot_model](https://github.com/AgibotTech/genie_sim_robot_model) | URDF / xacro、网格、USD 与处理工具 | 智元自有内容标注 Apache-2.0，捆绑的第三方机器人资源保留各自条款；不能只看仓库许可证徽章。[许可说明](https://github.com/AgibotTech/genie_sim_robot_model#license) |
| [agibot_agentsdk_sample_code](https://github.com/AgibotTech/agibot_agentsdk_sample_code) | 基于灵心 AgentSDK 的医院导诊示例 | 示例为 MIT，README 说明依赖官方 SDK 安装包和平台配置；不是 Master SDK，也不证明底层 SDK 同为 MIT。[说明](https://github.com/AgibotTech/agibot_agentsdk_sample_code#readme) |

GE-Sim-V2 的 README 还说明奖励模型未随包提供；已有闭环示例不等于具备自主策略学习闭环。[官方说明](https://github.com/AgibotTech/GE-Sim-V2#quickstart)

本仓库遵守的边界：

- MIT 只用于本项目有权授权的原创内容，不给第三方代码、权重或数据重新贴 MIT 标签。
- 本仓库**没有**复制主页的图片、视频、页面代码或任何官方仓库的代码；图表数字仅在本文中引用并注明来源。
- 后续引入依赖时，逐项登记版本、来源、许可证、资源类型与运行条件。
- 官网产品可访问、仓库公开、代码可再分发、权重可商用是不同问题，需分别核对。

## 6. 本仓库的实现状态

初版纪要提出了 A–D 四个阶段，当前进展如下（细节见 [docs/architecture.md](../architecture.md)）：

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| A 可审计的任务执行骨架 | 已实现 | 事件日志与逐请求回放（`replay.py`）；仿真、HTTP、回放三种执行适配器；最大尝试次数、任务取消、技能白名单（机器人技能 + 准入）、急停（`eep_robot_stop`）。HTTP 适配器有请求超时，步骤级时间预算尚未实现。 |
| B 经验检索与固定基线比较 | 已在仿真中实现 | 权重不变的记忆闭环：执行记录 → 反思 → 准入 → 长期记忆，可版本化和回滚。在相同种子上与关闭记忆的对照组比较，原始记录保留在 `results/`。 |
| C 单一公开后端集成 | 部分完成 | 自研语义仿真器（零依赖）可运行全部实验；尚未接入 Genie Sim、Isaac 等物理后端（执行适配器接口已预留）。 |
| D 受监督真机试验 | 未开始 | 提供了机器人端 HTTP 契约和人在回路后端（`examples/human_backend.py`），可在急停、人工接管条件下试跑；尚无真机结果。 |

关于表述：本仓库使用“独立开源复刻 / 开放实现”，不使用“官方开源版”或“已实现 Physical RSI”。评测数字一律注明来自仿真。

## 7. 方法、时间与待补证据

- 检索日期：2026-10-27（Asia/Shanghai）；结论限定于本次可访问的公开资料。
- 第一方材料：
  - AGIBOT 中英文官网预告；
  - 项目主页源码仓库 `genie-master/genie-master.github.io`：`main` 分支提交 `df3867a58ca2a998d1f28b88096ca69b165d6988`（2026-09-26，“Create GenieMaster project page”），`dev` 分支提交 `d4300d451f6d07bc07f753c0a3c62923fdf87c06`；
  - AgibotTech 公开仓库及其 README / LICENSE。
- 读取方式：通过只读 GitHub API 和原始文件读取 `index.html`、`experiments.js`，并直接查看 6 张图（架构图、概览图、引导模态、能力组合、新品泛化、记忆轮次）。主页上的视频未观看；论文 PDF 尚未公开，未读到。
- 媒体材料只用来补充发布会口径（Harness RSI / Policy RSI、24 小时直播计划），对应条目已标注为媒体转述：量子位（网易转载）、IT之家、界面新闻、新浪财经、投资界、凤凰网。
- 关键词：`Genie Master`、`GenieMaster`、`EEP`、`Embodied Continual Harness`、`Harness RSI`、`Policy RSI`。
- 未下载权重、未运行官方模型、未测试真机；未证实任何 Master 性能、无人工干预能力或跨场景泛化结果。

后续优先补证：论文全文与作者归属、官方代码与 EEP 线格式、11/2 起 24 小时直播的完整记录与干预日志、可复现评测。
