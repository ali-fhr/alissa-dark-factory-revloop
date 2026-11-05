#!/usr/bin/env python3
"""Release-train helpers for revloop's workflows (issue #220, fleet upgrade safety §4.2).

A merge is not a release. Only a release pull request changes the version file, and only a
version change publishes. Three workflows call this script, and test_release_train.py pins every
rule below:

  guard         check-version-bump.yaml. A pull request that changes a published path must add a
                CHANGELOG.md line under `## Unreleased`. The version file may change only on a
                `release/X.Y.Z` branch of this repository, where X.Y.Z must equal the new version,
                or on a pull request labelled `release:critical`.
  publish-gate  package-publish.yaml and image-publish.yaml. Publish only when the version file at
                the merge commit differs from its first parent, and the `v<version>` tag is absent
                or names this same commit (a re-run). The tag names another commit when this merge
                is a release branch merged back into main, and that version is already out.
  cut           release-train.yaml. Move the `## Unreleased` entries under `## X.Y.Z (date)`, bump
                the version file (patch, or minor when a bullet starts with `[minor]`) and sync the
                Dockerfile ARG. When there is nothing to release, it changes nothing.

Standard library only: the workflows run it with the runner's python3 and nothing installed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, cast

UNRELEASED = 'Unreleased'
CRITICAL_LABEL = 'release:critical'
RELEASE_PREFIX = 'release/'
TAG_PREFIX = 'v'

_HEADING = re.compile(r'^## +(\S+)')
_VERSION = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
# A `[minor]` entry is a bullet whose text STARTS with the tag. A `[minor]` in running text, such
# as "the `[minor]` left on the approving review of PR #217", is review vocabulary. It is not a
# release tag.
_MINOR_ENTRY = re.compile(r'^\s*[-*+]\s+\[minor\]', re.IGNORECASE)


# ---- CHANGELOG.md -------------------------------------------------------------------------------

def _key(line: str) -> Optional[str]:
    match = _HEADING.match(line)
    return match.group(1) if match else None


def section(text: str, key: str) -> Optional[List[str]]:
    """Body lines of the first `## <key>` section, or None when the heading is absent.

    The key is the first word after `## `, so `## 0.31.41 (2026-11-05)` has the key `0.31.41`.
    """
    body: Optional[List[str]] = None
    for line in text.splitlines():
        heading = _key(line)
        if heading is not None:
            if body is not None:
                return body
            if heading == key:
                body = []
            continue
        if body is not None:
            body.append(line)
    return body


def entries(lines: Optional[Iterable[str]]) -> List[str]:
    """The non-blank, stripped lines of a section body, with HTML comments removed."""
    out: List[str] = []
    in_comment = False
    for raw in lines or ():
        line = raw
        kept = ''
        while line:
            if in_comment:
                end = line.find('-->')
                if end < 0:
                    line = ''
                    break
                line = line[end + 3:]
                in_comment = False
            else:
                start = line.find('<!--')
                if start < 0:
                    kept += line
                    break
                kept += line[:start]
                line = line[start + 4:]
                in_comment = True
        if kept.strip():
            out.append(kept.strip())
    return out


def added_entries(base_text: str, head_text: str, key: str) -> List[str]:
    """Lines of `## <key>` in the head changelog that the base changelog's section lacks.

    It is a multiset difference, so a line repeated in the head counts once for each extra copy.
    """
    remaining: Dict[str, int] = {}
    for line in entries(section(base_text, key)):
        remaining[line] = remaining.get(line, 0) + 1
    added = []
    for line in entries(section(head_text, key)):
        if remaining.get(line, 0) > 0:
            remaining[line] -= 1
        else:
            added.append(line)
    return added


def parse_version(version: str) -> Tuple[int, int, int]:
    match = _VERSION.match(version.strip())
    if not match:
        raise ValueError(f'{version!r} is not a plain X.Y.Z version')
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_version(current: str, minor: bool) -> str:
    major, min_, patch = parse_version(current)
    return f'{major}.{min_ + 1}.0' if minor else f'{major}.{min_}.{patch + 1}'


def is_minor(lines: Iterable[str]) -> bool:
    return any(_MINOR_ENTRY.match(line) for line in lines)


def cut_changelog(text: str, current: str, date: str) -> Optional[Tuple[str, str]]:
    """(new changelog, new version), or None when `## Unreleased` holds no entry.

    The `## Unreleased` heading stays, now empty. The entries move, as written, under a new
    `## X.Y.Z (YYYY-MM-DD)` heading directly below it.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _key(line) == UNRELEASED), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _key(lines[i]) is not None), len(lines))
    body = lines[start + 1:end]
    if not entries(body):
        return None
    version = next_version(current, is_minor(body))
    while body and not body[0].strip():
        body = body[1:]
    while body and not body[-1].strip():
        body = body[:-1]
    out = lines[:start + 1] + ['', f'## {version} ({date})', ''] + body + [''] + lines[end:]
    return '\n'.join(out).rstrip('\n') + '\n', version


def sync_arg(dockerfile: str, arg: str, version: str) -> str:
    """The Dockerfile with its one `ARG <arg>=<value>` default set to the version."""
    pattern = re.compile(rf'^ARG {re.escape(arg)}=\S*', re.MULTILINE)
    found = pattern.findall(dockerfile)
    if len(found) != 1:
        raise ValueError(f'expected exactly one "ARG {arg}=<version>" line, found {len(found)}')
    return pattern.sub(f'ARG {arg}={version}', dockerfile)


# ---- the guard ----------------------------------------------------------------------------------

def decide(changed: Sequence[str], *, dist_prefix: str, test_prefix: str, image_prefix: str,
           version_file: str, head_ref: str, base_ref: str, same_repo: bool, labels: Sequence[str],
           head_version: Optional[str], base_changelog: str, head_changelog: str,
           tag_exists: bool) -> Dict[str, object]:
    """The check-version-bump.yaml verdict on one pull request. It is pure, so tests drive it."""
    published = [path for path in changed
                 if (path.startswith(dist_prefix) and not path.startswith(test_prefix))
                 or (image_prefix and path.startswith(image_prefix))]
    version_changed = version_file in changed
    critical = CRITICAL_LABEL in labels
    release_head = head_ref.startswith(RELEASE_PREFIX)
    release_branch = bool(same_repo and head_version and head_ref == RELEASE_PREFIX + head_version)
    errors: List[str] = []
    notices: List[str] = []

    if version_changed:
        if release_branch:
            notices.append(f'{version_file} changes on {head_ref}, a release branch: merging it releases '
                           f'{head_version}.')
        elif critical:
            notices.append(f'{version_file} changes on a pull request labelled {CRITICAL_LABEL}: merging '
                           f'it releases {head_version or "<no version>"}.')
        else:
            why = ''
            if release_head and not same_repo:
                why = f' ({head_ref} is a branch of a fork, not of this repository)'
            elif release_head:
                why = (f' ({head_ref} does not name the version file\'s {head_version or "<missing>"}; a '
                       f'release branch is release/<the version it releases>)')
            errors.append(
                f'RULE: only a pull request from a release/X.Y.Z branch, or one labelled {CRITICAL_LABEL}, '
                f'may change {version_file}{why}. A merge is not a release (fleet upgrade safety §4.2): '
                f'add a line under "## {UNRELEASED}" in CHANGELOG.md and leave the version alone. The '
                f'release train (release-train.yaml, 16:00Z and 23:00Z) bumps it. For a critical fix, '
                f'ask the operator for the {CRITICAL_LABEL} label.')

    if published:
        keys = [UNRELEASED]
        if head_version and (version_changed or base_ref.startswith(RELEASE_PREFIX)):
            keys.append(head_version)
        added = [line for key in keys for line in added_entries(base_changelog, head_changelog, key)]
        if added:
            notices.append(f'CHANGELOG.md gains {len(added)} line(s) under '
                           f'{" / ".join("## " + k for k in keys)}.')
        else:
            where = ' or '.join(f'"## {k}"' for k in keys)
            errors.append(
                f'RULE: a pull request that changes a published path ({dist_prefix}** except '
                f'{test_prefix}**, or {image_prefix}**) must add a line under {where} in CHANGELOG.md. '
                f'This one changes {len(published)} published file(s), for example {published[0]}, and '
                f'adds none. The release train releases only what "## {UNRELEASED}" lists, so an '
                f'unlisted change would ship unannounced in some later release.')

    # A release branch merged back into main carries a version that its own branch already
    # published: the tag exists. The PyPI-exists rule would refuse it, and the publish runs on
    # main stop at publish-gate, so the merge-back is exempt.
    merge_back = version_changed and release_branch and tag_exists and not base_ref.startswith(RELEASE_PREFIX)
    if merge_back:
        notices.append(f'{head_ref} merges back a release already tagged {TAG_PREFIX}{head_version}: the '
                       f'PyPI-exists check is skipped and the publish runs on the merge do nothing.')
    return {
        'ok': not errors,
        'errors': errors,
        'notices': notices,
        'published': bool(published),
        'version_changed': version_changed,
        'pypi_check': version_changed and not merge_back,
    }


# ---- the publish gate ---------------------------------------------------------------------------

def publish_decision(parent_version: Optional[str], version: Optional[str], tag_commit: Optional[str],
                     sha: str) -> Tuple[bool, str]:
    if version == parent_version:
        return False, (f'the version file is unchanged at {version or "<missing>"}: this merge is not a '
                       f'release, so nothing is published')
    if tag_commit and tag_commit != sha:
        return False, (f'{TAG_PREFIX}{version} is already tagged at {tag_commit[:12]}: this merge brings an '
                       f'already-published release back onto its branch, so nothing new is published')
    return True, f'the version file moves {parent_version or "<missing>"} -> {version}: publishing {version}'


# ---- git and CLI plumbing -----------------------------------------------------------------------

def _git(*args: str) -> Optional[str]:
    proc = subprocess.run(['git', *args], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _show(rev: str, path: str) -> Optional[str]:
    return _git('show', f'{rev}:{path}')


def _first_line(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    lines = text.splitlines()
    return (lines[0].strip() if lines else '') or None


def _tag_commit(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    out = _git('rev-parse', '-q', '--verify', f'refs/tags/{TAG_PREFIX}{version}^{{commit}}')
    return out.strip() if out else None


def _outputs(values: Dict[str, object]) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    lines = [f'{k}={str(v).lower() if isinstance(v, bool) else v}' for k, v in values.items()]
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')
    for line in lines:
        print(f'output: {line}')


def _summary(text: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(text + '\n')


def cmd_guard(args: argparse.Namespace) -> int:
    with open(args.changed_files, encoding='utf-8') as handle:
        changed = [line.strip() for line in handle if line.strip()]
    labels = json.loads(args.labels or '[]') or []
    head_version = _first_line(_show(args.head_sha, args.version_file))
    verdict = decide(
        changed, dist_prefix=args.dist_prefix, test_prefix=args.test_prefix, image_prefix=args.image_prefix,
        version_file=args.version_file, head_ref=args.head_ref, base_ref=args.base_ref,
        same_repo=args.same_repo == 'true', labels=labels, head_version=head_version,
        base_changelog=_show(args.base_sha, args.changelog) or '',
        head_changelog=_show(args.head_sha, args.changelog) or '',
        tag_exists=_tag_commit(head_version) is not None)
    for notice in cast(List[str], verdict['notices']):
        print(f'::notice::{notice}')
    for error in cast(List[str], verdict['errors']):
        _summary(error + '\n')
        print(f'::error::{error}')
    _outputs({'version_changed': verdict['version_changed'], 'pypi_check': verdict['pypi_check']})
    return 0 if verdict['ok'] else 1


def cmd_publish_gate(args: argparse.Namespace) -> int:
    version = _first_line(_show(args.sha, args.version_file))
    parent_version = _first_line(_show(f'{args.sha}^1', args.version_file))
    publish, why = publish_decision(parent_version, version, _tag_commit(version), args.sha)
    print(f'::notice::{why}')
    if not publish:
        _summary(f'## Nothing published\n\n{why}.\n')
    _outputs({'publish': publish, 'version': version or ''})
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    with open(args.changelog, encoding='utf-8') as handle:
        changelog = handle.read()
    with open(args.version_file, encoding='utf-8') as handle:
        current = handle.read().strip()
    result = cut_changelog(changelog, current, args.date)
    if result is None:
        print(f'::notice::"## {UNRELEASED}" in {args.changelog} is empty: nothing to release, no pull request.')
        _outputs({'version': ''})
        return 0
    new_changelog, version = result
    with open(args.dockerfile, encoding='utf-8') as handle:
        dockerfile = sync_arg(handle.read(), args.arg, version)
    with open(args.changelog, 'w', encoding='utf-8') as handle:
        handle.write(new_changelog)
    with open(args.version_file, 'w', encoding='utf-8') as handle:
        handle.write(version + '\n')
    with open(args.dockerfile, 'w', encoding='utf-8') as handle:
        handle.write(dockerfile)
    moved = len(entries(section(changelog, UNRELEASED)))
    print(f'::notice::cut {version} from {current}: {moved} line(s) moved under "## {version} ({args.date})".')
    _outputs({'version': version, 'previous': current, 'moved': moved})
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)

    guard = sub.add_parser('guard', help='check-version-bump.yaml verdict on one pull request')
    for name in ('changed-files', 'base-sha', 'head-sha', 'head-ref', 'base-ref', 'version-file',
                 'changelog', 'dist-prefix', 'test-prefix'):
        guard.add_argument(f'--{name}', required=True)
    guard.add_argument('--image-prefix', default='')
    guard.add_argument('--same-repo', choices=('true', 'false'), required=True)
    guard.add_argument('--labels', default='[]', help='JSON list of label names')
    guard.set_defaults(func=cmd_guard)

    gate = sub.add_parser('publish-gate', help='publish only on a version change')
    gate.add_argument('--sha', required=True)
    gate.add_argument('--version-file', required=True)
    gate.set_defaults(func=cmd_publish_gate)

    cut = sub.add_parser('cut', help='move ## Unreleased under a new release heading')
    for name in ('changelog', 'version-file', 'dockerfile', 'arg', 'date'):
        cut.add_argument(f'--{name}', required=True)
    cut.set_defaults(func=cmd_cut)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    sys.exit(main())
