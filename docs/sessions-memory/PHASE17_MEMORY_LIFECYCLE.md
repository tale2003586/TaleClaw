# Phase 17：Memory Lifecycle 说明

这次 Phase 17 做的是 Markdown 记忆生命周期的第一版。

它不是 embedding，不是向量库，也不是复杂语义检索。它解决的是更基础的问题：

```text
对话结束后，哪些内容应该进入长期记忆候选？
当前最近上下文怎么维护？
历史流水怎么归档？
```

---

## 一、这次新增了什么

### 1. 扩展 `memory/store.py`

原来只有：

```text
MEMORY.md
SELF.md
NOW.md
```

现在增加：

```text
PENDING.md
HISTORY.md
RECENT_CONTEXT.md
```

它们的含义：

```text
MEMORY.md
  已确认的长期记忆，比如用户偏好、项目约定。

SELF.md
  agent 自我设定。

NOW.md
  当前正在进行的状态。

PENDING.md
  候选记忆。不是所有东西都直接进长期记忆，先放这里等待后续确认/整理。

HISTORY.md
  对话历史流水。每轮结束后追加简短记录。

RECENT_CONTEXT.md
  最近上下文摘要。每轮覆盖更新，只保留当前最近状态。
```

---

### 2. 新增 `memory/lifecycle.py`

新增类：

```python
class MemoryLifecycle:
    def after_turn(session) -> MemoryLifecycleResult:
        ...
```

它在一轮对话结束后运行。

第一版做四件事：

```text
1. 如果用户明确说“记住...”，直接写入 MEMORY.md
2. 如果用户表达明显偏好/项目约定，写入 PENDING.md
3. 把这一轮 user/assistant 简要追加到 HISTORY.md
4. 用最近一轮内容覆盖 RECENT_CONTEXT.md
```

---

### 3. 接入 `Pipeline._after_turn`

现在 `pipeline.py` 的 `_after_turn()` 会：

```python
if self.memory_lifecycle is not None:
    self.memory_lifecycle.after_turn(session)
session.touch()
```

也就是说，每轮 agent 正常回复结束后，会自动维护记忆文件。

---

### 4. Bootstrap 装配

`bootstrap.py` 里现在有：

```python
memory_store = MemoryStore()
context_builder = ContextBuilder(memory_store=memory_store)
memory_lifecycle = MemoryLifecycle(memory_store)
```

然后注入 Pipeline：

```python
pipeline = Pipeline(
    ...
    context_builder=context_builder,
    memory_lifecycle=memory_lifecycle,
)
```

这体现了一个关键工程原则：

```text
MemoryStore 是底层存储
ContextBuilder 负责把 memory 读进上下文
MemoryLifecycle 负责在 turn 结束后维护 memory
Pipeline 只负责调用生命周期入口
```

---

## 二、现在的记忆流向

### 读取记忆

每轮调用模型前：

```text
MemoryStore.read_all()
  -> ContextBuilder._build_memory_block()
  -> context.messages
  -> provider.chat(...)
```

也就是：

```text
MEMORY/SELF/NOW/RECENT_CONTEXT 会进入模型上下文
```

### 写入记忆

每轮回复结束后：

```text
Pipeline._after_turn()
  -> MemoryLifecycle.after_turn(session)
  -> MEMORY.md / PENDING.md / HISTORY.md / RECENT_CONTEXT.md
```

也就是：

```text
对话产生的新信息会被生命周期处理
```

---

## 三、第一版规则

### 明确记忆

如果用户说：

```text
记住我喜欢简洁回答
请记住这个项目测试优先用 pytest
以后记得我不喜欢太啰嗦
```

会直接写入：

```text
MEMORY.md
```

### 候选记忆

如果用户说：

```text
我喜欢短一点的解释
这个项目用 pytest
代码风格尽量保守
```

但没有明确“记住”，会写入：

```text
PENDING.md
```

这是候选记忆，后续可以人工确认，或者未来加模型 consolidator 自动整理。

### 历史流水

每轮都会追加到：

```text
HISTORY.md
```

### 最近上下文

每轮都会覆盖：

```text
RECENT_CONTEXT.md
```

它只保留最近状态，不无限增长。

---

## 四、为什么要有 PENDING.md

不要把所有疑似偏好都直接写进 MEMORY.md。

原因：

```text
1. 模型可能误判
2. 用户一句临时抱怨不一定是长期偏好
3. 长期记忆污染后会一直影响后续回答
```

所以更稳的设计是：

```text
明确记忆 -> MEMORY.md
疑似长期信息 -> PENDING.md
```

这是很多长期记忆系统都会有的思想：

```text
candidate -> confirm -> consolidate
```

---

## 五、为了大模型应用开发实习，你要掌握什么

Phase 17 背后的面试重点不是 Markdown 文件，而是 **记忆系统设计**。

你需要能讲清楚：

### 1. Session Memory vs Long-term Memory

```text
Session Memory
  当前会话历史，存在 SQLite sessions.db。

Long-term Memory
  跨会话稳定信息，存在 MEMORY.md / SELF.md / NOW.md。
```

这两个不是一回事。

### 2. Retrieval 和 Writeback 是两条链路

读取：

```text
memory files -> ContextBuilder -> model context
```

写入：

```text
after_turn -> MemoryLifecycle -> memory files
```

这两个方向要分开设计。

### 3. 记忆不能无脑写

长期记忆要有筛选机制：

```text
明确要求记住
稳定偏好
项目约定
用户身份事实
长期目标
```

临时信息、工具输出、模型猜测，不应该直接进长期记忆。

### 4. 第一版不用向量库也能做记忆

很多人一说 memory 就想 embedding。

但工程上更重要的是先有：

```text
文件/数据库结构
写入时机
召回时机
去重策略
污染控制
```

embedding 是后面的检索优化，不是第一步。

### 5. 记忆污染是 LLM 应用的重要风险

如果错误信息进入长期记忆，模型后面会反复被污染。

所以要有：

```text
PENDING
source_ref
人工或模型确认
dedup
supersede
forget
```

你现在已经有了第一步：

```text
PENDING.md + source_ref
```

---

## 六、下一步可以怎么改

### 1. `/memory pending`

列出 PENDING.md。

### 2. `/memory accept`

把某条 pending 移入 MEMORY.md。

### 3. `/memory clear`

清理候选记忆。

### 4. 更好的候选提取

现在是关键词规则。

以后可以用 light model 提取：

```json
{
  "should_remember": true,
  "target": "pending",
  "tag": "preference",
  "content": "User prefers concise answers."
}
```

### 5. 去重和 supersede

比如旧记忆：

```text
用户喜欢详细解释。
```

新记忆：

```text
用户现在更喜欢简洁回答。
```

应该能替换旧记忆，而不是两个都保留。

---

## 八、本次补充：规则去重

这次在 Phase 17 里补了第一版规则去重。

新增文件：

```text
memory/dedup.py
```

它做三件事：

```text
1. 从 Markdown 中解析 bullet 记忆项
2. 对记忆文本做 normalize
3. 写入前判断是否已经存在等价记忆
```

normalize 会去掉这些不影响语义的格式差异：

```text
- bullet 符号
- [preference] / [project] 这类 tag
- (source: `...`) 来源标记
- 常见中英文标点
- 空白
- 大小写差异
```

例如下面两条会被认为重复：

```text
- 用户喜欢简洁回答。
- [preference] 用户喜欢简洁回答 (source: `cli:local:12`)
```

现在这些写入入口都会先做规则去重：

```python
MemoryStore.append(...)
MemoryStore.append_pending(...)
```

也就是说：

```text
明确记忆写入 MEMORY.md 前会查重
候选记忆写入 PENDING.md 前也会查重
```

### 为什么先做规则去重

第一版没有直接把整个 `MEMORY.md` 发给大模型重写。

原因：

```text
1. LLM 直接改整份记忆风险高，可能误删
2. 成本会随 MEMORY.md 增长而升高
3. 输出格式不稳定
4. 难追踪某条记忆为什么被删除或替换
```

更稳的路线是：

```text
规则去重
  -> 候选召回
  -> 小范围 LLM 判定 duplicate / add / supersede / conflict
  -> 程序执行写入
```

当前完成的是第一步：

```text
规则去重
```

### 后续 LLM 去重应该怎么做

以后如果要用大模型，不建议：

```text
把整个 MEMORY.md 丢给模型，让模型重写
```

更推荐：

```text
新记忆
  -> 规则找出 3-5 条可能重复的候选
  -> 只把新记忆和候选发给模型
  -> 模型返回结构化 JSON
  -> 代码执行 add / duplicate / supersede / conflict
```

这体现一个重要的大模型应用工程原则：

```text
LLM 负责判断，程序负责落库。
```

不要让 LLM 直接掌控长期状态文件。

---

## 七、你现在可以怎么介绍这个项目

面向大模型应用开发实习，可以这样说：

> 我实现了一个轻量 agent runtime 的长期记忆生命周期：区分 session history 和 long-term memory；通过 ContextBuilder 在推理前注入记忆块；通过 after-turn lifecycle 自动维护 pending memory、history journal 和 recent context；并保留 source_ref，为后续去重、确认和语义检索打基础。

这比“我做了个聊天机器人”专业很多。

---

## 九、本次代码改动补充：去重落点

这次直接改了两层：

```text
memory/dedup.py
memory/store.py
memory/lifecycle.py
```

`memory/dedup.py` 是纯规则层，只负责判断“这条新记忆和已有 bullet 是否等价”。它不读写文件，也不调用模型。

`MemoryStore` 是写入层：

```text
append(...)
  -> 写 MEMORY/SELF/NOW 等文件前，先查目标文件是否已有等价条目

append_pending(...)
  -> 先查 MEMORY.md
  -> 再查 PENDING.md
  -> 都没有重复才写入 PENDING.md
```

这里多查 `MEMORY.md` 很重要。因为如果一条记忆已经被确认进长期记忆，就不应该再作为候选记忆反复进入 `PENDING.md`。

`MemoryLifecycle` 是生命周期层：

```text
after_turn(...)
  -> 调用 MemoryStore 写入
  -> 只有返回 Saved... 时，才把 pending_added 加一
```

也就是说，去重后被跳过的记忆不会被统计成“新增记忆”。

这个设计里有一个很重要的边界：

```text
dedup 只做判断
store 只做读写
lifecycle 只决定什么时候触发
```

这就是实习面试里可以讲的工程点：不要把“判断、存储、调度”揉成一个大函数。拆开以后，每一层都容易测试，也容易替换。以后你想把规则去重升级成 embedding 召回或 LLM 判定，只需要替换 dedup/判定层，不需要重写整个 pipeline。
