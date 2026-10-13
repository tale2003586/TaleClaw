# Hybrid 路由离线试验（不部署）

本轮实现了真实入口的结构化 RouteDecision、权限/候选门控回放、原创数据校验与 CPU 便宜基线。**没有进行语言模型微调，没有部署新分类器，也没有证明 Agent 端到端提升。** 当前生产行为仍由原候选规则、模式权限、原模型分类器控制。

## 数据与标签

`pilot.jsonl` 是本轮原创合成 64 条中英请求、32 个意图/改写家族，单作者标注，未使用生产轨迹、用户私有代码或个人数据。每家族两条语言变体绑定划分：train 40 条/20 家族、dev 12/6、test 12/6。清单 SHA 在 `manifest.json`，脚本严格校验。家族表示同一个原创请求意图，不等于已经证实独立的真实用户分布；共享技术话题与人工写作风格限制泛化结论。

标签合同：`coding` 表示对具体工作区/文件/终端/代码实施或检查；`bot` 表示普通聊天、概念解释、写作等。标签不授予权限，非管理员/固定bot模式仍不能进入coding。缺上下文、指代或长文本截断的真实样本必须另标 `needs_context`/歧义，不能强迫二分类。当前pilot未覆盖这类真实分布，不作上线评估。

每行含 `id/family_id/split/label/raw_text/visible_text/source/authorized/annotation`。可见文本与现行分类器一致为strip后4000字符前缀；全入口是否触发分类器由单独回放检查。校验会拒绝家族跨split、重复输入/ID、可见输入不一致、未经授权来源，以及常见凭据/邮箱/个人目录模式。规则过滤不等于完备脱敏；真实数据需重新审查授权、人工去标识与清单，此脚本默认拒收真实来源。

## 可重现运行

已有环境实际使用 Python 3.12.4、scikit-learn 1.9.0。未安装新依赖。研究依赖单列在requirements.txt，不进入Agent必需依赖。仓库根目录：

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python research/hybrid_routing/baselines.py --output /tmp/routing-pilot-new.json
```

脚本不覆盖已有结果，不使用网络、LLM客户端、GPU或生产轨迹。规则`rule_v1`与字符TF-IDF(2–4gram)+LogisticRegression(C=1,seed=17)参数在首次holdout前冻结；词表只拟合train，dev/test不参与训练。`results/pilot-v1.json`保留最初运行；其`batch_prediction_seconds`错误地含指标计算，**不得用于预测延迟比较**。独立审查后修计时，`pilot-v2.json`只计predict；模型/规则/划分未调参，预测应完全一致。微秒级小批时延也不能外推生产P95。

| 结果（各12条/6家族） | 规则 macro-F1 | 字符线性 macro-F1 |
|---|---:|---:|
| dev | 0.9161 | 0.8333 |
| test | 0.9161 | 0.8333 |

这是同一小型合成分布上的探索性结果。独立执行者复跑得到相同逐题预测/混淆矩阵；没有独立人类标注者，没有足够family支持1pp路由或2pp E2E非劣性，因此不提供虚假的精确置信保证。错误示例见结果JSON：规则漏掉英文write-test请求，线性把英文项目介绍润色误判coding；不是线上事故。

## Go/No-go

当前结论 **No-go：不运行LoRA，不部署规则或线性替换器**。缺授权真实路由频率/错误/成本基线，64条单作者合成不能满足总方案§9。Qwen3-0.6B未缓存，未下载；已有6GiB GPU不等于训练方案和收益已经成立。现行模型prompt/few-shot、同基座zero/few-shot、LoRA、服务摊销与完整入口配对E2E均未执行。完成CPU线性训练不冒充LoRA smoke或微调成果。

后续只有独立标注/授权数据、完整费用与门控可见性齐备，B1–B5仍有可学误差和实用收益空间，才进入一次冻结模型/数据/tokenizer/配置的LoRA实验。届时训练依赖单独锁定，先tokenization/loss mask与小批smoke，再训练；若错误主要来自gate/缺历史/截断或便宜基线已够用，应停止。当前不保留一个从未运行的占位训练脚本或无作用的部署模块。

## 接口与证据

- `runtime/routing/hybrid_classifier.py::RouteDecision`：不可变决策，无共享last_result；`should_use_coding`兼容bool。valid=false保留具体fallback，cancel/budget继续向上停止。
- `AgentRouter.route`：决策只是门内建议；保留既有权限/固定模式，支持单次结果callback。coordinator记录routing.gate/classified，默认只有元数据。
- `tests/test_routing_decisions.py`：全入口、并发隔离、截断可见性、无效结构、取消、数据污染/家族泄漏；不是模型能力题库。
- 官方[TF-IDF文档](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html)（访问2026-09-27）支持字符特征与fit/transform接口；实际参数/版本以本目录脚本及结果为准，文档当前版本不代替运行依赖锁。
