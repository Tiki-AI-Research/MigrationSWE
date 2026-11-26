"""CLI postconditions with independent JSON, SQLite and fixture expectations."""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


class ContractFailure(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise ContractFailure(message)


def record(identifier, text='garden', author='alice', posted='2024-01-12T12:00:00Z', language='en', folders=()):
    return {'id': str(identifier), 'tweetId': str(identifier), 'url': 'https://x.com/' + author + '/status/' + str(identifier),
            'text': text, 'authorHandle': author, 'authorName': author,
            'postedAt': posted, 'bookmarkedAt': '2099-01-01T00:00:00Z', 'syncedAt': '2099-01-02T00:00:00Z',
            'language': language, 'folderIds': list(folders), 'folderNames': list(folders)}


class CLI:
    def __init__(self, binary, root, records, index=True):
        self.binary, self.root = str(Path(binary).resolve()), root
        self.data = root / 'data'
        self.data.mkdir()
        for name in ['home', 'library', 'commands']:
            (root / name).mkdir()
        self.env = dict(os.environ, HOME=str(root / 'home'), FT_DATA_DIR=str(self.data),
                        FT_LIBRARY_DIR=str(root / 'library'), FT_COMMANDS_DIR=str(root / 'commands'),
                        XDG_DATA_HOME=str(root / 'home/.local/share'))
        (self.data / 'bookmarks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in records))
        if index:
            self.run('index')

    def run(self, *args, ok=True, input=None):
        result = subprocess.run([self.binary, *args], cwd=self.root, env=self.env,
                                stdin=subprocess.DEVNULL if input is None else None, input=input,
                                capture_output=True, text=True, timeout=30)
        require(result.returncode >= 0, 'command terminated by signal: ' + ' '.join(args))
        if ok:
            require(result.returncode == 0, 'command failed: ' + ' '.join(args) + '\n' + result.stderr[-3000:])
        else:
            require(result.returncode != 0, 'command accepted invalid input: ' + ' '.join(args))
        return result

    def json(self, *args):
        result = self.run(*args, '--json')
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise ContractFailure('invalid JSON from ' + ' '.join(args)) from exc

    def database(self):
        path = self.data / 'bookmarks.db'
        require(path.is_file(), 'index did not create bookmarks.db')
        # URI mode=rw avoids silently creating a database for a missing file.
        return sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)


def check_database(cli, records):
    with cli.database() as db:
        try:
            version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            count = db.execute('SELECT COUNT(*) FROM bookmarks').fetchone()[0]
        except sqlite3.Error as exc:
            raise ContractFailure('index did not create a usable SQLite index: ' + str(exc)) from exc
    require(version is not None and str(version[0]) == '6', 'index schema version is not 6')
    require(count == len(records), 'indexed bookmark count differs from JSONL')


def list_records():
    records = [record('1', 'machine learning', 'alice', '2024-01-02T10:00:00Z', folders=['Research']),
               record('2', 'machine guide', 'bob', '2024-02-02T10:00:00Z', folders=['Security']),
               record('3', 'garden bees', 'alice', '2024-03-02T10:00:00Z', folders=['Resources']),
               record('4', 'machine system', 'carol', '2024-04-02T10:00:00Z')]
    records += [record(str(i), 'other garden', 'other', '2023-01-01T00:00:00Z') for i in range(5, 39)]
    return records


def check_list(cli, records):
    check_database(cli, records)
    with cli.database() as db:
        db.execute("UPDATE bookmarks SET categories='security,tool',primary_category='security',domains='ai',primary_domain='ai' WHERE id IN ('1','4')")
        db.execute("UPDATE bookmarks SET categories='research',primary_category='research',domains='science',primary_domain='science' WHERE id='2'")
    def ids(*args):
        rows = cli.json('list', *args)
        require(isinstance(rows, list) and all(isinstance(row, dict) and 'id' in row for row in rows), 'list JSON is not a bookmark array')
        result = [str(row['id']) for row in rows]
        require(len(set(result)) == len(result), 'list returned duplicate ids')
        return result
    cases = [(('--query','machine'), {'1','2','4'}), (('--author','alice'), {'1','3'}),
             (('--after','2024-02-01','--before','2024-03-31'), {'2','3'}),
             (('--category','security'), {'1','4'}), (('--domain','ai'), {'1','4'}),
             (('--folder','Research'), {'1'}), (('--folder','Secur'), {'2'}),
             (('--query','machine','--author','alice','--category','security','--domain','ai'), {'1'})]
    for args, expected in cases:
        got = set(ids(*args))
        require(got == expected, 'list filters ' + repr(args) + ': ' + repr(got) + ' want ' + repr(expected))
    all_ids = ids('--limit','100')
    require(set(all_ids) == {row['id'] for row in records}, 'list omitted fixture rows')
    require(ids() == all_ids[:30], 'list default limit/offset differs from 30/0')
    require(ids('--limit','2','--offset','2') == all_ids[2:4], 'list offset/limit ignored')
    cli.run('list','--folder','Res','--json',ok=False)


def stats_records():
    records = []
    for author in range(20):
        for _ in range(20 - author):
            records.append(record(str(len(records) + 1), author='author' + str(author), language='l' + str(len(records) % 13)))
    for row, stamp in zip(records, ['Wed Oct 10 20:19:24 +0000 2018', '2024-01-31T23:30:00-03:00',
                                   '2024-01-01T00:30:00+02:00', 'garbage']):
        row['postedAt'] = stamp
    return records


def check_stats(cli, records):
    check_database(cli, records)
    stats = cli.json('stats')
    require(isinstance(stats, dict), 'stats JSON is not an object')
    require(stats.get('totalBookmarks') == len(records), 'incorrect totalBookmarks')
    authors = Counter(row['authorHandle'] for row in records)
    languages = Counter(row['language'] for row in records)
    require(stats.get('uniqueAuthors') == len(authors), 'incorrect uniqueAuthors')
    require(stats.get('dateRange') == {'earliest': '2018-10-10', 'latest': '2024-02-01'},
            'dateRange must use valid posted_at timestamps in UTC, ignoring garbage and bookmarked_at')
    for field, key, expected, limit in [('topAuthors','handle',authors,15), ('languageBreakdown','language',languages,10)]:
        rows = stats.get(field)
        require(isinstance(rows, list) and len(rows) == min(limit,len(expected)), 'wrong ' + field + ' limit')
        names, counts = [], []
        for row in rows:
            require(isinstance(row, dict) and row.get(key) in expected and row.get('count') == expected[row[key]], 'incorrect ' + field + ' entry')
            names.append(row[key]); counts.append(row['count'])
        require(len(set(names)) == len(names), 'duplicate ' + field + ' entries')
        require(counts == sorted(expected.values(), reverse=True)[:limit], field + ' must be sorted by descending count')


def main():
    from cli_surface_contract import CHECKS, NO_INDEX, fixtures
    mode, binary = sys.argv[1:3]
    records = stats_records() if mode == 'stats' else fixtures(mode)
    checks = {'database': check_database, 'list': check_list, 'stats': check_stats}
    checks.update(CHECKS)
    try:
        with tempfile.TemporaryDirectory(prefix='ft-cli-contract-') as temporary:
            cli = CLI(binary, Path(temporary), records, index=mode not in NO_INDEX)
            checks[mode](cli, records)
        print(mode + ' contract passed')
        return 0
    except (ContractFailure, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print('CLI timed out: ' + str(exc.cmd), file=sys.stderr)
        return 124
    except OSError as exc:
        print('INCONCLUSIVE: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.modules.setdefault('cli_contract', sys.modules[__name__])
    raise SystemExit(main())
