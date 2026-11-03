# PaperWritingBench 接入

本目录把一个基准样本接到现有论文写作流程，并提供独立评分入口。原生大纲确认、F6 审稿和修订选择继续生效；评分器不会替代这些节点，也不会自动改稿。

## 1. 准备样本

从仓库根目录运行（Python 3.11+）：

```sh
python -m examples.paperwritingbench.fetch_sample \
  --sample cvpr2025_1ca5e1b7d4 --out /tmp/pwb-sample --reference

python -m examples.paperwritingbench.prepare \
  /tmp/pwb-sample/writing/raw_materials --out /tmp/pwb-input/writing_input.json \
  --exclude-title 'The Devil is in the Prompts: Retrieval-Augmented Prompt Optimization for Text-to-Video Generation' \
  --exclude-arxiv 2504.11739 --published-before 2025-06-30
```

下载器固定数据集 revision，按 HTTP Range 读取单个样本。`writing/` 只保存思路、实验日志和图片；显式指定 `--reference` 才把原论文 PDF/GT 另存到 `reference/`，仅供评分。

准备脚本只读取所选思路（默认 sparse，可用 `--idea idea_dense.md`）、原始日志和图注。Markdown 表中的百分数保留原单位及数值，不换算为 0–1；完整日志另外原样保留。输出为现有业务输入，包含 `experiment`、`data`、`result_meta.data_origin=user_supplied`、`execution_status=not_run` 和 `analysis_mode=interpret_provided`。

同时保存来源文件 SHA-256、需要一起上传的图片清单，以及显式选择的排除项和时间截止。时间截止应根据实际评测协议设定；上例只是配置示例。不设置时间限制时，检索可能引入样本发表之后的工作。

## 2. 运行原生论文工作流

在目标 LazyMind 部署新建会话，将 `writing_input.json` 与 manifest 列出的 PNG 图片**一起上传**，选择“论文写作”。使用准备脚本生成的 `writing_input.request.txt` 作为启动请求，它同时把排除项和时间范围明确传给原生节点。基本请求为：

> 从附件给定的研究思路开始，运行论文写作流程；将精确附件名 writing_input.json 绑定到 paper_input，遵守文件中的排除项和文献时间范围。实验阶段导入已有日志和真实结果，分析阶段复用已有图片并解释结果，然后继续英文大纲、正文、模拟审稿和修订。不运行新实验，不生成模拟数据。

JSON 以原始字节解析并验证，图片通过现有上传媒体注册路径导入；无需在准备脚本中填写服务器路径或复制旧材料池。F3 导入原始日志和多表结果，不调用实验代码生成器或执行器。F4 把给定数值组织为可追溯结论，调用既有文字解读，复用图片，不生成分析代码或重画图。

需要限定写作模型时，在部署的“模型与服务”中选择对应的工作流 `llm` 模型，并核对实际后台任务的模型身份。部分版本的后台工作流使用账户默认模型，仅更改主对话模型不足以保证后台相同；本工具不会静默切换账户默认值。

启用部署现有 tracing（例如 `LAZYLLM_TRACE_ENABLED=1`、`LAZYLLM_TRACE_BACKEND=local`），保留会话、任务状态、阶段材料池及产物。长 trace 属性可能截断，完整材料池也需保存。

在 F2 结束后下载 `literature_review_pool.json`，先核验再进入写作：

```sh
python -m examples.paperwritingbench.audit_sources /path/literature_review_pool.json \
  --manifest /tmp/pwb-input/writing_input.manifest.json
```

排除项与日期先作为原生检索/选文要求，核验器检查材料池内所有保留版本并在违规时返回非零；不是强制检索过滤器。发现原论文或超时文献时，应隔离该次结果、用干净会话重新运行，不只删除最终参考文献。

## 3. 独立评分

```sh
python -m pip install -r examples/paperwritingbench/requirements.txt
python -m examples.paperwritingbench.fetch_upstream
```

评分来源固定为 [google-research/paper-orchestra](https://github.com/google-research/paper-orchestra/tree/ca1b3fa01c2970fc7cda32d16245db38d57b3f56)。设置脚本下载其原始源码及 Apache-2.0 许可证，每次评分前校验 manifest 内的 SHA-256。只适配模型传输；未修改官方评分规则。

使用 `LAZYMIND_BASE_URL`（默认 `http://127.0.0.1:8091`）和 `LAZYMIND_ACCESS_TOKEN`；也可传 `--auth-file /private/session.json`，接受顶层或 `data` 内的 access_token/refresh_token。会话过期最多刷新一次，刷新信息只保存在当前进程内。模型供应商密钥留在 LazyMind 服务端，认证请求头不写入评分记录。

```sh
python -m examples.paperwritingbench.score literature /path/manuscript.pdf \
  --model gpt-6-luna --avg-citation-count 58 --out /tmp/pwb-eval/literature

python -m examples.paperwritingbench.score review /path/manuscript.pdf \
  --model gpt-6-luna --out /tmp/pwb-eval/review

python -m examples.paperwritingbench.score citations /path/manuscript.pdf \
  --model gpt-6-luna \
  --gt-citations /tmp/pwb-sample/reference/raw_materials/original_paper_gt_citations_gpt-5-2025-08-07.json \
  --out /tmp/pwb-eval/citations
```

模型名称重复时使用 `--model-id`。三个指标分别是综述六维 0–100、AgentReview（三次评审加一次汇总，整体 1–10）、引用 Precision/Recall/F1 和 P0/P1 Recall（0–1）。

`--avg-citation-count` 是官方综述规则的参照参数，上游默认值为 58，并非从该样本或本领域自动统计。它会影响覆盖及引用规范的封顶，应按评测协议明确选择，参数写入 status。模型打分可能误判，不应把单样本结果或引用数量直接等同于写作能力。

默认使用供应商生成参数，记录 upstream 请求温度及该差异；可显式选择 `--generation-policy forward` 传递温度和 8192 输出预算。模型与参数不同的结果不属于原论文同设置复现。

引用评分采用官方 ID 集合运算、复用 GT 已有 P0/P1 标注。两次模型调用完成参考文献提取与批量标题解析，替代上游逐条调用 Gemini。标题规范化后精确匹配已有标题/ID，否则按上游规则查询 Semantic Scholar 并取首条；可配置 `SEMANTIC_SCHOLAR_API_KEY`。网络失败（包括持续 429）记录为失败，不写成 0 分。未解析引用按上游公式不进入 Precision 分母，必须同时查看 `resolution` 覆盖情况；F1 也不验证引用真实性或上下文正确性。

每次使用新输出目录。保留 `status.json`、`score.json`、`trace.jsonl`、模型列表、各次请求/原始返回，以及引用提取、匹配与外部查询记录。输出目录应放在仓库外；记录含完整稿件及返回内容，应按私有材料保存。

给原论文做连接自检时显式使用 `--input-role reference_control`，不要与生成稿成绩混用。评分针对 PDF 提取文本，不包含图片/版式或双稿视觉比较。若 F6 或修订失败，评分时必须披露稿件实际阶段，不能标为完整流程完成。
