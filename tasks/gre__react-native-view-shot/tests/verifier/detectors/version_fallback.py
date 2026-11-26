"""Recognize an embedded old framework without forbidding the shared language.

Example applications, tests and installed dependencies are outside this task's
production scope. A normal Node process is valid for both framework versions.

Framework identity is established from the *content* of an embedded package and
from whether the candidate source actually *loads* it -- never from the package
name alone.  Renaming a vendored copy (``react-native`` -> ``old-rn``), moving it
under a scope alias (``@fb/react-native``) or stripping the ``ReactAndroid`` /
``Libraries`` layout markers therefore cannot hide it: as long as the copy
declares the source framework version and is wired into candidate code, it is an
active source fallback.  An unrelated package that merely shares a version number
is not flagged because it neither carries framework content nor is referenced by
a relative / local-alias load from candidate code.

The load relationship is established by *resolving the specifier token*, never by
matching the call syntax that consumes it.  Any literal that names an in-tree
module -- a relative path (``./`` / ``../``), or a local ``file:`` / ``link:`` /
``portal:`` alias -- establishes a source->sink edge as soon as it resolves to a
package root that declares the source version, regardless of whether the load is
spelled with ``require``, ``import``, ``from``, an aliased loader
(``const r = require; r("...")``), member access (``module.require`` /
``require.resolve`` / ``createRequire``) or any quoting style (single, double or
template literal).
"""
from pathlib import Path
import json
import os
import re


IGNORED = {'.git', 'node_modules', '.venv', '__pycache__', 'lib', 'dist', 'build',
           'example', 'example-expo', 'example-windows', 'src/__tests__'}

_JS_SUFFIXES = {'.js', '.cjs', '.mjs', '.ts', '.tsx'}

# A module specifier is the *literal token* that names a module, not the call
# syntax that consumes it.  Matching the token instead of a particular callee
# (``require``/``import``/``from``) makes the source->sink edge independent of
# how the load is spelled: an aliased loader (``const r = require; r("x")``),
# member access (``module.require`` / ``require.resolve`` / ``createRequire``)
# and any quoting style (single, double or template literal) all carry the same
# specifier.  Only in-tree specifiers can resolve to a vendored copy, so only
# relative paths and local aliases are evaluated.
_STRING_LITERAL = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""", re.DOTALL)

# Fields whose *keys* declare a dependency on another package.
_DEPENDENCY_FIELDS = ('dependencies', 'devDependencies', 'peerDependencies',
                      'optionalDependencies', 'resolutions', 'overrides')
# Fields whose *values* may point at an in-tree package via a local specifier.
_LOCAL_SPEC_FIELDS = _DEPENDENCY_FIELDS
_LOCAL_SPEC_PREFIXES = ('file:', 'link:', 'portal:')

# Manifest keys whose values declare a dependency on the source framework.
_CONSUMER_FIELDS = ('dependencies', 'devDependencies', 'peerDependencies',
                    'optionalDependencies')

# Framework content fingerprints: identity evidence a rename cannot erase.
# Keyed by the casefolded, hyphenated source framework name.  Only the leading
# markers of a framework distribution are listed; the version gate alone is not
# enough (an unrelated package may share a version number) and a self-declared
# name alone is not enough (it can be renamed).
_FRAMEWORK_CONTENT = {
    'react-native': {
        'dirs': ('Libraries', 'ReactAndroid', 'ReactCommon'),
        'files': ('jest-preset.js', 'rn-get-polyfills.js', 'template.config.js'),
        'globs': ('*.podspec',),
        'dependency_prefixes': ('@react-native/',),
    },
}


def version_upgrade(config):
    source = config.get('source_technology', {})
    target = config.get('target_technology', {})
    return (source.get('name') == target.get('name') and source.get('version')
            and target.get('version') and source['version'] != target['version'])


def _self_declared_identity(package, directory, name):
    """Signals carried in the package's own manifest / layout.

    A rename defeats these; they are kept because they are cheap and correct when
    the identity is still self-declared, but they are never the *only* basis for
    a conclusion (see ``_is_old_framework_package``).
    """
    identity = str(package.get('name', '')).casefold()
    repository = package.get('repository', '')
    repository = repository.get('url', '') if isinstance(repository, dict) else repository
    upstream = str(repository).casefold()
    return (identity == name or identity.startswith(name + '-')
            or f'facebook/{name}' in upstream
            or ((directory / 'ReactAndroid').is_dir()
                and (directory / 'Libraries').is_dir()))


def _content_signature(directory, package, name):
    """Framework-specific content that a rename cannot remove."""
    signature = _FRAMEWORK_CONTENT.get(name)
    if not signature:
        return False
    if any((directory / item).is_dir() for item in signature.get('dirs', ())):
        return True
    if any((directory / item).is_file() for item in signature.get('files', ())):
        return True
    for pattern in signature.get('globs', ()):
        if any(directory.glob(pattern)):
            return True
    prefixes = signature.get('dependency_prefixes', ())
    if prefixes:
        for field in ('dependencies', 'peerDependencies', 'optionalDependencies'):
            table = package.get(field)
            if isinstance(table, dict) and any(
                str(key).casefold().startswith(tuple(prefixes)) for key in table
            ):
                return True
    return False


def _declares_source_dependency(package, name):
    """True when the package *consumes* the framework instead of being it.

    A legitimate consumer (an example app, a workspace member) declares the
    framework among its dependencies.  A vendored copy of the framework itself
    does not depend on itself, so this keeps "an unrelated package sharing a
    version number" out of the L2 conclusion.
    """
    for field in _CONSUMER_FIELDS:
        table = package.get(field)
        if not isinstance(table, dict):
            continue
        for key in table:
            if str(key).casefold() == name:
                return True
    return False


def _is_old_framework_package(package, directory, name, version, *, loaded):
    """Return True when ``package`` is an embedded copy of the source framework.

    ``version`` is the source framework version.  Identity requires the package
    to declare that version *and* one rename-proof signal: self-declared identity,
    framework content, or an actual load relationship (``loaded``) established by
    the caller.
    """
    if str(package.get('version')) != version:
        return False
    if _declares_source_dependency(package, name):
        return False
    if _self_declared_identity(package, directory, name):
        return True
    if _content_signature(directory, package, name):
        return True
    return bool(loaded)


def _read_package(path):
    try:
        package = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return package if isinstance(package, dict) else None


def _local_dependency_targets(root, manifests):
    """Map a dependency alias to its in-tree package root (``file:``/``link:``)."""
    targets = {}
    resolved_root = root.resolve()
    for manifest in manifests:
        package = _read_package(manifest)
        if package is None:
            continue
        base = manifest.parent
        for field in _LOCAL_SPEC_FIELDS:
            table = package.get(field)
            if not isinstance(table, dict):
                continue
            for alias, spec in table.items():
                if not isinstance(spec, str):
                    continue
                trimmed = spec.strip()
                prefix = next((item for item in _LOCAL_SPEC_PREFIXES
                               if trimmed.startswith(item)), None)
                if prefix is None:
                    continue
                relative = trimmed[len(prefix):].strip()
                if not relative:
                    continue
                try:
                    target = (base / relative).resolve()
                except OSError:
                    continue
                if not target.is_relative_to(resolved_root):
                    continue
                targets[str(alias).casefold()] = target
    return targets


def _resolve_package_root(candidate, root):
    """Walk up from ``candidate`` to the nearest in-tree package root.

    Ignored directories (``node_modules``, build output, examples, fixtures) are
    not candidate production sources; a specifier that resolves through them is
    not a vendor-load edge, so the walk stops and returns ``None``.
    """
    resolved_root = root.resolve()
    for package_root in (candidate, *candidate.parents):
        if not package_root.is_relative_to(resolved_root):
            return None
        if any(part in IGNORED
               for part in package_root.relative_to(resolved_root).parts):
            return None
        metadata = package_root / 'package.json'
        if metadata.is_file():
            return package_root
    return None


def _module_specifiers(text):
    """Yield every string / template literal token in ``text``.

    Extracting the literal token (rather than the loader call) is what makes the
    load relationship spelling-independent: the same specifier is recovered from
    ``require("x")``, ``import("x")``, ``from "x"``, ``require(`x`)`` and an
    aliased ``const r = require; r("x")``.
    """
    for match in _STRING_LITERAL.finditer(text):
        yield match.group(2)


def _resolve_local_specifier(specifier, file, root, alias_targets):
    """Resolve an in-tree specifier to its package root, else ``None``.

    Only specifiers that can point back into the candidate tree are considered:
    relative paths (``./`` / ``../``) and local ``file:`` / ``link:`` /
    ``portal:`` aliases (whose targets were resolved by the caller).  A bare
    registry dependency such as ``react-native`` is not a vendored copy and is
    deliberately ignored.
    """
    value = specifier.casefold()
    if value.startswith('./') or value.startswith('../'):
        return _resolve_package_root((file.parent / specifier).resolve(), root)
    if value in alias_targets:
        # A local alias (``file:`` / ``link:`` / ``portal:``) that points back
        # into the tree is a load relationship too.
        return _resolve_package_root(alias_targets[value], root)
    return None


def detect_framework_fallback(root, config):
    source = config['source_technology']
    name = source['name'].casefold().replace(' ', '-')
    version = source['version']
    hits = []
    manifest_files = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        directory = Path(parent)
        dirs[:] = [d for d in dirs if d not in IGNORED and d not in {'__tests__', 'tests', 'fixtures'}
                   and not (directory / d).is_symlink()]
        for filename in files:
            if filename == 'package.json':
                candidate = directory / filename
                if not candidate.is_symlink():
                    manifest_files.append(candidate)
    alias_targets = _local_dependency_targets(root, manifest_files)
    for parent, dirs, files in os.walk(root, followlinks=False):
        directory = Path(parent)
        dirs[:] = [d for d in dirs if d not in IGNORED and d not in {'__tests__', 'tests', 'fixtures'}
                   and not (directory / d).is_symlink()]
        for filename in files:
            file = directory / filename
            if file.is_symlink() or file.stat().st_size > 8 * 1024 * 1024:
                continue
            relative = file.relative_to(root).as_posix()
            if filename == 'package.json' and directory != root:
                package = _read_package(file)
                if package is None:
                    continue
                # Renaming a vendored package does not change its framework
                # identity. Identity comes from content / load relationship, not
                # from the self-declared name, so a renamed copy is still caught
                # when it is actually loaded below.
                if _is_old_framework_package(package, directory, name, version, loaded=False):
                    hits.append({'detector': 'embedded_old_framework', 'path': relative,
                                 'evidence': f'{name}@{version}', 'capability': 'callgraph'})
            elif file.suffix in _JS_SUFFIXES:
                text = file.read_text(errors='replace')
                # Only actual module specifiers: documentation and a harmless
                # source-version string alone are not fallback hits.  Comments
                # are blanked so prose cannot pose as a load relationship.
                from .runtime_identity import strip_detector_comments
                clean = strip_detector_comments(text, file.suffix)
                seen = set()
                for specifier in _module_specifiers(clean):
                    value = specifier.casefold()
                    if name in value and version in value:
                        # A literal that spells the framework and the source
                        # version is evidence regardless of how it is wired.
                        if specifier in seen:
                            continue
                        seen.add(specifier)
                        hits.append({'detector': 'old_framework_load', 'path': relative,
                                     'evidence': specifier, 'capability': 'callgraph'})
                        continue
                    package_root = _resolve_local_specifier(
                        specifier, file, root, alias_targets)
                    if package_root is None:
                        continue
                    package = _read_package(package_root / 'package.json')
                    if package is None:
                        continue
                    if _is_old_framework_package(package, package_root, name, version, loaded=True):
                        metadata = package_root / 'package.json'
                        evidence = specifier + ' -> ' + metadata.relative_to(root).as_posix()
                        if evidence in seen:
                            continue
                        seen.add(evidence)
                        hits.append({'detector': 'old_framework_load', 'path': relative,
                                     'evidence': evidence, 'capability': 'callgraph'})
    return hits
