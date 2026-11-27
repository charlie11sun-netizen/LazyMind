# Generate PPTs with LazyMind and paper baselines

This example uses two pinned public tasks. LazyMind, DeepPresenter, and DeepSlides each consume the original prompt and PDF for both tasks and produce editable PowerPoint files.

| Task | Data source | Generate with |
|---|---|---|
| Alphabet 2024 Q1 | [PresentBench](https://huggingface.co/datasets/PresentBench/PresentBench) | LazyMind, [DeepPresenter](https://github.com/icip-cas/PPTAgent), [DeepSlides](https://github.com/sxswz213/DeepSlides) |
| 50 Million Steps | [SlidesGen-Bench](https://github.com/YunqiaoYang/SlidesGen-Bench) | LazyMind, DeepPresenter, DeepSlides |

Run the commands from the LazyMind checkout root. Use Python 3.12, Docker, Node.js, Chromium with Playwright, and Poppler. The DeepSlides execution container provides LibreOffice. Keep the run directories, API keys, and LazyMind session JSON outside the checkout.

## 1. Download the inputs

```bash
export WORK="$(mktemp -d)"
python3.12 -m examples.ppt_benchmarks.cases fetch "$WORK/cases"
```

The script checks the SHA-256 of both prompts and both PDFs. The generated PPTX and PDF files stay under `$WORK`.

## 2. Generate with LazyMind

Start a LazyMind deployment using the [Quick Start guide](../../docs/quick_start.md). Set `LAZYMIND_URL` to its base URL and `LAZYMIND_AUTH_FILE` to a private file containing the auth response or the unwrapped JSON object with `access_token`. Set `LAZYMIND_MODEL_ID` to the ID of your configured `deepseek/deepseek-v4.1-flash` model. The runner uses the native `builtin:ppt-workflow` and saves the reported model and requested thinking depth in `submission.json`.

Install the editable exporter dependencies from the checkout. If an existing Playwright browser is supplied through the LazyMind runtime, use that dependency bundle as described in the exporter.

```bash
EXPORTER="$PWD/workflows/ppt-workflow/runtime/scripts/export_pptx"
npm ci --prefix "$EXPORTER"
(cd "$EXPORTER" && npx playwright install chromium)

for CASE in alphabet steps; do
  RUN="$WORK/lazymind-$CASE"
  python3.12 -m examples.ppt_benchmarks.cases prepare "$CASE" "$WORK/cases" "$RUN"
  python3.12 -m examples.ppt_benchmarks.run_lazymind submit "$RUN" \
    --base-url "$LAZYMIND_URL" --auth-file "$LAZYMIND_AUTH_FILE" \
    --model-id "$LAZYMIND_MODEL_ID" --thinking-depth low
  python3.12 -m examples.ppt_benchmarks.run_lazymind status "$RUN" \
    --base-url "$LAZYMIND_URL" --auth-file "$LAZYMIND_AUTH_FILE"
done
```

For a local case in PresentBench's directory format, use `python3.12 -m examples.ppt_benchmarks.run_lazymind prepare "$CASE_DIR" "$RUN"`. It reads `generation_task/instructions.md` and `material*`. Both preparation commands accept `--delivery` to append a delivery requirement to the original prompt.

If startup questions are returned, read `$RUN/*.private.questions.json` and run `python3.12 -m examples.ppt_benchmarks.run_lazymind clarify "$RUN" --answer 'question_id=value' --base-url "$LAZYMIND_URL" --auth-file "$LAZYMIND_AUTH_FILE"`, repeating `--answer` for each question. Use the task requirements when answering. If the workflow pauses for review, review and continue the same conversation in LazyMind. Poll `status` until `generate_ppt` has succeeded and the deck is published. Then export each run.

```bash
for CASE in alphabet steps; do
  python3.12 -m examples.ppt_benchmarks.run_lazymind export "$WORK/lazymind-$CASE" \
    --base-url "$LAZYMIND_URL" --auth-file "$LAZYMIND_AUTH_FILE" \
    --local-exporter "$EXPORTER/html_to_pptx.mjs"
done
```

The export writes `slides.pptx` and its metadata under each run directory. For new paired comparisons use the same thinking depth on both sides. The initial Alphabet LazyMind pilot used `high`.

## 3. Generate with the paper methods

For the default `--backend api`, set `PPT_GENERATION_BASE_URL` to your OpenAI-compatible Chat Completions base URL, including `/v1` if the provider uses that path. Supply the credential in `PPT_GENERATION_API_KEY`. The endpoint must expose `deepseek/deepseek-v4.1-flash` with tool calling, image input, and `reasoning_effort=low`. The same installation steps below apply to `--backend codex`.

```bash
DEEP_PRESENTER="$WORK/DeepPresenter"
DEEP_SLIDES="$WORK/DeepSlides"
git clone --filter=blob:none https://github.com/icip-cas/PPTAgent.git "$DEEP_PRESENTER"
git -C "$DEEP_PRESENTER" checkout --detach 2419d30b134a71486523e95ded60b32489fd3c61
git -C "$DEEP_PRESENTER" apply "$PWD/examples/ppt_benchmarks/deeppresenter-compat.patch"
python3.12 -m venv "$WORK/presenter-venv"
PRESENTER_PY="$WORK/presenter-venv/bin/python"
"$PRESENTER_PY" -m pip install -e "$DEEP_PRESENTER" huggingface_hub
"$PRESENTER_PY" -m playwright install chromium
PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true npm install --prefix "$DEEP_PRESENTER/deeppresenter/html2pptx"
docker build -t deeppresenter-sandbox -f "$DEEP_PRESENTER/deeppresenter/docker/SandBox.Dockerfile" "$DEEP_PRESENTER"
for DATASET in presentbench slidesgen; do
  "$PRESENTER_PY" -m examples.ppt_benchmarks.run_deeppresenter "$WORK/cases" "$WORK/deeppresenter-$DATASET" \
    --checkout "$DEEP_PRESENTER" --dataset "$DATASET"
done
```

The compatibility patch handles multipart MCP results and preserves the image MIME type. If the upstream sandbox image is already provisioned, use that local image tagged `deeppresenter-sandbox`. The host needs Chromium system dependencies when using Playwright on Linux.

```bash
git clone --filter=blob:none https://github.com/sxswz213/DeepSlides.git "$DEEP_SLIDES"
git -C "$DEEP_SLIDES" checkout --detach 40240e5847f18c61ea893200597deb0b6f3a41c1
python3.12 -m venv "$WORK/slides-venv"
SLIDES_PY="$WORK/slides-venv/bin/python"
"$SLIDES_PY" -m pip install -e "$DEEP_SLIDES" -e "$DEEP_SLIDES/pptx_tools" \
  langchain==1.5.2 langchain-openai==1.8.5 langgraph==1.2.24 langgraph-checkpoint-sqlite==3.1.3 python-pptx==1.0.2
docker build -t ppt-benchmarks-deepslides -f examples/ppt_benchmarks/deepslides.Dockerfile "$DEEP_SLIDES"
for DATASET in presentbench slidesgen; do
  "$SLIDES_PY" -m examples.ppt_benchmarks.run_deepslides "$WORK/cases" "$WORK/deepslides-$DATASET" \
    --dataset "$DATASET"
done
```

`--dataset presentbench` selects Alphabet 2024 Q1. `--dataset slidesgen` selects 50 Million Steps. DeepPresenter receives the original prompt and PDF attachment. DeepSlides receives page-numbered PDF text and queries the supplied source in its presentation graph. The DeepSlides planner uses durations of 22 minutes for Alphabet and 11 minutes for 50 Million Steps. The two methods use offline containers for generated code. Their output directories contain `slides.pptx`.

### Use the signed-in Codex backend

Install Codex CLI and sign in with ChatGPT using `codex login`. Check the session with `codex login status`. Add `--backend codex` to either runner. This path uses the signed-in session and `gpt-6.2-sol` at `low` reasoning effort. It requires no model API key and uses the account's Codex allowance. It requires a Codex CLI version with App Server dynamic tools. [Codex authentication](https://learn.chatgpt.com/docs/auth) · [Codex App Server](https://learn.chatgpt.com/docs/app-server)

With the two upstream checkouts, environments and containers installed as above, run any of the four combinations. For example:

```bash
"$PRESENTER_PY" -m examples.ppt_benchmarks.run_deeppresenter "$WORK/cases" "$WORK/deeppresenter-codex-presentbench" \
  --checkout "$DEEP_PRESENTER" --dataset presentbench --backend codex
"$SLIDES_PY" -m examples.ppt_benchmarks.run_deepslides "$WORK/cases" "$WORK/deepslides-codex-slidesgen" \
  --dataset slidesgen --backend codex
```

Switch `--dataset` and pick a fresh output directory to run the other task with either method. If DeepPresenter's Python Docker client uses a non-default Docker context, set `DOCKER_HOST` to that context's endpoint before running it.

A loopback adapter translates the methods' existing model requests into Codex App Server turns. The Python workflows still execute their own tools and export the slides. Each run records the selected backend and model in `result.json`; App Server diagnostics are saved under the sibling `<output>-codex` directory.

## 4. Score the generated decks

See [PPT benchmark scoring with Codex](scoring/README.md) to apply both paper rubrics and the matching dataset rubric to a PPTX. It uses the same ChatGPT login as `--backend codex` and records cache usage for each judge call.
