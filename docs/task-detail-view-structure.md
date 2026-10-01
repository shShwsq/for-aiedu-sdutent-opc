# 任务详情界面(TaskDetailView)显示结构

> 描述对象:`frontend/src/views/TaskDetailView.vue` —— 双 Agent 协作的核心可视化界面。
> 本文档描述**迭代摘要行("N 个工具调用: xxx")及其外框移除后**、
> **右侧栏布局调整(状态/导出迁入标题行、结果置底、移除用户意图卡片)后**的目标结构。

## 1. 页面整体:三栏布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ AppHeader(顶栏,左侧含历史任务栏折叠按钮)                              │
├────────────┬──────────────────────────────────┬───────────────────────┤
│ 左侧        │ 主区 main                        │ 右侧 detail-sidebar    │
│ Workspace  │ (协作对话流,页面核心)             │ (任务详情抽屉)         │
│ Sidebar    │                                  │                       │
│ 历史任务列表 │                                  │ 覆盖度/概览/验证/       │
│ (可折叠隐藏) │                                  │ 结果(可折叠)            │
└────────────┴──────────────────────────────────┴───────────────────────┘
```

- **左侧 WorkspaceSidebar**:历史任务列表,支持删除/重命名;折叠时完全隐藏,由顶栏按钮切换。
- **右侧 detail-sidebar**:宽度 `clamp(320px, 28vw, 420px)`,默认展开,折叠后退化为右上角悬浮把手(有结果时显示数量角标)。详见 §5。

## 2. 主区 main 纵向结构

```
main
├── conv-header          标题/状态行(固定,不随对话滚动)
├── main-scroll          滚动容器
│   ├── conversation-section   协作对话流(核心,见 §3)
│   └── workspace-changes-section  工作区变更(git diff,仅有产物时)
└── UserMessageInput     用户补充消息输入框(running/paused/completed 可见)
```

### 2.1 conv-header(标题/状态行)

- 左:任务标题(截断显示)+ 创建时间。
- 右:运行态显示红色"实时"徽标 + 暂停按钮;暂停态显示橙色"已暂停"徽标 + 恢复按钮。

### 2.2 工作区变更区

任务完成时捕获的 git diff patch,只读展示(按行着色);头部显示变更文件数/字符数/截断提示,支持折叠。

## 3. 协作对话流(conversation-section)

对话流按 `round_idx` 分组,层级如下:

```
conversation-section
├── user-directive                 用户指令(右对齐气泡,从对话中提取,置顶)
└── round-group × N                轮次分组(round_idx=0 显示"初始评估",否则"第 N 轮")
    ├── round-label                轮次标签
    ├── plan-card                  计划清单(复杂任务时 agent1 输出,可无)
    │     └── plan-step × N        步骤条目(✓ done / ◌ in_progress / ○ pending)+ 进度 x/y
    └── messages                   消息容器(flex 纵向,gap 控制间距)
        ├── plain segment × N      平铺段(关键消息,单张卡片直接显示)
        └── step group × N         步骤分组(折叠块,承载迭代内容)
```

### 3.1 plain segment(平铺段)

不进折叠块、直接渲染为单张卡片的关键消息:

- agent2 的轮次评估、追问、总结;
- 用户补充消息(type=message,右对齐,与顶部 userDirective 视觉一致)。

### 3.2 step group(步骤分组,折叠块)

**唯一的折叠单位**。一个 step group 对应:

- 有 plan 时:归属同一 plan step 的若干迭代(文字 = step 文本);
- 无 plan 时:该轮所有迭代归入单个 **"执行过程"** 组;
- 无法归属 plan step 的迭代也归入 "执行过程" 组。

```
step-block
├── step-header(点击切换折叠)
│   ├── 折叠箭头 ▶/▼
│   ├── 状态图标(✓ done / ◌ in_progress / ○ pending / · none)
│   ├── step 文本
│   ├── "N 次迭代"
│   └── 打字动画(含流式内容时)
└── step-body(展开时)
    └── 迭代内容 × N(直接平铺,无摘要行、无边框包装)
        ├── thinking 卡片          ConversationMessage(流式或历史)
        ├── 工具渲染行 × N          见 §3.3
        └── otherItems 卡片        submit 等其他项
```

**折叠策略**(`isStepExpanded`):

1. 用户手动收起优先级最高(`collapsedSteps`);
2. 用户手动展开、或组内含流式中内容 → 展开;
3. 任务已结束且是该界面最后一个 step 组(最终总结所在)→ 展开;
4. 其余默认折叠。

### 3.3 迭代与工具渲染行

一个**迭代** = agent1 一次 ReAct 循环(thinking + N 个 tool_call/tool_result)。
切分规则:遇到 agent1 thinking 开新迭代,后续 agent1 工具项归入当前迭代,
遇到下一个 thinking 或非 agent1 消息则关闭当前迭代。

**锚点缺失兜底**:thinking 锚点可能缺失(空 thinking 未落库、CLI agent 未发文本
直接发起工具调用、前面的非 agent1 消息关闭了迭代)。此时开一个无 thinking 的
兜底迭代承接工具项,保证其仍归入 step 组正常渲染,而不是退化为 plain 段
被追加到"执行过程"折叠块末尾。

迭代内容在 step-body 内**直接平铺**(不再包 "N 个工具调用: xxx" 摘要行和外框),
工具项经 `toolRowsOf` 配对后渲染为四种行类型:

| 类型 | 触发条件 | 形态 |
|---|---|---|
| `compact` | 浏览型工具(read_file/list_files/search_code 等) | 单行摘要(🔧 意图),点击轻量展开原始结果 |
| `agent` | 子智能体调用(`[Agent]`) | 标题卡片,展开后:子任务参数 + 内部思考(二级折叠)+ Markdown 报告 |
| `toolpair` | 普通工具 | 标题卡片,展开后:调用参数 + 工具结果(等宽块) |
| `plain` | 落单项(如孤儿 tool_result) | ConversationMessage 原样渲染 |

工具行默认折叠,展开状态按 tool_call id 记录(`expandedToolRows`,
子智能体内部思考用 `${callId}-think` 复合键)。

### 3.4 运行中等待提示(waiting-hint)

任务运行中且无流式项时显示:优先展示后端推送的克隆进度
(阶段文案 + 百分比 + 进度条),否则显示通用打字动画;暂停态不显示动画。

## 4. 数据组装管线

```
task.conversations(正式对话,含历史 thinking 还原)
  + streamingItems(实时流式 thinking,SSE 推送,不入 convs)
      │  按 round_idx 归组;seq 稳定排序
      │  (正式对话 seq = 轮内下标×1000;流式项 seq = insertSeq×1000−500,
      │   保证 thinking 恰好插在其后 tool_call 之前)
      ▼
roundGroups(computed)
      │  每轮:segmentRoundItems()
      │    一阶段:按 thinking 切迭代,非 agent1 消息记为 plain 段
      │    二阶段:迭代按 plan step 关键词推断归组(TOOL_STEP_KEYWORDS),
      │           无 plan / 无法归属 → "执行过程"组;
      │           plain 段按轮内原始位置穿插到组间/组内迭代边界
      ▼
RoundGroup { roundIdx, label, segments, planSteps }
```

## 5. 右侧栏 detail-sidebar

### 5.1 标题行(detail-sidebar-header)

```
┌──────────────────────────────────────────────┐
│ 任务详情        [状态徽标] [⬇下载] [🖨打印] [⟩⟩] │
└──────────────────────────────────────────────┘
```

- 左:标题"任务详情";
- 右(自左至右):**状态徽标**(如"已完成",随 task.status 实时变化)→
  **下载按钮**(导出 Markdown 报告)→ **打印按钮**(打印/另存 PDF)→
  **侧栏折叠按钮**(WorkspaceToggleButton);
- 下载/打印按钮的显示条件保持原逻辑:任务 completed 或有结果时才出现;
  状态徽标始终显示(按钮隐藏时仅剩徽标)。

### 5.2 内容区(detail-sidebar-body)自上而下

1. **任务概览**:场景/创建时间/完成时间、当前阶段、错误信息。
   不再包含:状态徽标与下载/打印按钮(已移至标题行,见 §5.1)、
   用户意图卡片(不再显示;用户指令仍保留在对话流顶部 userDirective 气泡)。
2. **动态验证**(配置了测试环境 URL 时):开关、授权模式切换、登录凭证(脱敏);不出现 verifier_agent 字样。
3. **结果清单**(最底部):按 `task.params._grouping` 动态分组(如按严重度);卡片默认折叠,展开显示 Markdown 正文;文件类 meta 标签可点击打开左侧工作区文件。

> 历史说明:覆盖度看板(task.checklist 驱动的维度卡片网格)已随覆盖度清单功能移除。

## 6. 全局弹窗

| 弹窗 | 触发 |
|---|---|
| VerifyActionDialog | 动态验证 per_action 模式,逐动作授权 |
| CommandConfirmDialog | local 模式危险命令确认 |

## 7. 与旧版结构的差异(本次改动)

| 旧结构 | 新结构 |
|---|---|
| step group → iteration-block(边框+背景盒子)→ iteration-divider("N 个工具调用: xxx"摘要行)→ iteration-body | step group → 迭代内容直接平铺(wrapper 退化为透明容器) |
| 流式迭代靠盒子光晕(iteration-streaming)提示 | 由 step-header 打字动画 + 流式 thinking 卡片自身样式表达 |
| `iterationSummary()` / `toolCallCount()` 生成摘要文本 | 两个函数移除(信息已由工具行自身的单行摘要/卡片标题覆盖) |

保留不变:迭代切分逻辑(`segmentRoundItems`)、
step 组折叠策略、工具行四种渲染类型。

## 8. 右侧栏布局调整(本次改动)

| 旧结构 | 新结构 |
|---|---|
| 标题行仅"任务详情" + 折叠按钮 | 标题行追加:状态徽标 + 下载按钮 + 打印按钮,排在折叠按钮左侧 |
| body 顺序:覆盖度 → 结果清单 → 任务概览 → 动态验证 | body 顺序:覆盖度 → 任务概览 → 动态验证 → 结果清单 |
| 状态徽标与下载/打印按钮在"任务概览"区块顶部(overview-header) | 迁入标题行;任务概览区块只剩元信息/当前阶段/错误 |
| 任务概览含用户意图卡片(user_input 的 Markdown 渲染) | 移除,不再显示(对话流顶部 userDirective 气泡仍保留用户指令) |

保留不变:下载/打印按钮的显示条件(completed 或有结果)、导出逻辑
(exportMarkdown / exportPdf)、结果卡片折叠交互。

实现要点(供代码改动参考):

1. `detail-sidebar-header` 内新增状态徽标 + `overview-actions`(下载/打印),
   位于 WorkspaceToggleButton 之前;原 overview-section 的 `overview-header` 整块移除。
2. body 内把 `sidebar-results` section 移到 `verifier-section` 之后。
3. 移除 overview-section 内的 `.overview-input`(用户意图)块;相关 CSS 一并清理。
4. 标题行空间有限:状态徽标与按钮需紧凑样式(小尺寸图标按钮),
   避免挤压标题;窄屏下优先保标题截断而非换行。
