# Open EEP 0.1：具身执行协议（开放实现规范）

协议标识：`open-eep/0.1`。

GenieMaster 主页把 EEP 描述为“受 MCP 工具抽象启发”的协议，由四类契约组成。官方公开了字段名，但没有公开线格式。本规范把公开字段原样保留，其余部分（封装、默认值、错误码、版本）由本项目补全，**不代表智元官方协议**。

机器可读的 JSON Schema 可以导出：

```bash
ogm schema --out schemas/
```

## 1. 传输与约定

| 层 | 方式 |
| --- | --- |
| Agent ↔ Harness | MCP（JSON-RPC 2.0，stdio，按行分隔）；支持协议版本 `2025-11-25`、`2025-06-18`、`2025-03-26`、`2024-11-05` |
| Harness ↔ 机器人执行服务 | HTTP/JSON，路径前缀 `/eep/v0/`；可选 `Authorization: Bearer <token>` |

通用约定：

- 值为 `null` 的字段在序列化时省略；接收方必须忽略未知字段。显式传入的 `null` 等同于未提供。
- 坐标一律为归一化图像坐标 `[0, 1]`，`frame` 指明所属相机。
- 时间单位为秒，取执行服务的时钟。

## 2. Capability 契约

`eep_capabilities` 返回 `CapabilityContract`：

```text
protocol            "open-eep/0.1"
domains[]           name, description, vocabulary[], safety[], workflows[], tools[]
  workflows[]       name, domain, description, params(JSON Schema)
  tools[]           name, domain, description, params(JSON Schema)      ← 组合工具，例如 store.inspect_shelf
robots[]            robot_id, model, features[], skills[], status, location, holding, calibrated, adapter, policy{}
  skills[]          name, service(policy|control|primitive|perception), mode, params(JSON Schema), guidance[]
memory              records, pending_reflection, long_term_version, long_term_entries, ...
recoveries          {name: description}
```

机器人特性（`features`）包括：`mobile`、`dual_arm`、`head_camera`、`wrist_camera`、`head_waist`、`arm_ik`、`grasp_planning`、`guided_policy`、`object_reference`。

`policy` 为策略卡片：名称、训练规模、训练过的 SKU 列表。编码 Agent 据此判断某商品对该策略来说是否属于新品。

## 3. Guidance 契约

`Guidance` 随执行请求下发：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `instruction` | string | 子任务指令 |
| `detailed` | string | 细粒度语言：顺序、手臂分工、接近方向、接触约束 |
| `constraints` | string[] | 显式约束，例如 “Keep the gripper closed during retraction and travel” |
| `spatial[]` | SpatialCue | `kind`（box / point / region / trajectory）、`coords`、`frame`、`role`（target / placement / waypoint / avoid）、`label` |
| `references[]` | ObjectReference | `ref_id`、`image`（URI）、`description`、`attributes`、`source` |
| `camera` | string | 感知相机偏好：`head` / `wrist` |
| `arm`、`approach` | string | 手臂、接近方向 |
| `options` | object | 机读执行选项：`keep_gripper_closed`、`pause_on_intrusion`、`verify_close_range` 等 |

协同设计约束：只有具备 `guided_policy` 特性的策略才使用空间提示和细粒度语言，只有具备 `object_reference` 特性的策略才使用参考图。其他策略会忽略这些引导，引导构建器也不会为它们添加。

## 4. 执行请求

`ExecutionRequest`（工具 `eep_execute`；HTTP `POST /eep/v0/execute`）：

```json
{
  "robot_id": "cart-01",
  "skill": "hang",
  "args": {"sku": "cable-usbc", "location": "hook_wall", "slot": "hook-3"},
  "guidance": {
    "instruction": "Hang the USB-C cable at hook_wall",
    "camera": "wrist",
    "spatial": [{"kind": "point", "coords": [0.833, 0.167], "frame": "head", "role": "placement", "label": "hook-3"}],
    "options": {"keep_gripper_closed": true}
  },
  "mode": "language",
  "task_id": "task-3ceb-0001",
  "step_id": "i1-hang",
  "memory": ["mem-1a2b3c4d"],
  "recovery": "use_wrist_camera"
}
```

`mode` 有四个取值，对应架构图中的四种执行请求：`language`（策略推理，VLA）、`affordance`（抓取点、放置点、框）、`action`（机器人控制，例如 IK 目标）、`primitive`（运动原语或全身行为）。省略时，取机器人技能绑定的默认值。

步骤参数可以引用前序步骤的结果，例如 `"location": "@f1-search.location"`，Harness 会在执行时解析。

## 5. Feedback 契约

`Feedback` 的前三个字段是公开字段，其余为扩展：

| 字段 | 含义 |
| --- | --- |
| `observation` | 动作结束时的观测：`detections[]`（label、ref_id、bbox、confidence、camera、track_id）、`robot_state`（location、holding、gripper、held_label、clock_s）、`flags`（如 `person_in_workspace`） |
| `action_result` | `skill`、`mode`、`outcome`（success / failure / partial / rejected / error / cancelled）、`duration_s`、`details` |
| `failure_reason` | `code`、`message`、`evidence`（例如 `{"camera": "head", "detection_confidence": 0.31, "occluder": "price_tag"}`）、`recoverable` |
| `success` | Harness 的最终判定：**由完成判据决定，而不是策略自报** |
| `policy_success` | 策略服务自报的结果 |
| `criteria[]` | 判据结果：`name`、`passed`、`confidence`、`evidence`（含 `close_range`） |
| `progress` | 本步骤进度，0–1 |
| `suggestions[]` | 恢复建议，来自恢复预案和长期记忆中的恢复顺序 |
| `continuity` | 执行后的任务连续性快照 |

失败码见 `opengeniemaster/eep/errors.py`。主要有 `target_not_found`、`target_not_localized`、`wrong_object`、`grasp_failed`、`dropped`、`not_holding`、`hand_occupied`、`misplaced`、`unreachable`、`workspace_intrusion`、`service_unavailable`、`navigation_blocked`、`criteria_failed`、`admission_rejected`、`precondition_failed`、`incomplete`。

策略报告成功而判据未通过时，失败码按判据映射：`holding_target` → `wrong_object`；`on_slot`、`on_hook`、`in_bin` → `misplaced`；`delivered` → `wrong_object`；`surface_clean`、`cloth_coverage` → `incomplete`。

## 6. Continuity 契约

`ContinuityState`：

```json
{
  "task_id": "task-3ceb-0001",
  "goal": "Restock missing items: cable-usbc, tissues-pack, cola-zero-330",
  "status": "interrupted",
  "completed_steps": ["i1-go-src", "i1-pick"],
  "pending_work": ["i1-go-dst", "i1-hang", "i2-go-src", "..."],
  "assigned_robot": "cart-01",
  "held_objects": {"cart-01": "cable-usbc"},
  "dependencies": {"i1-hang": ["i1-go-dst"]},
  "assignments": {"i1-go-dst": "cart-01", "i1-hang": "cart-01"},
  "steps": [{"step_id": "i1-pick", "skill": "pick", "status": "succeeded", "attempts": 2, "group": "i1", "...": "..."}],
  "notes": ["cart-01 service unavailable during i1-go-dst"]
}
```

任务状态：`active`、`paused`、`interrupted`、`completed`、`failed`、`cancelled`。

步骤状态：`pending`（依赖未满足）、`ready`、`assigned`、`running`、`succeeded`、`failed`、`skipped`、`cancelled`。

`eep_task_update` 的动作有：`pause`、`resume`、`interrupt`、`cancel`、`fail`、`complete`、`retry_step`、`skip_step`、`fail_step`、`reopen`、`assign`、`handoff`、`note`。已终止（completed / failed / cancelled）的任务不能 `resume`，只能 `reopen` 其中的步骤；仍有步骤分配在已急停机器人上的任务，需先恢复机器人或交接。

`interrupted` 与 `paused` 的区别：动作服务宕机导致 `interrupted`，编排器可在等待后自动续接；`eep_robot_stop` 导致 `paused`，必须显式恢复。

交接规则：同一 `group` 内的步骤（取物 → 搬运 → 放置）绑定同一台机器人。原机器人仍持有该工作项的物体时，`handoff` 默认被拒绝；`force=true` 会把整个工作项（含已完成步骤）交给新机器人从头重做，并在备注中提示原机器人放下物体。

步骤参数中的 `@step.key` 引用必须指向该步骤（直接或间接）依赖的步骤，否则建任务时即被拒绝。

## 7. 记忆接口

| 工具 | 作用 |
| --- | --- |
| `memory_records` | 列出等待反思的执行记录：失败码、证据、恢复动作、是否被推翻 |
| `memory_propose` | 提交长期记忆提案（见下），经共享准入后写入 |
| `memory_search` | 按上下文（domain、skill、robot_model、tags、sku）或关键词检索 |
| `memory_note` | 记一条备注；`kind=takeover` 表示人工接管 |
| `memory_reflect` | 运行内置规则反思器 |

提案格式：

```json
{
  "scope": "embodiment",
  "kind": "guidance",
  "text": "For hang at small hooks, localize with the wrist camera first.",
  "when": {"domain": "store", "skill": "hang", "tags_all": ["hook", "small"], "robot_model": "dual_arm_cart"},
  "patch": {"camera": "wrist"},
  "evidence": ["rec-3ceb-0031", "rec-3ceb-0044"]
}
```

`when` 可用的键：`domain`、`skill`、`robot_model`、`features_all`、`tags_any`、`tags_all`、`sku`。

`patch` 可用的键：`camera`、`arm`、`approach`、`constraints`、`detailed`、`options`、`use_reference`、`use_box`、`refresh_observation`、`use_placement_point`、`corner_points`、`verify_close_range`、`pause_on_intrusion`、`keep_gripper_closed`、`prefer_features`、`max_attempts`、`recovery_order`、`search_first`。提案禁止把 `pause_on_intrusion` 或 `keep_gripper_closed` 设为 `false`。

## 8. 机器人端 HTTP 契约

| 方法与路径 | 请求体 | 响应 |
| --- | --- | --- |
| `GET /eep/v0/health` | — | `{"ok": true, "protocol": "open-eep/0.1", "robot_id": "..."}` |
| `GET /eep/v0/state` | — | `{"state": {"location": "...", "holding": false, "clock_s": 12.3}}` |
| `POST /eep/v0/execute` | `{"request": ExecutionRequest, "mode": "language"}` | `{"feedback": Feedback}`，为未经判据评估的原始反馈 |
| `POST /eep/v0/observe` | `{"camera": "head", "location": null}` | `{"observation": Observation}` |
| `POST /eep/v0/check` | `{"criterion": "on_slot", "args": {...}, "close_range": false}` | `{"result": CriterionResult}` |
| `POST /eep/v0/stop`、`POST /eep/v0/resume` | `{}` | `{"ok": true}` |

错误响应统一为 `{"error": {"code", "message"}}`，状态码为 400、401、404 或 500。请求体必须是 JSON 对象；`request.robot_id` 必须与服务控制的机器人一致；`Authorization` 头不区分大小写。

Harness 侧的映射：4xx 视为不可重试的请求错误（`precondition_failed`，结果为 `rejected`，不中断任务）；连接失败、超时和 5xx 映射为 `service_unavailable`，此时编码 Agent 可以等待服务恢复，也可以交接给其他机器人。

参考实现见 `opengeniemaster/embodiment/service.py`（`RobotBackend`、`RobotService`、`serve`）。可运行的人在回路后端见 `examples/human_backend.py`。

## 9. 版本演进

不兼容的变更会提升 `open-eep/<major>.<minor>` 中的版本号。新增可选字段不视为不兼容。HTTP 路径中的 `v0` 在 1.0 之前保持不变。
