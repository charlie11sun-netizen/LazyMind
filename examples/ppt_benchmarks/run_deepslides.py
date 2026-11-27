"""Connect the public benchmark packet to DeepSlides' presentation graph."""

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace


from .cases import case_files
from .generation import EFFORT, MODEL, connection


TOPIC = '50 Million Steps Campaign Toolkit'


async def run(case_root, output, sandbox_image):
    source_prompt, material = case_files('steps', case_root)
    base, token = connection()
    output.mkdir(parents=True)
    tmp = output / 'tmp'
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.chdir(output)

    def sandbox_run(command, **kwargs):
        executable = {'python': 'python', 'python3': 'python', 'true': '/usr/bin/soffice'}[Path(command[0]).name]
        args = ['docker', 'run', '--rm', '--network', 'none', '--cap-drop=ALL',
                '--security-opt', 'no-new-privileges', '--volume', f'{output}:{output}',
                '--workdir', str(output), '--entrypoint', executable, sandbox_image, *command[1:]]
        return subprocess.run(args, **kwargs)

    for var in ('OPENAI_BASE_URL', 'OPENAI_API_BASE', 'OPENAI_API_BASE2', 'CODER_API_BASE', 'DESIGNER_API_BASE'):
        os.environ[var] = base
    for var in ('OPENAI_API_KEY', 'OPENAI_API_KEY2'):
        os.environ[var] = token
    for role in ('PLANNER', 'WRITER', 'DESIGNER', 'CODER'):
        os.environ[f'{role}_MODEL'] = MODEL
        os.environ[f'{role}_PROVIDER'] = 'openai'
    os.environ['SOFFICE_PATH'] = '/usr/bin/true'
    os.environ['SAVES_ROOT'] = str(output / 'output')
    os.environ['LANGSMITH_TRACING'] = 'false'

    import pymupdf
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from open_deep_research import graph as upstream
    from open_deep_research.state import ReportState

    class FixedModel(ChatOpenAI):
        def with_structured_output(self, schema=None, **kwargs):
            kwargs.setdefault('method', 'function_calling')
            return super().with_structured_output(schema, **kwargs)

    def pinned_model(**kwargs):
        if kwargs.get('model') != MODEL:
            raise ValueError('Unexpected model requested by DeepSlides')
        return FixedModel(model=MODEL, base_url=base, api_key=token, timeout=250, max_retries=0,
                          reasoning_effort=EFFORT)

    upstream.init_chat_model = pinned_model
    upstream.subprocess = SimpleNamespace(run=sandbox_run, TimeoutExpired=subprocess.TimeoutExpired)
    prompt = source_prompt.read_text(encoding='utf-8')
    with pymupdf.open(material) as pdf:
        pages = [f'## Source page {page.number + 1}\n\n{page.get_text(sort=True)}' for page in pdf]
    report = prompt + '\n\n# Source document\n\n' + '\n\n'.join(pages)
    (output / 'request.txt').write_text(prompt)
    (output / 'source.md').write_text(report)
    terms = [set(re.findall(r'[\w#-]{3,}', page.lower())) for page in pages]

    async def source_search(_backend, queries, _parameters):
        query = set(re.findall(r'[\w#-]{3,}', ' '.join(queries).lower()))
        ranked = sorted(range(len(pages)), key=lambda i: -len(terms[i] & query))[:5]
        return '\n\n'.join(pages[i] for i in ranked)

    upstream.select_and_execute_search = source_search
    (output / 'output' / TOPIC).mkdir(parents=True, exist_ok=True)
    initial = {
        'topic': TOPIC, 'final_report': report, 'presentation_minutes': 11,
        'style': 'Follow the campaign brief and use only the supplied source material. ' + prompt.strip(),
    }
    graph = StateGraph(ReportState)
    pipeline = ('generate_ppt_outline', 'generate_ppt_styles', 'generate_cover_slide',
                'generate_section_cover_slides', 'generate_end_slide', 'manage_ppt_templates')
    for name in pipeline:
        graph.add_node(name, getattr(upstream, name))
    graph.add_node('generate_ppt_sections', upstream.ppt_section_subgraph)
    graph.add_node('compile_ppt', upstream.compile_ppt)
    graph.add_edge(START, pipeline[0])
    for left, right in zip(pipeline, pipeline[1:]):
        graph.add_edge(left, right)
    graph.add_edge('generate_ppt_sections', 'compile_ppt')
    graph.add_edge('compile_ppt', END)
    started = datetime.now().astimezone().isoformat()
    final = None
    async with AsyncSqliteSaver.from_conn_string(str(output / 'checkpoint.sqlite')) as checkpoint:
        runnable = graph.compile(checkpointer=checkpoint)
        config = {'configurable': {'thread_id': 'slidesgen-50-million-steps'},
                  'recursion_limit': 100, 'max_concurrency': 2}
        async for update in runnable.astream(initial, config=config, stream_mode='updates'):
            print(datetime.now().isoformat(timespec='seconds'), 'finished stages', *update, flush=True)
            if build := update.get('compile_ppt'):
                final = Path(build['final_ppt_path'])
    if final is None or not final.is_file():
        raise RuntimeError('DeepSlides finished without a final PPTX')
    target = output / 'slides.pptx'
    shutil.copyfile(final, target)
    (output / 'result.json').write_text(json.dumps({'output': str(target), 'started_at': started,
      'finished_at': datetime.now().astimezone().isoformat(), 'model': MODEL, 'effort': EFFORT}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--sandbox-image', default='ppt-benchmarks-deepslides')
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args.cases, args.output.resolve(), args.sandbox_image))


if __name__ == '__main__':
    main()
