"""Check FTS5 declarations without prescribing SQL whitespace or quoting."""
from __future__ import annotations

import re
import shlex
import sys


TOKEN = re.compile(r'''\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:[^']|'')*'|"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]])*\]|[A-Za-z_][A-Za-z_0-9]*|[^\s]''')


def value(token):
    if token[:1] in {"'", '"', '`'}:
        quote = token[0]
        return token[1:-1].replace(quote * 2, quote)
    if token.startswith('['):
        return token[1:-1]
    return token


def validate_fts(sql):
    tokens = [t for t in TOKEN.findall(sql)
              if not t.isspace() and not t.startswith(('--', '/*'))]
    words = [t.casefold() for t in tokens]
    # Read the declaration itself, never a matching phrase inside a comment or
    # quoted column name. sqlite_master supplies a CREATE statement.
    if words[:3] != ['create', 'virtual', 'table']:
        raise ValueError('bookmarks_fts must be an FTS5 virtual table')
    using = 3
    if words[using:using + 3] == ['if', 'not', 'exists']:
        using += 3
    table = tokens[using:using + 1]
    using += 1
    if tokens[using:using + 1] == ['.']:
        if not table or value(table[0]).casefold() != 'main':
            raise ValueError('unexpected FTS5 schema')
        table = tokens[using + 1:using + 2]
        using += 2
    if not table or value(table[0]).casefold() != 'bookmarks_fts':
        raise ValueError('unexpected FTS5 table')
    if words[using:using + 1] != ['using']:
        raise ValueError('missing FTS5 declaration')
    if len(tokens) < using + 3 or value(tokens[using + 1]).casefold() != 'fts5' or tokens[using + 2] != '(':
        raise ValueError('bookmarks_fts must use FTS5')
    if not tokens or tokens[-1] != ')':
        # SQLite stores declarations without the optional terminating semicolon.
        if tokens[-2:] == [')', ';']:
            tokens.pop()
        else:
            raise ValueError('invalid FTS5 declaration')
    items, current = [], []
    for token in tokens[using + 3:-1]:
        if token == ',':
            items.append(current)
            current = []
        else:
            current.append(token)
    items.append(current)
    columns, options = [], {}
    for item in items:
        if len(item) == 1:
            columns.append(value(item[0]).casefold())
        elif len(item) == 3 and item[1] == '=':
            key = value(item[0]).casefold()
            if key in options:
                raise ValueError('duplicate FTS5 option: ' + key)
            options[key] = value(item[2])
        else:
            raise ValueError('unsupported FTS5 column or option: ' + ' '.join(item))
    if columns != ['text', 'author_handle', 'author_name', 'article_text']:
        raise ValueError('FTS5 must index text, author_handle, author_name and article_text')
    if options.get('content', '').casefold() != 'bookmarks':
        raise ValueError('FTS5 content must be bookmarks')
    if options.get('content_rowid', '').casefold() != 'rowid':
        raise ValueError('FTS5 content_rowid must be rowid')
    try:
        tokenizer = shlex.split(options.get('tokenize', ''))
    except ValueError:
        raise ValueError('invalid FTS5 tokenizer') from None
    if [part.casefold() for part in tokenizer[:2]] != ['porter', 'unicode61'] or len(tokenizer) % 2:
        raise ValueError('FTS5 tokenizer must be porter unicode61')
    # Explicit defaults have the same behavior as `porter unicode61`.
    # Non-default tokenizer options change matching and must still fail.
    seen = set()
    for key, setting in zip(tokenizer[2::2], tokenizer[3::2]):
        key = key.casefold()
        if key in seen:
            raise ValueError('duplicate tokenizer option: ' + key)
        seen.add(key)
        if key == 'remove_diacritics':
            try:
                equivalent = int(setting) == 1
            except ValueError:
                equivalent = False
        elif key == 'categories':
            equivalent = set(setting.split()) == {'L*', 'N*', 'Co'}
        else:
            equivalent = key in {'tokenchars', 'separators'} and setting == ''
        if not equivalent:
            raise ValueError('non-default tokenizer option: ' + key)


if __name__ == '__main__':
    try:
        validate_fts(sys.argv[1])
    except (ValueError, IndexError) as exc:
        sys.exit(str(exc))
