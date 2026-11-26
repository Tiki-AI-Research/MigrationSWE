"""Keep failing test assertions in short diagnostics and retain bounded logs."""
from __future__ import annotations

import json
import sys

LOG_LIMIT = 1024 * 1024


def summarize(text, limit=4000):
    important, ordinary = [], []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get('Action') in {'output', 'fail'}:
            output = event.get('Output', '')
            if event.get('Action') == 'fail' and event.get('Test'):
                important.append('FAILED ' + str(event['Test']))
            if output:
                ordinary.append(str(output).rstrip())
                if any(word in output for word in ['.go:', 'panic:', 'fatal error:', 'HARBOR_UNSUPPORTED:', '--- FAIL:']):
                    important.append(str(output).rstrip())
        else:
            ordinary.append(line)
    if len(text) <= limit:
        return text
    selected = '\n'.join(dict.fromkeys(important))
    if selected:
        # Assertions come before package summaries, even for long runner logs.
        return (selected[:limit - 500] + '\n... log tail ...\n' + '\n'.join(ordinary)[-450:])[:limit]
    half = (limit - 30) // 2
    return text[:half] + '\n... log omitted ...\n' + text[-half:]


def log_record(text):
    return {'text': text[:LOG_LIMIT], 'truncated': len(text) > LOG_LIMIT}


def main():
    text = sys.stdin.read()
    if len(sys.argv) == 2 and sys.argv[1] == 'summary':
        print(summarize(text))
        return
    oid, status = sys.argv[1:3]
    print(json.dumps({'observation_id': oid, 'status': status,
                      'evidence': summarize(text), 'diagnostic_log': log_record(text)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
