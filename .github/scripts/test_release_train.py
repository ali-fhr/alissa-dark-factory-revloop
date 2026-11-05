"""Tests for release_train.py (issue #220). Run: python3 -m unittest discover -s .github/scripts

Each acceptance line of the issue has a test here: the guard, the publish gate on a merge without
a version change, the train with nothing to release, and a release:critical cut from vX.Y.Z that
publishes only the fix and whose merge back into main publishes nothing.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release_train as rt  # noqa: E402

DIST = 'alissa-tools-github-revloop/'
TESTS = DIST + 'src/test/'
VERSION_FILE = DIST + 'src/main/alissa/tools/github/revloop/version'
SRC = DIST + 'src/main/alissa/tools/github/revloop/loop.py'
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'release_train.py')

BASE_CL = """# Changelog

Preamble.

## Unreleased

## 0.31.40

- older entry
"""


def with_unreleased(*lines: str, base: str = BASE_CL) -> str:
    return base.replace('## Unreleased\n', '## Unreleased\n\n' + ''.join(f'{line}\n' for line in lines), 1)


def guard(changed, *, head_ref='TASK-1-X', base_ref='main', same_repo=True, labels=(), head_version='0.31.40',
          base_changelog=BASE_CL, head_changelog=BASE_CL, tag_exists=False, tag_elsewhere=False):
    return rt.decide(changed, dist_prefix=DIST, test_prefix=TESTS, image_prefix='docker/',
                     version_file=VERSION_FILE, head_ref=head_ref, base_ref=base_ref, same_repo=same_repo,
                     labels=list(labels), head_version=head_version, base_changelog=base_changelog,
                     head_changelog=head_changelog, tag_exists=tag_exists, tag_elsewhere=tag_elsewhere)


class ChangelogTest(unittest.TestCase):

    def test_section_and_entries_skip_blank_lines_and_comments(self):
        text = with_unreleased('<!-- one', 'still comment -->', '- real <!-- inline --> entry', '')
        self.assertEqual(rt.entries(rt.section(text, 'Unreleased')), ['- real  entry'])
        self.assertEqual(rt.entries(rt.section(text, '0.31.40')), ['- older entry'])
        self.assertIsNone(rt.section(text, '9.9.9'))

    def test_dated_heading_is_keyed_by_its_version(self):
        self.assertEqual(rt.entries(rt.section('## 1.2.3 (2026-11-05)\n- x\n', '1.2.3')), ['- x'])

    def test_added_entries_is_a_multiset_difference(self):
        base = with_unreleased('- a')
        self.assertEqual(rt.added_entries(base, with_unreleased('- a', '- a'), 'Unreleased'), ['- a'])
        self.assertEqual(rt.added_entries(base, base, 'Unreleased'), [])

    def test_next_version(self):
        self.assertEqual(rt.next_version('0.31.40', minor=False), '0.31.41')
        self.assertEqual(rt.next_version('0.31.40', minor=True), '0.32.0')
        with self.assertRaises(ValueError):
            rt.next_version('0.31.40rc1', minor=False)

    def test_minor_tag_must_lead_a_bullet(self):
        self.assertTrue(rt.is_minor(['- [minor] new config key']))
        self.assertTrue(rt.is_minor(['  * [MINOR] x']))
        self.assertFalse(rt.is_minor(['- the `[minor]` left on the approving review of PR #217']))
        self.assertFalse(rt.is_minor(['  continuation [minor]']))

    def test_cut_moves_entries_under_a_dated_heading(self):
        cut = rt.cut_changelog(with_unreleased('- fix one', '  wrapped', '- fix two'), '0.31.40', '2026-11-05')
        assert cut is not None
        new, version = cut
        self.assertEqual(version, '0.31.41')
        self.assertEqual(rt.entries(rt.section(new, 'Unreleased')), [])
        self.assertEqual(rt.entries(rt.section(new, '0.31.41')), ['- fix one', 'wrapped', '- fix two'])
        self.assertIn('## Unreleased\n\n## 0.31.41 (2026-11-05)\n\n- fix one\n', new)
        self.assertEqual(rt.entries(rt.section(new, '0.31.40')), ['- older entry'])

    def test_cut_bumps_minor_on_a_minor_entry(self):
        _, version = rt.cut_changelog(with_unreleased('- [minor] new key', '- fix'), '0.31.40', '2026-11-05')
        self.assertEqual(version, '0.32.0')

    def test_train_has_nothing_to_release_when_unreleased_is_empty(self):
        self.assertIsNone(rt.cut_changelog(BASE_CL, '0.31.40', '2026-11-05'))
        self.assertIsNone(rt.cut_changelog(with_unreleased('<!-- add entries here -->'), '0.31.40', '2026-11-05'))
        self.assertIsNone(rt.cut_changelog('# Changelog\n\n## 0.31.40\n- x\n', '0.31.40', '2026-11-05'))

    def test_sync_arg(self):
        dockerfile = 'FROM x\nARG REVLOOP_VERSION=0.31.40\nRUN pip install y==${REVLOOP_VERSION}\n'
        self.assertIn('ARG REVLOOP_VERSION=0.31.41\n', rt.sync_arg(dockerfile, 'REVLOOP_VERSION', '0.31.41'))
        with self.assertRaises(ValueError):
            rt.sync_arg(dockerfile * 2, 'REVLOOP_VERSION', '0.31.41')


class GuardTest(unittest.TestCase):

    def test_src_change_with_a_changelog_line_and_no_bump_passes(self):
        verdict = guard([SRC, 'CHANGELOG.md'], head_changelog=with_unreleased('- loop change'))
        self.assertTrue(verdict['ok'], verdict['errors'])
        self.assertFalse(verdict['pypi_check'])

    def test_src_change_without_a_changelog_line_fails(self):
        verdict = guard([SRC])
        self.assertFalse(verdict['ok'])
        self.assertIn('## Unreleased', verdict['errors'][0])

    def test_docker_change_needs_a_changelog_line_too(self):
        self.assertFalse(guard(['docker/claude/Dockerfile'])['ok'])
        self.assertTrue(guard(['docker/claude/Dockerfile', 'CHANGELOG.md'],
                              head_changelog=with_unreleased('- image'))['ok'])

    def test_a_line_under_an_old_release_does_not_count(self):
        head = BASE_CL.replace('- older entry\n', '- older entry\n- sneaked in\n')
        self.assertFalse(guard([SRC, 'CHANGELOG.md'], head_changelog=head)['ok'])

    def test_tests_and_repo_paths_need_nothing(self):
        self.assertTrue(guard([TESTS + 'test_loop.py', '.github/workflows/x.yaml', 'README.md'])['ok'])

    def test_version_change_outside_a_release_branch_or_label_fails(self):
        verdict = guard([SRC, VERSION_FILE, 'CHANGELOG.md'], head_version='0.31.41',
                        head_changelog=with_unreleased('- x'))
        self.assertFalse(verdict['ok'])
        self.assertIn('release/X.Y.Z', verdict['errors'][0])

    def test_release_branch_must_name_the_new_version(self):
        head = BASE_CL.replace('## Unreleased\n', '## Unreleased\n\n## 0.31.41 (2026-11-05)\n\n- x\n')
        ok = guard([VERSION_FILE, 'CHANGELOG.md'], head_ref='release/0.31.41', head_version='0.31.41',
                   base_changelog=with_unreleased('- x'), head_changelog=head)
        self.assertTrue(ok['ok'], ok['errors'])
        self.assertTrue(ok['pypi_check'])
        wrong = guard([VERSION_FILE, 'CHANGELOG.md'], head_ref='release/0.31.42', head_version='0.31.41',
                      base_changelog=with_unreleased('- x'), head_changelog=head)
        self.assertFalse(wrong['ok'])
        fork = guard([VERSION_FILE, 'CHANGELOG.md'], head_ref='release/0.31.41', head_version='0.31.41',
                     same_repo=False, base_changelog=with_unreleased('- x'), head_changelog=head)
        self.assertFalse(fork['ok'])
        self.assertIn('fork', fork['errors'][0])

    def test_critical_label_may_change_the_version(self):
        head = BASE_CL.replace('## Unreleased\n', '## Unreleased\n\n## 0.31.41 (2026-11-05)\n\n- hotfix\n')
        verdict = guard([SRC, VERSION_FILE, 'CHANGELOG.md'], head_ref='TASK-9-HOTFIX', base_ref='release/0.31.41',
                        labels=['release:critical'], head_version='0.31.41', head_changelog=head)
        self.assertTrue(verdict['ok'], verdict['errors'])
        self.assertTrue(verdict['pypi_check'])

    def test_merge_back_skips_the_pypi_check_only_when_tagged(self):
        head = BASE_CL.replace('## Unreleased\n', '## Unreleased\n\n## 0.31.41 (2026-11-05)\n\n- hotfix\n')
        args = dict(head_ref='release/0.31.41', head_version='0.31.41', head_changelog=head)
        self.assertFalse(guard([SRC, VERSION_FILE, 'CHANGELOG.md'], tag_exists=True, **args)['pypi_check'])
        self.assertTrue(guard([SRC, VERSION_FILE, 'CHANGELOG.md'], tag_exists=False, **args)['pypi_check'])

    def test_a_tag_the_head_does_not_descend_from_is_no_merge_back(self):
        released = BASE_CL.replace('## 0.31.40', '## 0.31.41\n\n- x\n\n## 0.31.40')
        args = dict(head_ref='release/0.31.41', head_version='0.31.41', head_changelog=released)
        verdict = guard([SRC, VERSION_FILE, 'CHANGELOG.md'], tag_exists=False, tag_elsewhere=True, **args)
        self.assertTrue(verdict['pypi_check'])
        self.assertTrue(any('does not descend from' in n for n in verdict['notices']), verdict['notices'])


class PublishGateTest(unittest.TestCase):

    def test_unchanged_version_publishes_nothing(self):
        self.assertFalse(rt.publish_decision('0.31.40', '0.31.40', None, 'abc')[0])

    def test_changed_version_publishes(self):
        self.assertTrue(rt.publish_decision('0.31.40', '0.31.41', None, 'abc')[0])

    def test_rerun_on_the_tagged_commit_still_publishes(self):
        self.assertTrue(rt.publish_decision('0.31.40', '0.31.41', 'abc', 'abc')[0])

    def test_tag_on_another_commit_publishes_nothing(self):
        self.assertFalse(rt.publish_decision('0.31.40', '0.31.41', 'def', 'abc')[0])

    # Issue #222: a rebase merge of several commits whose bump is not in the last one.
    def test_rebase_merge_bump_in_an_earlier_commit_publishes(self):
        publish, why = rt.publish_decision('0.31.41', '0.31.41', None, 'abc', ('0.31.40', '0.31.41'))
        self.assertTrue(publish)
        self.assertIn('rebase merge', why)

    def test_head_without_a_version_change_publishes_nothing(self):
        publish, why = rt.publish_decision('0.31.41', '0.31.41', None, 'abc', ('0.31.41', '0.31.41'))
        self.assertFalse(publish)
        self.assertNotIn('first-parent comparison only', why)

    def test_rebase_merge_of_an_already_tagged_version_publishes_nothing(self):
        self.assertFalse(rt.publish_decision('0.31.41', '0.31.41', 'def', 'abc', ('0.31.40', '0.31.41'))[0])

    def test_without_the_head_the_gate_says_what_it_assumes(self):
        self.assertIn('first-parent comparison only', rt.publish_decision('0.31.40', '0.31.40', None, 'abc')[1])


class CriticalReleaseScenarioTest(unittest.TestCase):
    """The whole §4.2 item 4 flow in a throwaway repository, through the CLI the workflows call."""

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def read(self, path):
        with open(os.path.join(self.repo, path), encoding='utf-8') as handle:
            return handle.read()

    def run_cli(self, *args):
        out = os.path.join(self.repo, '.gh-output')
        open(out, 'w').close()
        env = dict(os.environ, GITHUB_OUTPUT=out)
        env.pop('GITHUB_STEP_SUMMARY', None)
        proc = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, env=env, capture_output=True, text=True)
        with open(out, encoding='utf-8') as handle:
            values = dict(line.split('=', 1) for line in handle.read().splitlines() if '=' in line)
        os.unlink(out)
        return proc.returncode, values, proc.stdout

    def gate(self, sha, head=None):
        extra = ('--head-sha', head) if head else ()
        return self.run_cli('publish-gate', '--sha', sha, '--version-file', VERSION_FILE, *extra)[1]['publish']

    def guard_cli(self, base, head, head_ref, base_ref, labels='[]'):
        changed = os.path.join(self.tmp.name, 'changed.txt')
        merge_base = self.git('merge-base', base, head)
        with open(changed, 'w', encoding='utf-8') as handle:
            handle.write(self.git('diff', '--no-renames', '--name-only', merge_base, head) + '\n')
        rc, values, out = self.run_cli(
            'guard', '--changed-files', changed, '--base-sha', merge_base, '--head-sha', head,
            '--head-ref', head_ref, '--base-ref', base_ref, '--version-file', VERSION_FILE,
            '--changelog', 'CHANGELOG.md', '--dist-prefix', DIST, '--test-prefix', TESTS,
            '--image-prefix', 'docker/', '--same-repo', 'true', '--labels', labels)
        return rc, values, out

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 't@example.invalid')
        self.git('config', 'user.name', 't')
        self.git('config', 'commit.gpgsign', 'false')
        self.write(VERSION_FILE, '1.0.0\n')
        self.write(SRC, 'base\n')
        self.write('docker/claude/Dockerfile', 'ARG REVLOOP_VERSION=1.0.0\n')
        self.write('CHANGELOG.md', '# Changelog\n\n## Unreleased\n\n## 1.0.0\n\n- first\n')
        self.git('tag', 'v1.0.0', self.commit('release 1.0.0'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_critical_cut_from_the_tag_publishes_only_the_fix_and_merge_back_publishes_nothing(self):
        # main moves on: a feature with its Unreleased line, merged without a bump.
        self.git('switch', '-q', '-c', 'feature')
        self.write(DIST + 'src/main/alissa/tools/github/revloop/feature.py', 'feature\n')
        self.write('CHANGELOG.md', self.read('CHANGELOG.md').replace('## Unreleased\n', '## Unreleased\n\n- feature\n'))
        feature = self.commit('feature')
        self.assertEqual(self.guard_cli('main', feature, 'feature', 'main')[0], 0)
        self.git('switch', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge feature', 'feature')
        self.assertEqual(self.gate('HEAD'), 'false')  # a merge is not a release

        # The critical release branch is cut from the tag, not from main.
        self.git('switch', '-q', '-c', 'release/1.0.1', 'v1.0.0')
        self.git('switch', '-q', '-c', 'hotfix')
        self.write(SRC, 'fixed\n')
        self.write(VERSION_FILE, '1.0.1\n')
        self.write('docker/claude/Dockerfile', 'ARG REVLOOP_VERSION=1.0.1\n')
        self.write('CHANGELOG.md', self.read('CHANGELOG.md').replace(
            '## Unreleased\n', '## Unreleased\n\n## 1.0.1 (2026-11-05)\n\n- hotfix\n'))
        hotfix = self.commit('hotfix + bump')
        rc, values, out = self.guard_cli('release/1.0.1', hotfix, 'hotfix', 'release/1.0.1')
        self.assertEqual(rc, 1, out)  # the bump needs the label
        rc, values, out = self.guard_cli('release/1.0.1', hotfix, 'hotfix', 'release/1.0.1', '["release:critical"]')
        self.assertEqual(rc, 0, out)
        self.assertEqual(values['pypi_check'], 'true')

        self.git('switch', '-q', 'release/1.0.1')
        self.git('merge', '-q', '--no-ff', '-m', 'merge hotfix', 'hotfix')
        released = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.gate(released), 'true')
        files = self.git('ls-tree', '-r', '--name-only', released).splitlines()
        self.assertNotIn(DIST + 'src/main/alissa/tools/github/revloop/feature.py', files)
        self.assertNotIn('- feature', self.git('show', f'{released}:CHANGELOG.md'))
        self.git('tag', 'v1.0.1', released)  # package-publish.yaml tags what it published

        # Merge back: allowed, PyPI check skipped, and the publish gate publishes nothing.
        rc, values, out = self.guard_cli('main', 'release/1.0.1', 'release/1.0.1', 'main')
        self.assertEqual(rc, 0, out)
        self.assertEqual(values['pypi_check'], 'false')
        self.git('switch', '-q', 'main')
        merge = subprocess.run(['git', 'merge', '-q', '--no-ff', '-m', 'merge back release/1.0.1', 'release/1.0.1'],
                               cwd=self.repo, capture_output=True, text=True)
        # Both sides edited the lines below "## Unreleased", so the merge-back conflicts on
        # CHANGELOG.md, and only there. The resolution keeps both: main's pending entries
        # under Unreleased, and the released section below them.
        self.assertNotEqual(merge.returncode, 0)
        self.assertEqual(self.git('diff', '--name-only', '--diff-filter=U'), 'CHANGELOG.md')
        self.write('CHANGELOG.md', '# Changelog\n\n## Unreleased\n\n- feature\n\n## 1.0.1 (2026-11-05)\n\n'
                                   '- hotfix\n\n## 1.0.0\n\n- first\n')
        self.commit('merge back release/1.0.1')
        self.assertEqual(self.read(SRC), 'fixed\n')
        self.assertEqual(self.gate('HEAD'), 'false')
        # main keeps the feature under Unreleased for the next train, which now cuts 1.0.2.
        rc, values, _ = self.run_cli('cut', '--changelog', 'CHANGELOG.md', '--version-file', VERSION_FILE,
                                     '--dockerfile', 'docker/claude/Dockerfile', '--arg', 'REVLOOP_VERSION',
                                     '--date', '2026-11-06')
        self.assertEqual((rc, values['version']), (0, '1.0.2'))
        self.assertIn('ARG REVLOOP_VERSION=1.0.2', self.read('docker/claude/Dockerfile'))
        self.assertEqual(rt.entries(rt.section(self.read('CHANGELOG.md'), '1.0.2')), ['- feature'])

    def bump(self, version):
        self.write(VERSION_FILE, version + '\n')
        self.write('docker/claude/Dockerfile', f'ARG REVLOOP_VERSION={version}\n')
        self.write('CHANGELOG.md', self.read('CHANGELOG.md').replace(
            '## Unreleased\n', f'## Unreleased\n\n## {version} (2026-11-05)\n\n- fix\n'))

    def test_rebase_merge_with_the_bump_in_an_earlier_commit_publishes(self):
        # Issue #222: release/1.0.1 carries the bump first and another commit after it.
        self.git('switch', '-q', '-c', 'release/1.0.1')
        self.bump('1.0.1')
        self.commit('bump 1.0.1')
        self.write(SRC, 'fixed\n')
        head = self.commit('fix')
        # main moves on meanwhile, so the rebase merge replays both commits onto a new tip.
        self.git('switch', '-q', 'main')
        self.write('README.md', 'readme\n')
        self.commit('unrelated')
        self.git('cherry-pick', 'release/1.0.1~1', 'release/1.0.1')  # what a rebase merge writes
        merged = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('show', f'{merged}:{VERSION_FILE}'), '1.0.1')
        self.assertEqual(self.gate(merged), 'false')  # the first-parent comparison alone misses it
        self.assertEqual(self.gate(merged, head), 'true')
        rc, values, out = self.run_cli('publish-gate', '--sha', merged, '--version-file', VERSION_FILE,
                                       '--head-sha', '0' * 40)
        self.assertEqual(values['publish'], 'false')
        self.assertIn('::warning::', out)  # an unreadable head is said, not silent

    def test_squash_and_merge_commit_with_the_head_publish_nothing_without_a_bump(self):
        self.git('switch', '-q', '-c', 'feature')
        self.write(SRC, 'feature\n')
        head = self.commit('feature')
        self.git('switch', '-q', 'main')
        self.git('merge', '-q', '--squash', 'feature')
        squashed = self.commit('feature (#1)')
        self.assertEqual(self.gate(squashed, head), 'false')
        # A release merged on main after the feature forked does not count as the feature's bump.
        self.git('switch', '-q', '-c', 'feature2', 'HEAD~1')
        self.write('README.md', 'two\n')
        head2 = self.commit('feature2')
        self.git('switch', '-q', 'main')
        self.bump('1.0.1')
        self.commit('release 1.0.1')
        self.git('merge', '-q', '--no-ff', '-m', 'merge feature2', 'feature2')
        self.assertEqual(self.gate('HEAD', head2), 'false')

    def test_reused_release_branch_not_descending_from_its_tag_keeps_the_pypi_check(self):
        # v1.0.1 exists on a commit cut from v1.0.0; release/1.0.1 is then re-made off main.
        self.git('switch', '-q', '-c', 'old', 'v1.0.0')
        self.bump('1.0.1')
        self.git('tag', 'v1.0.1', self.commit('the real 1.0.1'))
        self.git('switch', '-q', '-c', 'release/1.0.1', 'main')
        self.write(SRC, 'other tree\n')
        self.bump('1.0.1')
        head = self.commit('another 1.0.1')
        rc, values, out = self.guard_cli('main', head, 'release/1.0.1', 'main')
        self.assertEqual(rc, 0, out)
        self.assertEqual(values['pypi_check'], 'true')
        self.assertIn('does not descend from', out)

    def test_train_cli_changes_nothing_when_unreleased_is_empty(self):
        before = self.git('status', '--porcelain')
        rc, values, out = self.run_cli('cut', '--changelog', 'CHANGELOG.md', '--version-file', VERSION_FILE,
                                       '--dockerfile', 'docker/claude/Dockerfile', '--arg', 'REVLOOP_VERSION',
                                       '--date', '2026-11-06')
        self.assertEqual((rc, values['version']), (0, ''))
        self.assertIn('nothing to release', out)
        self.assertEqual(self.git('status', '--porcelain'), before)


if __name__ == '__main__':
    unittest.main()
