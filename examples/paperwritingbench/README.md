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

## 4. AutoSurvey 作者原规则

该入口独立于上一节的整篇审稿与引用 F1，使用 [AutoSurvey 作者代码](https://github.com/AutoSurveys/AutoSurvey/tree/5e8f389f3d51b29bad16dc6ae75db3e8a45a3b65)。固定完整五档标准、原提示词、数字解析和引用计算函数，通过 AST 加载原类和常量；只替换模型传输、摘要查找、记账和线程异常报告。源码下载到本机，不随本仓库重新分发。

```sh
python -m examples.paperwritingbench.fetch_upstream --suite autosurvey

python -m examples.paperwritingbench.autosurvey prepare /path/manuscript.md \
  --topic '实际研究主题' --start '## Introduction' --end '## Problem Formulation' \
  --out /tmp/autosurvey-input.json

python -m examples.paperwritingbench.autosurvey run /tmp/autosurvey-input.json \
  --abstracts /path/literature_review_pool.json \
  --model gpt-6-luna --auth-file /private/owner-session.json --out /tmp/autosurvey-run
```

起止标题由调用者按实际稿件选择；终止标题不进入输入。省略起止参数表示使用整个文件。准备工具机械转换 LazyMind Markdown 的原生引用和图片文字图注，逐个检查相邻引用标记是否保留，记录原文件 SHA-256。LaTeX 支持正文 `\cite{key}` 和内嵌 `\bibitem{key}`，需要本机可执行的 Pandoc；引用编号沿用完整文末书目顺序。其他引用宏或外部 BibTeX 请先转换为明确的数字引用格式。图片仅保留文字图注，不做视觉评测。

也可以直接提供 JSON：`topic` 为主题，`survey` 为选定的综述文本，`reference` 将数字标签映射到论文 ID，例如 `{"1":"SRC-001"}`；正文使用 `[1][2]` 或 `[1;2]`。准备时可以用 `--references mapping.json` 显式覆盖 ID 映射，用于 LaTeX 书目键与摘要库 ID 不同的情况。

`--abstracts` 接受原生材料池的当前 papers 版本，或 `{"papers":{"SRC-001":{"title":"论文标题","abs":"真实摘要","url":"来源链接"}}}` 格式。必须提供每个所引工作的真实摘要及对应身份；本入口不在线搜索、猜测或编造缺失摘要。材料池中的 `text` 必须确实是摘要。原论文与 GT 参考集合不应作为证据输入。可用 `--phase content` 只跑三项内容评分，或 `--phase citations` 只核查引用。

输出保留准确的 `input.json`、`survey.md`、所用摘要、上游版本、逐次原始提示词/请求/响应、调用状态、抽取的引用句及 `score.json`。评分模型与生成参数不同于作者论文时，结果属于原规则在当前样本上的评测，不是官方原设置复现或排行榜分数。作者原内容提示词只要求数字，因此不会额外生成 strengths/weaknesses 解释。

| 指标 | 原代码实际计算内容 |
|---|---|
| Coverage / Structure / Relevance | 覆盖、组织结构、主题相关性，各 1–5；完整使用作者五档描述 |
| Citation Recall（带引用句支持率） | 被模型判定能由整组被引摘要支持的句子数 / 正则抽出的带引用句数 |
| Citation Precision（引用有效率） | 有用引用数 / 全部引用关联数；整组不支持时该句所有引用计零 |

某个引用单独支持句子，或者移除它后其他引用不能再共同支持句子，原函数将它记为有用。Recall **不统计无引用的句子**，不是全文事实正确率、漏引检测或与原论文书目的匹配率。摘要中没有足够证据不代表引用造假；本入口保留作者二值判断，不增加 unknown 类别或调整分母。三项均值仅为算术汇总，不与引用指标合成总分。

作者 Judge 传入 `temperature=0`，但固定版本的 API 层把它放在消息对象中，而非顶层生成参数。本入口使用供应商默认参数，并记录该原参数；没有额外系统提示词。占位 tokenCounter 只影响作者内部记账，真实 token 使用保存在供应商响应中。并发量由 `--jobs` 指定，默认 4；不设置额外的模型调用次数上限。

中断后可恢复相同输入、摘要、阶段、模型和上游版本；成功响应按完整提示词缓存，不重抽分数。原作者线程中的异常会转成明确失败，不将未完成判断的默认零当成有效分数：

```sh
# 补齐缺失调用；保留已有成功判断。
python -m examples.paperwritingbench.autosurvey run /tmp/autosurvey-input.json \
  --abstracts /path/literature_review_pool.json --model gpt-6-luna \
  --auth-file /private/owner-session.json --out /tmp/autosurvey-run --resume

# 全部调用已留存时，离线重新执行作者计算，不访问登录或模型接口。
python -m examples.paperwritingbench.autosurvey run /tmp/autosurvey-input.json \
  --abstracts /path/literature_review_pool.json --model gpt-6-luna \
  --out /tmp/autosurvey-run --resume --offline
```

AutoSurvey 的所有并发调用共用一个客户端，登录过期只刷新一次。显式提供 `--auth-file` 时，将轮换后的会话原子写回**该文件**，供后续恢复；供应商密钥留在服务端，登录请求头和刷新令牌不写入评测记录。只使用当前指定的会话来源。输出包含完整稿件和响应，应放在仓库外并保留为私有材料。

## 5. WritingBench、ALCE、FActScore 材料对照适配

这三个入口整理自本地已运行的论文写作试跑，与第 4 节的 AutoSurvey 作者原函数入口分开。它们是**基于方法思想的论文场景适配**，不是三套官方 benchmark 实现或官方分数；JSON、提示词文件和报告均标记 `official_benchmark=false`，指标名带 `_inspired`。

| 方法来源 | 这里执行的内容 | 与原评价流程的主要区别 |
|---|---|---|
| [WritingBench](https://papers.nips.cc/paper_files/paper/2025/hash/4aedf0cba303537fcb6cf948bb41b2df-Abstract-Datasets_and_Benchmarks_Track.html) | 只看任务和给定研究材料生成五项细则，冻结五档锚点；逐项评完整稿件，输出 1–10 分及理由 | 使用用户的通用评分模型，未接官方微调 critic，也未使用原测试集 |
| [ALCE](https://aclanthology.org/2023.emnlp-main.398/) | 抽取需要外部引用的陈述，判定引用适用范围，再核查整组支持与逐引用作用 | 科研论文场景，GPT 判断替代原 NLI；允许摘要不足时保留未知项，不是原问答评测全套指标 |
| [FActScore](https://aclanthology.org/2023.emnlp-main.741/) | 逐段抽取原子事实，依据给定实验材料和被引文献核查来源忠实度 | 证据为本次给定材料，未复现官方传记任务、知识库检索和完整评分流程；区分输入冲突及证据不足 |

准备完整 Markdown 稿件和**实际交给写作者的输入材料**：

```sh
python -m examples.paperwritingbench.material_eval prepare /path/full-manuscript.md \
  --assignment /path/original-writing-request.txt \
  --source /path/idea_dense.md --source /path/experimental_log.md \
  --source /path/figure_captions.json --literature /path/literature_review_pool.json \
  --out /tmp/material-input.json

python -m examples.paperwritingbench.material_eval run /tmp/material-input.json \
  --model gpt-6-luna --auth-file /private/owner-session.json --out /tmp/material-eval
```

也可直接提供包含 `task`、`sources`、`literature`、`manuscript` 的 JSON，或为已有材料包指定 `--manuscript /path/full.md`。`task` 包含原始写作要求，`sources` 将来源 ID 映射到原文或结构化数据，`literature` 是含 `id/title/text` 的摘要记录列表。给出普通数字引用时，准备阶段可用 `--references` 提供标签到文献 ID 的映射；LazyMind 原生 `#paper-ref-ID` 引用直接识别。LaTeX 请先用 Pandoc 转成完整 Markdown，并显式核对引用 ID 与摘要身份。

WritingBench 评分始终读取**包括文末书目的完整稿件**。事实抽取只去掉书目内容，保留后面的附录，并检查全部正文段落恰好返回一次。无法逐字定位的抽取条目不计入指标，并保存在排除记录中。数字、表格和图注作为原文传入；没有自动像素识别或图表美观评分。需要使用人工/其他工具读图结果时，应把带来源说明的文字记录通过 `--source` 显式提供。

可选 `--provenance` 提供该次工作流的真实执行记录，例如是否导入已有结果。此记录用于写作/事实核查，**不进入细则生成**；不要把“本轮写作未重跑实验”误解为“原研究者没做实验”。其他未提供的实验细节视为输入边界，判断写作者是否歪曲、虚构或无依据补全。

比较两个 agent 时，使用相同原始要求和研究材料，并让第二篇复用第一篇的冻结细则与证据清单：

```sh
python -m examples.paperwritingbench.material_eval run /tmp/opencode-material-input.json \
  --rubric /tmp/material-eval/rubric-frozen.json --model gpt-6-luna \
  --auth-file /private/owner-session.json --out /tmp/opencode-material-eval
```

冻结包记录任务和研究材料指纹；任务或材料不同的稿件需要各自生成细则。默认执行三个适配器，也可用 `--metrics writingbench`、`--metrics alce factscore` 单独选择。

输出中明确区分分母：

- `writingbench_inspired`：五项细则、逐项分数/理由和均分；另附主要输入证据组的覆盖判断。
- `factscore_inspired`：保留支持、可推导支持、矛盾、无据新增、输入冲突、证据不足、非事实陈述的全部计数；“可判定子集支持率”排除冲突与未知，“严格支持占比”保留其影响。空分母为 `null`，不写成满分或零分。
- `alce_inspired`：保留支持、矛盾、证据不足和漏引计数，分别报告可判定子集比例、包含未知项的确认支持下界、逐引用未知比例；原子事实正确与引用支持是不同检查。

引用适用范围判断会排除摘要、仅描述本研究的方法/结果和明确的作者推理；只允许同段落中无歧义的上下文引用绑定。该规则在核查支持之前统一作用于所有候选句，属于本适配协议。它不同于第 4 节 AutoSurvey 只统计带引用句子的二值口径，两者不能直接混算。

每步保留任务提示词、输入、原始请求/响应、失败状态和结构化结果；记录完整评测说明及其指纹。`--resume` 仅补齐缺失调用，`--resume --offline` 仅使用相同消息的留存响应；更换材料、细则、方法、模型或服务需新建输出目录。JSON 格式、ID 完整性、评分范围等无效返回不作为成功判断缓存。

`evidence-anchor-audit.json` 检查模型所引证据能否在允许来源中逐字定位。警告与原始标签一并保留，不能把模型标签当作人工确认的事实。旧试跑中的事后诊断复核和人工摘录修正不自动套用到新稿；新运行声明 `posthoc_adjudication=false`，不会把旧稿的修订分数当成一次新的全流程成绩。原论文/GT 无需进入这些材料，结果不代表科学结论成立或投稿接收概率。
