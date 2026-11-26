"""Public CLI assertions, shared by differential and P2P observations.

Expectations come from the instruction and the existing public command formats.
Each mode owns isolated state. Exit zero alone is never a passing receipt.
"""
from collections import Counter
import hashlib
from itertools import groupby
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from cli_contract import CLI, require, record, list_records, check_database


def fixtures(mode):
    if mode in {'classify', 'regex-no-engine', 'native-classify'}:
        texts = ['CVE-2024-1234 https://github.com/acme/tool tutorial just shipped new paper hot take buy now',
                 'https://github.com/acme/tool tutorial just shipped new paper hot take buy now',
                 'tutorial just shipped new paper hot take buy now',
                 'just shipped new paper hot take buy now', 'new paper hot take buy now',
                 'hot take buy now', 'buy now', 'garden flowers']
        return [record(i + 1, text) for i, text in enumerate(texts)]
    if mode in {'engine-resolution', 'llm'}:
        return [record('1', 'garden flowers')]
    if mode in {'search', 'bm25'}:
        return [record(i, 'machine ' * (1 + i % 3) + 'garden', 'alice' if i % 2 else 'bob',
                       '2024-01-%02dT12:00:00Z' % i) for i in range(1, 26)]
    return list_records()


def rows(cli, *args):
    value = cli.json(*args)
    require(isinstance(value, list) and all(isinstance(row, dict) and 'id' in row for row in value),
            'expected bookmark JSON array: ' + ' '.join(args))
    identifiers = [str(row['id']) for row in value]
    require(len(set(identifiers)) == len(identifiers), 'duplicate bookmark ids')
    return value


def check_paths(cli, records):
    # Give each scenario AND invocation a fresh home. A CLI may create its
    # selected directories; that must not contaminate a later legacy fixture.
    for case in ['overrides', 'data-only', 'default', 'legacy', 'canonical']:
        for command in ['path', 'paths']:
            home = cli.root / 'path-homes' / case / command
            home.mkdir(parents=True)
            cli.env['HOME'] = str(home)
            for key in ['FT_DATA_DIR','FT_LIBRARY_DIR','FT_COMMANDS_DIR']:
                cli.env.pop(key, None)
            canonical = home / '.fieldtheory'
            legacy = home / '.ft-bookmarks'
            if case in {'legacy','canonical'}:
                (legacy / 'md').mkdir(parents=True)
                (canonical / 'commands').mkdir(parents=True)
            if case == 'canonical':
                (canonical / 'bookmarks').mkdir()
                (canonical / 'library/Commands').mkdir(parents=True)
            data = legacy if case == 'legacy' else canonical / 'bookmarks'
            library = legacy / 'md' if case == 'legacy' else canonical / 'library'
            commands = canonical / 'commands' if case == 'legacy' else canonical / 'library/Commands'
            if case in {'overrides','data-only'}:
                cli.env['FT_DATA_DIR'] = str(cli.data)
                data, library = cli.data, cli.data / 'md'
                commands = library / 'Commands'
            if case == 'overrides':
                library, commands = cli.root / 'library', cli.root / 'commands'
                cli.env.update(FT_LIBRARY_DIR=str(library),FT_COMMANDS_DIR=str(commands))
            if command == 'path':
                require(cli.run('path').stdout.strip() == str(data), 'path did not print active data directory')
                continue
            report = cli.json('paths')
            require(isinstance(report, dict), 'paths must return an object')
            expected = {'bookmarksDir': str(data), 'libraryDir': str(library), 'commandsDir': str(commands),
                        'bookmarksCachePath': str(data / 'bookmarks.jsonl'),
                        'bookmarksIndexPath': str(data / 'bookmarks.db')}
            require(all(report.get('active', {}).get(key) == value for key, value in expected.items()),
                    'incorrect active paths: ' + repr(report.get('active')))
            want = {'bookmarksDir': cli.env.get('FT_DATA_DIR', str(canonical / 'bookmarks')),
                    'libraryDir': cli.env.get('FT_LIBRARY_DIR', str(canonical / 'library')),
                    'commandsDir': cli.env.get('FT_COMMANDS_DIR', str(Path(cli.env.get('FT_LIBRARY_DIR', str(canonical / 'library'))) / 'Commands'))}
            require(all(report.get('canonical', {}).get(key) == value for key, value in want.items()), 'incorrect canonical paths')
            require(all(report.get('env', {}).get(key) == cli.env.get(key) for key in ['FT_DATA_DIR','FT_LIBRARY_DIR','FT_COMMANDS_DIR']), 'incorrect environment paths')
            require(report.get('legacy', {}).get('bookmarksDir') == str(legacy) and
                    report.get('legacy', {}).get('exists') == legacy.exists(), 'incorrect legacy path report')


def check_search_ranking(cli, records):
    check_database(cli, records)
    with cli.database() as db:
        expected = [str(row[0]) for row in db.execute("SELECT b.id FROM bookmarks_fts JOIN bookmarks b ON b.rowid=bookmarks_fts.rowid WHERE bookmarks_fts MATCH 'machine' ORDER BY bm25(bookmarks_fts,5.0,1.0,1.0,3.0) ASC")]
    all_rows = rows(cli, 'search', 'machine', '--limit', '100')
    got = [str(row['id']) for row in all_rows]
    # Equal BM25 scores may be returned in either order.
    require(set(got) == set(expected) == {r['id'] for r in records}, 'search lost matching fixture rows')
    with cli.database() as db:
        scores = {str(identifier): score for identifier, score in db.execute("SELECT b.id,bm25(bookmarks_fts,5.0,1.0,1.0,3.0) FROM bookmarks_fts JOIN bookmarks b ON b.rowid=bookmarks_fts.rowid WHERE bookmarks_fts MATCH 'machine'")}
    require([scores[i] for i in got] == sorted(scores.values()), 'search is not ranked by the required BM25 weights')
    for options, limit in [((), 20), (('--limit', '3'), 3)]:
        limited = [str(row['id']) for row in rows(cli, 'search', 'machine', *options)]
        require(len(limited) == limit, 'search default limit must be 20' if not options else 'search ignored limit')
        require(all(identifier in scores for identifier in limited) and
                [scores[identifier] for identifier in limited] == sorted(scores.values())[:limit],
                'search limit did not select the best BM25 matches')


def check_search(cli, records):
    check_search_ranking(cli, records)
    for options, wanted in [(('--author','alice'), {r['id'] for r in records if r['authorHandle'] == 'alice'}),
                            # Existing SQL compares posted_at with the given
                            # bound; Jan 12 at noon is after Jan 12 midnight.
                            (('--after','2024-01-08','--before','2024-01-12'), {'8','9','10','11'})]:
        actual = {str(r['id']) for r in rows(cli, 'search', 'machine', '--limit','100', *options)}
        require(actual == wanted, 'search filters ignored: ' + repr(options))
    require(rows(cli, 'search', 'zzznomatchzzz') == [], 'empty search returned rows')
    # The original human CLI prints a logo before the result body and may
    # include update notices. Preserve that chrome, including ANSI styles,
    # while still requiring the exact empty-result message on its own line.
    output = re.sub(r'\x1b\[[0-9;]*m', '', cli.run('search', 'zzznomatchzzz').stdout)
    require('No results found.' in [line.strip() for line in output.splitlines()], 'incorrect empty result message')
    # Punctuation and operator tokens must not broaden a literal query or
    # surface raw FTS syntax errors. This query has no matching literal terms.
    require(rows(cli, 'search', 'machine OR garden') == [], 'FTS OR operator input not sanitized into literal terms')
    require(rows(cli, 'search', 'machine*'), 'literal punctuation query lost matching token')
    check_search_weights(cli)


def check_bm25(cli, records):
    # Share the independent score, limit and field-weight postconditions with
    # the full search probe. Insertion order alone must never prove ranking.
    check_search_ranking(cli, records)
    # The previous shell fixture also exercised a nonempty multiword query.
    # Keep that coverage when replacing its insertion-order-only assertion.
    with cli.database() as db:
        scores = {str(identifier): score for identifier, score in db.execute("SELECT b.id,bm25(bookmarks_fts,5.0,1.0,1.0,3.0) FROM bookmarks_fts JOIN bookmarks b ON b.rowid=bookmarks_fts.rowid WHERE bookmarks_fts MATCH 'machine garden'")}
    got = [str(row['id']) for row in rows(cli, 'search', 'machine garden', '--limit', '100')]
    require(set(got) == set(scores) == {row['id'] for row in records}, 'multiword search lost matching fixture rows')
    require([scores[identifier] for identifier in got] == sorted(scores.values()), 'multiword search is not ranked by the required BM25 weights')
    check_search_weights(cli)


def check_search_weights(cli):
    # Equal-length documents with hits in different FTS columns distinguish
    # the four weights. Text-only hits cannot test relative field weights.
    fixtures = [('1','harborweight anchor','alpha','beta','gamma delta'),
                ('2','anchor bravo','harborweight','beta','gamma delta'),
                ('3','anchor bravo','alpha','harborweight','gamma delta'),
                ('4','anchor bravo','alpha','beta','harborweight delta')]
    with cli.database() as db:
        db.execute("UPDATE bookmarks SET text='anchor bravo',author_handle='alpha',author_name='beta',article_text='gamma delta'")
        for identifier, text, handle, name, article in fixtures:
            db.execute('UPDATE bookmarks SET text=?,author_handle=?,author_name=?,article_text=? WHERE id=?',
                       (text,handle,name,article,identifier))
        db.execute("INSERT INTO bookmarks_fts(bookmarks_fts) VALUES('rebuild')")
        scores = {str(identifier): score for identifier, score in db.execute("SELECT b.id,bm25(bookmarks_fts,5.0,1.0,1.0,3.0) FROM bookmarks_fts JOIN bookmarks b ON b.rowid=bookmarks_fts.rowid WHERE bookmarks_fts MATCH 'harborweight'")}
    got = [str(row['id']) for row in rows(cli,'search','harborweight','--limit','100')]
    require(set(got) == set(scores) == {'1','2','3','4'}, 'search did not include text/handle/name/article matches')
    require([scores[identifier] for identifier in got] == sorted(scores.values()), 'incorrect relative BM25 field weights or order')


def check_show(cli, records):
    output = cli.run('show', '1').stdout
    require(all(str(records[0][key]) in output for key in ['id', 'text', 'authorHandle', 'url']), 'show omitted bookmark details')
    missing = cli.run('show', 'zzznomatchzzz', ok=False)
    require(re.search(r'not.found|no bookmark', missing.stdout + missing.stderr, re.I), 'show needs a readable not-found message')


def check_distribution(cli, records, mode):
    check_database(cli, records)
    with cli.database() as db:
        db.execute("UPDATE bookmarks SET primary_category='unclassified',primary_domain=NULL,folder_names='[]'")
        for identifier, category, domain, folders in [('1','security','ai',['Research','Security']),
                                                       ('2','security','ai',['Research']), ('3','tool','science',['Research'])]:
            db.execute('UPDATE bookmarks SET primary_category=?,primary_domain=?,folder_names=? WHERE id=?',
                       (category, domain, json.dumps(folders), identifier))
    expected = {'categories': {'security': 2, 'tool': 1}, 'domains': {'ai': 2, 'science': 1},
                'folders': {'Research': 3, 'Security': 1, '(untagged)': len(records) - 3}}[mode]
    output = cli.run(mode).stdout
    # These commands document human tables, not --json. Accept padding, colon
    # delimiters and optional percentages without requiring exact decoration.
    found = []
    for line in output.splitlines():
        match = re.match(r'^\s*(.+?)(?:\s*:\s*|\s+)(\d+)(?:\s|$)', line)
        if match and not match[1].lower().rstrip(':').startswith('total'):
            found.append((match[1].rstrip(':'), int(match[2])))
    require(dict(found) == expected and len(found) == len(expected), mode + ' distribution has missing/incorrect counts: ' + output)
    ranked = [count for name, count in found if name != '(untagged)']
    require(ranked == sorted(ranked, reverse=True), mode + ' distribution not sorted by count')
    if mode == 'categories':
        require('unclassified' not in output.lower(), 'categories included unclassified placeholder')


def check_classify(cli, records):
    result = cli.run('classify', '--regex')
    check_database(cli, records)
    priority = ['security','tool','technique','launch','research','opinion','commerce']
    with cli.database() as db:
        actual = {str(identifier): (categories, primary) for identifier, categories, primary in db.execute('SELECT id,categories,primary_category FROM bookmarks')}
    for index, primary in enumerate(priority + ['unclassified']):
        categories, got = actual[str(index + 1)]
        require(got == primary, 'incorrect regex primary for ' + str(index + 1) + ': ' + repr(got))
        parts = set(categories.split(',')) if categories else set()
        require(parts == set(priority[index:]), 'incorrect regex categories for ' + str(index + 1))
    summary = result.stdout + result.stderr
    require(re.search(r'\bclassified\s*:?\s*7\b|\b7\s+classified\b', summary, re.I) and
            re.search(r'\b1\s+unclassified\b|\bunclassified\s*:?\s*1\b', summary, re.I),
            'classification summary did not report classified/unclassified counts')


def check_regex_no_engine(cli, records):
    directory = isolated_path(cli, 'no-engines', {'claude','codex'})
    cli.env['PATH'] = str(directory)
    check_classify(cli, records)


def check_native(cli, records, classify=False):
    directory = isolated_path(cli, 'native-path', {'node','nodejs','tsx','ts-node','claude','codex'})
    cli.env['PATH'] = str(directory)
    cli.run('index')
    check_database(cli, records)
    require(cli.run('path').stdout.strip() == str(cli.data), 'native path returned incorrect data root')
    if classify:
        check_classify(cli, records)


def check_refresh(cli, records):
    preserved = {'categories':'security,tool','primary_category':'security','github_urls':'https://github.com/acme/tool',
                 'domains':'ai','primary_domain':'ai','quoted_tweet_json':'{"text":"saved quote"}',
                 'article_title':'Saved title','article_text':'Saved article','article_site':'example.com',
                 'enriched_at':'2024-05-01T00:00:00Z','folder_ids':'["db-folder"]','folder_names':'["DB Folder"]'}
    check_database(cli, records)
    with cli.database() as db:
        db.execute('UPDATE bookmarks SET '+','.join(name+'=?' for name in preserved)+" WHERE id='1'", list(preserved.values()))
    updated = [dict(row) for row in records] + [record('999','new garden')]
    updated[0].update(text='updated garden',folderIds=['cache-folder'],folderNames=['Cache Folder'])
    (cli.data/'bookmarks.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in updated))
    for new in [1,0]:
        result = cli.run('index')
        require(re.search(r'\b'+str(new)+r'\s+new\b|\bnew\s*[:=]\s*'+str(new)+r'\b', result.stdout+result.stderr, re.I), 'index did not report newly added count')
        require(re.search(r'\b'+str(len(updated))+r'\b',result.stdout+result.stderr), 'index did not report total count')
        with cli.database() as db:
            row = db.execute('SELECT '+','.join(preserved)+",text FROM bookmarks WHERE id='1'").fetchone()
            require(db.execute('SELECT COUNT(*) FROM bookmarks').fetchone()[0] == len(updated), 'refresh lost or duplicated records')
        require(row is not None and tuple(row[:-1]) == tuple(preserved.values()), 'refresh overwrote stored classification/enrichment/folder columns')
        require(row[-1] == 'updated garden', 'refresh did not update cache text')
    cli.run('index','--force')
    with cli.database() as db:
        row = db.execute("SELECT categories,article_text,folder_names FROM bookmarks WHERE id='1'").fetchone()
    require(row and row[0] != preserved['categories'] and row[1] != preserved['article_text'] and
            json.loads(row[2]) == ['Cache Folder'], '--force did not rebuild state from cache')


def check_library(cli, records):
    root = cli.root / 'library'
    original, changed = '# Garden\n\nCONTROL_DOCUMENT_CONTENT\n', '# Garden\n\nCONTROL_CHANGED_CONTENT\n'
    target = root / 'notes/garden.md'
    cli.run('library','create','notes/garden.md','--stdin',input=original)
    require(target.is_file() and target.read_text() == original, 'library create did not persist content')
    require(target.stat().st_mode & 0o777 == 0o644, 'Markdown mode must be 0644')
    require(original.strip() in cli.run('library','show','notes/garden.md').stdout, 'library show lost content')
    for relative in ['.hidden/secret.md', 'Codex Context/sessions/internal.md']:
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# Hidden CONTROL_HIDDEN_MARKER\n')
    listing = cli.json('library','list')
    require('notes/garden.md' in json.dumps(listing) and 'CONTROL_HIDDEN_MARKER' not in json.dumps(listing)
            and 'secret.md' not in json.dumps(listing) and 'internal.md' not in json.dumps(listing), 'library listing omitted document or exposed hidden/session files')
    require('notes/garden.md' in json.dumps(cli.json('library','search','CONTROL_DOCUMENT_CONTENT')), 'library search did not find content')
    hidden_search = json.dumps(cli.json('library','search','CONTROL_HIDDEN_MARKER'))
    require(not any(marker in hidden_search for marker in ['CONTROL_HIDDEN_MARKER','secret.md','internal.md']),
            'library search exposed hidden/session files')
    for content, options in [('', ['--force']), (changed, []), (changed, ['--expected-sha256','deadbeef'])]:
        cli.run('library','update','notes/garden.md','--stdin',*options,input=content,ok=False)
        require(target.read_text() == original, 'rejected library update changed content')
    cli.run('library','update','notes/garden.md','--stdin','--expected-sha256',hashlib.sha256(original.encode()).hexdigest(),input=changed)
    require(target.read_text() == changed, 'library hash-guarded update failed')
    cli.run('library','update','notes/garden.md','--stdin','--force',input=original)
    require(target.read_text() == original, 'library force update failed')
    cli.run('library','rename','notes/garden.md','notes/renamed.md')
    renamed = root / 'notes/renamed.md'
    require(not target.exists() and renamed.is_file() and renamed.read_text() == original, 'library rename failed')
    sentinel = cli.root / 'outside.md'; sentinel.write_text('OUTSIDE_SENTINEL')
    cli.run('library','show',str(sentinel),ok=False)
    cli.run('library','create','../escape.md','--stdin',input=original,ok=False)
    cli.run('library','update','../outside.md','--stdin','--force',input=changed,ok=False)
    cli.run('library','rename','notes/renamed.md','../escape-rename.md',ok=False)
    cli.run('library','rename','notes/renamed.md','../outside.md',ok=False)
    cli.run('library','rename','../outside.md','notes/escaped.md',ok=False)
    cli.run('library','delete','../outside.md',ok=False)
    require(sentinel.read_text() == 'OUTSIDE_SENTINEL' and not (cli.root / 'escape.md').exists()
            and not (cli.root / 'escape-rename.md').exists(), 'library traversal changed outside files')
    require(renamed.is_file() and renamed.read_text() == original and not (root/'notes/escaped.md').exists(),
            'rejected library rename changed documents')
    cli.run('library','delete','notes/renamed.md')
    require(not renamed.exists(), 'library delete left source in place')
    trash_roots = [cli.root / 'home/.Trash', Path(cli.env['XDG_DATA_HOME']) / 'Trash/files']
    require(any(path.read_text() == original for trash in trash_roots if trash.exists() for path in trash.rglob('*') if path.is_file()), 'library delete did not preserve content in Trash')


VALID_COMMAND = '# Garden\n\nUse this when checking a garden.\n\n## Steps\n\n1. Inspect the garden.\n\n## Guardrails\n\nDo not remove plants.\n\n## Verification\n\nRun `git status` and verify the output is clean.\n'


def check_commands(cli, records):
    root = cli.root / 'commands'; target = root / 'garden.md'
    cli.run('commands','new','garden','--stdin',input=VALID_COMMAND)
    require(target.is_file() and target.read_text() == VALID_COMMAND, 'commands new lost content')
    require(target.stat().st_mode & 0o777 == 0o644, 'command Markdown mode must be 0644')
    require(VALID_COMMAND.strip() in cli.run('commands','show','garden').stdout, 'commands show lost content')
    require('garden' in json.dumps(cli.json('commands','list')), 'commands list omitted created command')
    cli.run('commands','validate','garden')
    target.write_text(VALID_COMMAND.replace('## Guardrails','## Safety'))
    cli.run('commands','validate','garden')
    target.write_text(VALID_COMMAND)
    # Secret-like values also occur in prose, without an api_key/token label.
    # Keep the same bare provider-key case as the independent F2P integrity probe.
    bare_secret = VALID_COMMAND + '\nCredential example: sk-ABCDEFGHIJKLMNOPQRSTUVWX\n'
    mutations = [VALID_COMMAND.replace('# Garden\n',''), VALID_COMMAND.replace('Use this when','Choose this for'),
                 VALID_COMMAND.replace('## Steps','## Workflow'), VALID_COMMAND.replace('## Guardrails','## Notes'),
                 VALID_COMMAND.replace('## Verification\n\nRun `git status` and verify the output is clean.\n',''),
                 VALID_COMMAND + '\napi_key=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n', bare_secret]
    for content in mutations:
        target.write_text(content)
        cli.run('commands','validate','garden',ok=False)
    target.write_text(VALID_COMMAND)
    for invalid in ['No heading or guidance\n', bare_secret]:
        (root / 'bad.md').write_text(invalid)
        cli.run('commands','validate',ok=False)
    (root / 'bad.md').unlink()
    cli.run('commands','validate')
    for name in ['../evil','nested/evil','nested\\evil',str(cli.root/'absolute'),'.hidden','_hidden']:
        cli.run('commands','new',name,'--stdin',input=VALID_COMMAND,ok=False)
    require(not (cli.root/'evil.md').exists() and not (cli.root/'absolute.md').exists(), 'commands escaped root')
    changed = VALID_COMMAND.replace('Garden','Garden changed')
    for content, options in [('', ['--force']), (changed, []), (changed, ['--expected-sha256','deadbeef'])]:
        cli.run('commands','update','garden','--stdin',*options,input=content,ok=False)
        require(target.read_text() == VALID_COMMAND, 'rejected commands update changed content')
    cli.run('commands','update','garden','--stdin','--expected-sha256',hashlib.sha256(VALID_COMMAND.encode()).hexdigest(),input=changed)
    require(target.read_text() == changed, 'commands update failed')
    cli.run('commands','rename','garden','renamed')
    renamed = root / 'renamed.md'
    require(not target.exists() and renamed.is_file() and renamed.read_text() == changed, 'commands rename failed')
    cli.run('commands','delete','renamed')
    require(not renamed.exists(), 'commands delete left file in place')
    trash_roots = [cli.root/'home/.Trash', Path(cli.env['XDG_DATA_HOME'])/'Trash/files']
    require(any(path.read_text() == changed for trash in trash_roots if trash.exists() for path in trash.rglob('*') if path.is_file()), 'commands delete did not move content to Trash')


def isolated_path(cli, name, excluded):
    # Hide only the tools whose absence is under test. An empty PATH would
    # accidentally forbid legitimate external utilities unrelated to Node or
    # engine discovery. Preserve first-match PATH order for every other tool.
    directory = cli.root / name; directory.mkdir(exist_ok=True)
    for path in directory.iterdir():
        path.unlink()
    for component in os.environ.get('PATH','').split(os.pathsep):
        if not component:
            continue
        source = Path(component)
        if not source.is_dir():
            continue
        for path in source.iterdir():
            destination = directory / path.name
            if path.name not in excluded and not destination.is_symlink() and path.is_file() and os.access(path,os.X_OK):
                destination.symlink_to(path.absolute())
    return directory


def engines(cli, names):
    directory = isolated_path(cli, 'engines', {'claude','codex'})
    log = cli.root / 'engine-calls.jsonl'; log.write_text('')
    for name in names:
        path = directory / name
        # Absolute Python path keeps the fixture independent of candidate PATH.
        path.write_text('#!' + sys.executable + '\n' +
            'import json,sys\nfrom pathlib import Path\n' +
            'with Path(' + repr(str(log)) + ').open("a") as log: log.write(json.dumps({"name":' + repr(name) + ',"argv":sys.argv[1:]})+"\\n")\n' +
            'prompt=sys.argv[-1].lower()\n' +
            'payload={"id":"1","domains":["ai"],"primary":"ai"} if "domain" in prompt else {"id":"1","categories":["tool"],"primary":"tool"}\n' +
            'print(json.dumps([payload]))\n')
        path.chmod(0o755)
    cli.env['PATH'] = str(directory)
    return log


def clear_classification(cli):
    with cli.database() as db:
        db.execute("UPDATE bookmarks SET categories=NULL,primary_category='unclassified',domains=NULL,primary_domain=NULL")


def check_engine_resolution(cli, records):
    log = engines(cli, ['claude','codex'])
    preference = cli.data / '.preferences'
    def selected(want, override=None):
        clear_classification(cli); log.write_text('')
        cli.run('classify', *(['--engine',override] if override else []))
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        require(calls and all(call['name'] == want for call in calls), 'wrong engine selected: ' + repr(calls))
    selected('claude')  # no preference, non-TTY preference order
    preference.write_text(json.dumps({'defaultEngine':'codex'}))
    selected('codex'); selected('claude','claude'); selected('codex','codex')
    preference.write_text(json.dumps({'defaultEngine':'unavailable'}))
    selected('claude')
    preference.write_text('{ malformed')
    selected('claude')
    engines(cli, ['codex']); selected('codex')
    for name in ['bogus','claude']:
        clear_classification(cli); log.write_text('')
        failure = cli.run('classify','--engine',name,ok=False)
        require(not log.read_text() and (failure.stdout + failure.stderr).strip(), 'invalid engine did not fail fast with a diagnostic')


def check_llm(cli, records):
    log = engines(cli, ['claude'])
    clear_classification(cli)
    cli.run('classify','--engine','claude')
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    require(calls and all(call['name'] == 'claude' and call['argv'][:3] == ['-p','--output-format','text']
                          and 'garden flowers' in call['argv'][-1] for call in calls), 'LLM bridge did not invoke the engine with bookmark content')
    with cli.database() as db:
        row = db.execute("SELECT categories,primary_category FROM bookmarks WHERE id='1'").fetchone()
    require(row and row[1] == 'tool' and 'tool' in (row[0] or '').split(','), 'LLM bridge did not persist engine classification')


def check_model(cli, records):
    engines(cli, ['claude','codex'])
    preferences = cli.data / '.preferences'
    for initial in [None, '{ malformed']:
        if initial is None:
            preferences.unlink(missing_ok=True)
        else:
            preferences.write_text(initial)
        output = cli.run('model').stdout
        require('claude' in output and 'codex' in output, 'model did not list detected engines')
        require(preferences.read_text() == initial if initial is not None else not preferences.exists(), 'read-only model changed preferences')
    # Saving must create the data directory, including missing parents.
    cli.data = cli.root / 'nested/data'; cli.env['FT_DATA_DIR'] = str(cli.data)
    preferences = cli.data / '.preferences'
    cli.run('model','codex')
    require(preferences.is_file(), 'model did not create preferences')
    require(json.loads(preferences.read_text()).get('defaultEngine') == 'codex', 'model did not save selected engine')
    require(preferences.stat().st_mode & 0o777 == 0o600, 'preferences mode must be 0600')
    output = cli.run('model').stdout
    require(re.search(r'codex[^\n]*(?:default)|(?:default)[^\n]*codex', output, re.I), 'model did not load saved default')
    before = preferences.read_bytes()
    cli.run('model','bogus',ok=False)
    require(preferences.read_bytes() == before, 'invalid model changed preferences')


def check_welcome(cli, records):
    cache = cli.data / 'bookmarks.jsonl'; saved = cache.read_bytes(); cache.unlink()
    first = cli.run().stdout
    require(re.search(r'welcome|first.run|get started|start by', first, re.I), 'missing first-run welcome')
    cache.write_bytes(saved)
    dashboard = cli.run().stdout
    require(dashboard.strip() and first != dashboard and re.search(r'bookmark|dashboard', dashboard, re.I), 'existing cache did not select dashboard')


def check_alias(cli, records):
    with cli.database() as db:
        search_scores = {str(identifier): score for identifier, score in db.execute("SELECT b.id,bm25(bookmarks_fts,5.0,1.0,1.0,3.0) FROM bookmarks_fts JOIN bookmarks b ON b.rowid=bookmarks_fts.rowid WHERE bookmarks_fts MATCH 'machine'")}
    for args in [('search','machine','--json'),('list','--limit','2','--json'),('stats','--json'),('path',)]:
        top = cli.run(*args).stdout
        alias = cli.run('bookmarks',*args).stdout
        if '--json' in args:
            def normalized(value):
                if isinstance(value,dict):
                    return {key: normalized(item) for key,item in value.items()}
                if isinstance(value,list):
                    items = [normalized(item) for item in value]
                    # Normalize only adjacent ties. Sorting all rows by id
                    # would conceal an alias that reverses unequal ranks.
                    if items and all(isinstance(item,dict) for item in items):
                        rank = None
                        if args[0] == 'search' and all(str(item.get('id')) in search_scores for item in items):
                            rank = lambda item: search_scores[str(item['id'])]
                        elif all('count' in item for item in items):
                            rank = lambda item: item['count']
                        elif args[0] == 'list' and all('postedAt' in item for item in items):
                            rank = lambda item: item['postedAt']
                        if rank:
                            items = [item for _, group in groupby(items, rank)
                                     for item in sorted(group, key=lambda item:json.dumps(item,sort_keys=True))]
                    return items
                return value
            require(normalized(json.loads(top)) == normalized(json.loads(alias)), 'bookmarks alias did not forward ' + args[0])
        else:
            require(top.strip() and alias.strip() == top.strip(), 'bookmarks alias did not forward ' + args[0])
    actual = rows(cli,'bookmarks','search','machine')
    expected = {row['id']: row['text'] for row in records if 'machine' in row['text']}
    require({str(row['id']): row.get('text') for row in actual} == expected,
            'bookmarks alias did not return the fixture search results')
    class AliasedCLI(CLI):
        def run(self, *args, **kwargs):
            return super().run('bookmarks', *args, **kwargs)
    # The four read-only comparisons above cannot prove forwarding for the
    # other offline aliases. Exercise their actual content and state effects
    # in fresh roots, using the same public postconditions as the top level.
    for mode in ['show','categories','domains','folders','classify','model','llm']:
        with tempfile.TemporaryDirectory(prefix='alias-'+mode+'-', dir=cli.root) as temporary:
            alias_records = fixtures(mode)
            alias_cli = AliasedCLI(cli.binary, Path(temporary), alias_records, index=mode not in NO_INDEX)
            CHECKS[mode](alias_cli, alias_records)
    with tempfile.TemporaryDirectory(prefix='alias-classify-domains-', dir=cli.root) as temporary:
        alias_records = fixtures('llm')
        alias_cli = AliasedCLI(cli.binary, Path(temporary), alias_records)
        log = engines(alias_cli, ['claude'])
        clear_classification(alias_cli)
        alias_cli.run('classify-domains','--engine','claude')
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        require(calls and all(call['name'] == 'claude' and call['argv'][:3] == ['-p','--output-format','text']
                             and 'garden flowers' in call['argv'][-1] for call in calls),
                'classify-domains alias did not invoke the engine with bookmark content')
        with alias_cli.database() as db:
            value = db.execute("SELECT domains,primary_domain FROM bookmarks WHERE id='1'").fetchone()
        require(value and value[1] == 'ai' and 'ai' in (value[0] or '').split(','),
                'classify-domains alias did not persist engine domains')


def check_errors(cli, records):
    for args in [('search',), ('not-a-command',), ('list','--not-an-option')]:
        failure = cli.run(*args,ok=False)
        text = (failure.stdout + failure.stderr).strip()
        require(text and re.search(r'argument|usage|unknown|unrecognized|option|command|query|help', text, re.I), 'parse failure lacks actionable diagnostic')
    (cli.root/'commands/bad.md').write_text('invalid portable command\n')
    failure = cli.run('commands','validate',ok=False)
    require((failure.stdout + failure.stderr).strip(), 'validate failure has no diagnostic')


def check_json_modes(cli, records):
    value = cli.json('stats')
    require(isinstance(value,dict) and value.get('totalBookmarks') == len(records), 'stats JSON has no correct data')
    output = cli.run('stats').stdout
    require(output.strip() and str(len(records)) in output, 'human stats omitted total')
    try:
        json.loads(output)
    except ValueError:
        return
    require(False, 'human stats printed JSON')


def check_help(cli, records):
    output = cli.run('--help').stdout
    for command in ['search','list','show','stats','index','classify','categories','domains','folders','model','path','paths','library','commands']:
        require(re.search(r'\b' + command + r'\b', output), 'top-level help omitted ' + command)
    alternative = cli.root / ('ft' if Path(cli.binary).name == 'fieldtheory' else 'fieldtheory')
    shutil.copy2(cli.binary, alternative)
    original = cli.binary
    try:
        cli.binary = str(alternative)
        other = cli.run('--help').stdout
    finally:
        cli.binary = original
    def normalize(text, invocation):
        text = text.replace(str(invocation), '<cli>')
        return ' '.join(re.sub(r'(?<![\w-])(?:fieldtheory|ft)(?![\w-])', '<cli>', text).split())
    require(normalize(output, original) == normalize(other, alternative), 'ft and fieldtheory expose different command trees')


def check_surface(cli, records):
    # Execute documented commands with their real arguments. No subcommand
    # --help is required or used as a substitute for successful operations.
    require(rows(cli,'list','--limit','2') and rows(cli,'search','machine'), 'documented list/search failed')
    require('machine learning' in cli.run('show','1').stdout, 'documented show failed')
    require(cli.run('path').stdout.strip() == str(cli.data), 'documented path failed')
    require(cli.json('stats').get('totalBookmarks') == len(records), 'documented stats failed')


CHECKS = {'paths': check_paths, 'search': check_search, 'bm25': check_bm25, 'show': check_show,
          **{name: (lambda cli, records, name=name: check_distribution(cli, records, name)) for name in ['categories','domains','folders']},
          'classify': check_classify, 'regex-no-engine': check_regex_no_engine,
          'native-classify': lambda cli, records: check_native(cli, records, True),
          'native-database': check_native, 'refresh': check_refresh,
          'library': check_library, 'commands': check_commands,
          'engine-resolution': check_engine_resolution, 'llm': check_llm, 'model': check_model,
          'preferences': check_model, 'welcome': check_welcome, 'alias': check_alias,
          'errors': check_errors, 'json': check_json_modes, 'help': check_help, 'surface': check_surface}
NO_INDEX = {'paths','model','preferences','welcome','errors','help','native-classify','native-database'}
