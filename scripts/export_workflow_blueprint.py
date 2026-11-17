"""Export any saved Core workflow for reuse in another conversation; no model calls."""
import argparse
import json
from pathlib import Path

from mewcode.expert_runs import ExpertRunStore
from mewcode.workflow_core.blueprint import documents, pretty, summary_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-store', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--selection', choices=['applied', 'draft'], default='applied')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    names = ['construction.md', 'blueprint.json', 'workflow.json', 'structure.pseudo.txt', 'summary.md']
    if any((output / name).exists() for name in names):
        parser.error('目标目录已有导出文件，请使用新目录，避免覆盖。')
    store = ExpertRunStore(args.run_store, storage='sqlite')
    packet = store.read_workflow_blueprint(args.run_id, args.selection)
    rendered = documents(packet)
    values = [rendered['documents']['brief']['text'], rendered['documents']['json']['text'],
              pretty(packet['workflow']), rendered['documents']['pseudocode']['text'], summary_markdown(packet['summary'])]
    output.mkdir(parents=True, exist_ok=True)
    for name, text in zip(names, values):
        with (output / name).open('x', encoding='utf-8') as stream:
            stream.write(text)
    print(json.dumps(dict(output=str(output), nodes=rendered['node_count'], edges=rendered['edge_count'],
                          source=packet['source'], files=names), ensure_ascii=False))


if __name__ == '__main__':
    main()
