# Generate a PresentBench PPT with LazyMind

This example sends one published task and its source files to a new native `ppt-workflow` conversation. It downloads the selected native artifact or invokes the same editable export interface as the workflow panel.

## Prepare a case

Use Python 3.10 or newer and an authenticated LazyMind test deployment. Keep the session JSON out of the repository and pass it with `--auth-file`. Both a raw authentication response and its unwrapped `data` object work.

Pin the [PresentBench dataset](https://huggingface.co/datasets/PresentBench/PresentBench/tree/3c6e01f563165d3a364a459f9777cd7286f42cd8) to `3c6e01f563165d3a364a459f9777cd7286f42cd8`. Each case directory needs `material*` and `generation_task/instructions.md`. The runner sends these files to the workflow.

Set `CASE`, `RUN`, `BASE_URL`, `AUTH_FILE`, and `MODEL_ID` for the case, deployment, and chosen generation model. Run commands from the LazyMind checkout root.

```bash
python -m examples.presentbench.run prepare "$CASE" "$RUN"
python -m examples.presentbench.run submit "$RUN" --base-url "$BASE_URL" --auth-file "$AUTH_FILE" --model-id "$MODEL_ID"
python -m examples.presentbench.run status "$RUN" --base-url "$BASE_URL" --auth-file "$AUTH_FILE"
```

`prepare --delivery` sets the delivery instruction appended to the original prompt. Record any added constraints, such as a page count inside the permitted range. `submit --model-id` pins the conversation model. The run records the model reported by the submission stream, and backend stages may use their workflow-specific model settings.

If the deployment requests clarification, the `*.private.questions.json` file contains its question IDs and choices. Pass one `--answer 'question_id=value'` per question to `run clarify` along with the same run directory and authentication arguments. Use the benchmark's requested values where specified.

## Export

```bash
python -m examples.presentbench.run export "$RUN" --base-url "$BASE_URL" --auth-file "$AUTH_FILE"
```

For HTML slides, the deployment must provide editable export. If the dependency is unavailable on the server, `--local-exporter /path/to/html_to_pptx.mjs` uses the nominated LazyMind exporter with its installed Node and Chromium dependencies. The run records which path produced the file, its SHA-256, and the number of slides, text shapes, tables, charts, and pictures.

The workflow panel can publish a finished deck before the parent conversation closes the session. The exporter accepts that published deck once `generate_ppt` has succeeded and records the session status at export.
