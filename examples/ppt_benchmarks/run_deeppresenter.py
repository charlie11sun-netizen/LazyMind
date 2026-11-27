"""Run the pinned DeepPresenter workflow on either benchmark task."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from .cases import DATASET_CASES, case_files
from .generation import BACKENDS, EFFORT, MODEL, connection, model_session


async def run(case_root, output, checkout, dataset='presentbench', *, target=None, backend='api'):
    prompt, attachment = case_files(DATASET_CASES[dataset], case_root)
    model_name, base, key = target if target is not None else (MODEL, *connection())
    output.mkdir(parents=True)
    os.environ['DEEPPRESENTER_WORKSPACE_BASE'] = str(output)

    from deeppresenter.main import AgentLoop
    from deeppresenter.utils.config import DeepPresenterConfig
    from deeppresenter.utils.typings import ConvertType, InputRequest
    from deeppresenter.utils.webview import PlaywrightConverter

    specs = json.loads((checkout / 'deeppresenter/mcp.json.example').read_text())
    for spec in specs:
        if spec['command'] == 'python3':
            spec['command'] = sys.executable
        if spec['name'] == 'any2markdown':
            spec['env'] = {'MINERU_API_KEY': '', 'MINERU_API_URL': ''}
        if spec['name'] == 'deeppresenter' and 'PLAYWRIGHT_BROWSERS_PATH' in os.environ:
            spec['env'] = {'PLAYWRIGHT_BROWSERS_PATH': os.environ['PLAYWRIGHT_BROWSERS_PATH']}
        if spec['name'] == 'sandbox':
            spec['args'][1:1] = ['--network', 'none']
    model = dict(model=model_name, base_url=base, api_key=key, is_multimodal=True,
                 sampling_parameters={'reasoning_effort': EFFORT},
                 client_kwargs={'max_retries': 0, 'timeout': 250})
    request = InputRequest(instruction=prompt.read_text(encoding='utf-8'), attachments=[str(attachment)],
                           convert_type=ConvertType.DEEPPRESENTER, enable_planner=False)
    work = output / f'deeppresenter-{uuid4().hex[:8]}'
    work.mkdir()
    final = None
    with TemporaryDirectory(prefix='ppt-deeppresenter-') as private:
        config_path = Path(private) / 'config.json'
        mcp_path = Path(private) / 'mcp.json'
        mcp_path.write_text(json.dumps([s for s in specs if s['name'] not in {'search', 'pptagent'}]))
        config = dict(file_path=str(config_path), mcp_config_file=str(mcp_path), offline_mode=True,
                      research_agent=model, design_agent=model, long_context_model=model, vision_model=model)
        if backend == 'codex':
            # Leave room for Codex's instructions and registered tools in the reported context.
            config['context_window'] = 120_000
        config_path.write_text(json.dumps(config))
        loop = AgentLoop(DeepPresenterConfig(**config), workspace=work)
        try:
            async for event in loop.run(request):
                if isinstance(event, Path):
                    final = event
                print('generation event', type(event).__name__, flush=True)
        finally:
            await PlaywrightConverter.shutdown()
    if final is None or final.suffix != '.pptx' or not final.is_file():
        raise RuntimeError('DeepPresenter finished without an editable PPTX')
    shutil.copyfile(final, output / 'slides.pptx')
    if final.with_suffix('.pdf').is_file():
        shutil.copyfile(final.with_suffix('.pdf'), output / 'slides.pdf')
    (output / 'result.json').write_text(json.dumps({'dataset': dataset, 'backend': backend,
        'model': model_name, 'effort': EFFORT, 'output': str(output / 'slides.pptx')}, indent=2) + '\n')
    print(output / 'slides.pptx')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--dataset', choices=DATASET_CASES, default='presentbench')
    parser.add_argument('--backend', choices=BACKENDS, default='api')
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    with model_session(args.backend, output) as target:
        asyncio.run(run(args.cases, output, args.checkout.resolve(), args.dataset,
                        target=target, backend=args.backend))


if __name__ == '__main__':
    main()
