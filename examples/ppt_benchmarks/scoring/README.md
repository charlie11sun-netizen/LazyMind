# Score PPT benchmark outputs with Codex

Run from the LazyMind checkout root. This scores one PPTX from any generation workflow on one of the two pinned cases in [the PPT benchmark example](../README.md).

| Score | Scope | Implementation |
| --- | --- | --- |
| DeepPresenter | Both cases | Upstream PPTEval prompts describe each slide's content and style and score those descriptions on a 1–5 scale. Local rules check English and 16:9, plus 15–20 slides for Alphabet. Language detection reads the exported PDF text. Inter-deck Vendi diversity requires a set of decks. |
| DeepSlides | Both cases | A rubric adapted from Appendix C.2 scores clarity, coherence, layout, hierarchy, and color scheme on each slide. Rendering success and image-based balance are local metrics. Balance uses 100 DPI, λ = 0.5 and σ = 2 pixels. The human preference study is outside this runner. |
| PresentBench | Alphabet 2024 Q1 | The upstream checklist, local checks, and weighted aggregation. Codex returns one yes/no verdict per model-judged item. Material-dependent checks receive both the deck and the source PDF. |
| SlidesGen-Bench | 50 Million Steps | The upstream prompts extract each slide and answer the ten QuizBank questions. Local code compares the choices with the published answer key. Optional visual metrics use the upstream calculator. The runner exports native PowerPoint object counts for manual PEI review. |

The judge is fixed to `gpt-6.2-sol` with `low` reasoning for every model-based component. Scores use this judge consistently across decks. The papers and benchmarks may have used different judges and protocols for their published results.

## Setup

Use Python 3.12, Poppler (`pdftoppm` and `pdfinfo`), and the Codex CLI with App Server fork and turn history support. Install LibreOffice (`soffice`) if you need to render the PPTX. Use the same ChatGPT login as the generation backend. Run `codex login` to sign in and `codex login status` to verify the session. The runner requires that login and uses the account's Codex usage allowance.

Use the same `WORK`, `DEEP_PRESENTER`, and `PRESENTER_PY` as in the [generation example](../README.md) with the DeepPresenter checkout installed. Its language model is fetched on first use. The language model can be preloaded with `"$PRESENTER_PY" -c 'from pptagent.model_utils import _get_lid_model; _get_lid_model()'`.

```bash
"$PRESENTER_PY" -m pip install -r examples/ppt_benchmarks/scoring/requirements.txt huggingface_hub

git clone --filter=blob:none https://github.com/PresentBench/PresentBench.git "$WORK/PresentBench"
git -C "$WORK/PresentBench" checkout --detach e70ff01da962274e1e3cc03f77ec435ad66c5eb6

GIT_LFS_SKIP_SMUDGE=1 git clone --filter=blob:none https://github.com/YunqiaoYang/SlidesGen-Bench.git "$WORK/SlidesGen-Bench"
GIT_LFS_SKIP_SMUDGE=1 git -C "$WORK/SlidesGen-Bench" checkout --detach b202e60c39e19c5f9da57029fe53ff189e6642b0

WORK="$WORK" "$PRESENTER_PY" - <<'PY'
import os
from pathlib import Path

from huggingface_hub import hf_hub_download

for filename in (
    "economics/common_judge_prompt.json",
    "economics/judge_weights.yaml",
    "economics/Alphabet_Investor_Relations/2024q1/generation_task/judge_prompt.json",
):
    hf_hub_download(
        "PresentBench/PresentBench",
        filename,
        repo_type="dataset",
        revision="3c6e01f563165d3a364a459f9777cd7286f42cd8",
        local_dir=Path(os.environ["WORK"]) / "presentbench-data",
    )
PY
```

## Run

```bash
"$PRESENTER_PY" -m examples.ppt_benchmarks.scoring \
  --dataset presentbench --cases "$WORK/cases" \
  --pptx "$WORK/deeppresenter-codex-presentbench/slides.pptx" \
  --deeppresenter "$DEEP_PRESENTER" --dataset-code "$WORK/PresentBench" \
  --presentbench-data "$WORK/presentbench-data" \
  --out "$WORK/scores-deeppresenter-alphabet"

"$PRESENTER_PY" -m examples.ppt_benchmarks.scoring \
  --dataset slidesgen --cases "$WORK/cases" \
  --pptx "$WORK/deepslides-codex-slidesgen/slides.pptx" \
  --deeppresenter "$DEEP_PRESENTER" --dataset-code "$WORK/SlidesGen-Bench" \
  --out "$WORK/scores-deepslides-steps"
```

Use `--pdf` if you already have the PDF exported from that exact PPTX. Use `--prepare-only` to render and run the deterministic checks before starting Codex. Use a separate output directory per deck.

For SlidesGen-Bench's visual metrics, install PaddleOCR, the PaddlePaddle runtime for your platform, OpenCV, SciPy, scikit-image and pyrtools in the scoring environment. Download [PP-DocLayout_plus-L](https://huggingface.co/PaddlePaddle/PP-DocLayout_plus-L) and pass its directory as `--aesthetics-model`. The same directory can be scored once and the visual metrics attached later without repeating judge calls.

## Cache and results

PresentBench loads shared evidence into one Codex turn for each material group. Each check forks from that turn and gets only its own question. Per-slide DeepPresenter, DeepSlides, and SlidesGen-Bench tasks fork from a shared rubric turn. The SlidesGen-Bench QuizBank judge reads this deck's extracted content in one fresh turn. The correct answers are used locally after that turn.

`checkpoint.json` stores each completed check, plus usage for warm-ups and judging. Re-run the same command to resume. The runner verifies the deck, PDF, and scoring inputs before reusing any responses. A resumed group loads its evidence into a fresh thread. `scores.json` reports input, cached input, and output tokens separately for scoring and warm-up, as reported by Codex. Cached input varies by request. Keep the output directories private because they contain deck renders and judge responses.
