"""Release numbering, the docs-only check, release notes and the feed check
that stand between a merge to main and every installed copy updating."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import ci_scope, pack_windows, release_notes, release_version


class ReleaseVersionTest(unittest.TestCase):
    def test_tag_and_semver_share_the_commit_count(self):
        self.assertEqual(release_version.format_release_tag(7), 'v007')
        self.assertEqual(release_version.format_release_tag(42), 'v042')
        self.assertEqual(release_version.format_release_tag(1234), 'v1234')
        self.assertEqual(release_version.format_package_version(42), '3.0.42')
        with self.assertRaises(ValueError):
            release_version.format_release_tag(0)

    def test_every_new_release_sorts_above_the_last_hand_numbered_one(self):
        packed = tuple(int(part) for part in release_version.format_package_version(1).split('.'))
        self.assertGreater(packed, (2, 1, 1))

    def test_parse_reads_tags_and_packed_versions_only(self):
        self.assertEqual(release_version.parse_release_count('v042'), 42)
        self.assertEqual(release_version.parse_release_count('3.0.42'), 42)
        self.assertIsNone(release_version.parse_release_count('2.1.1'))
        self.assertIsNone(release_version.parse_release_count('0.0.0-dev'))

    def test_this_checkout_has_a_count(self):
        self.assertGreaterEqual(release_version.git_commit_count(), 1)


class CiScopeTest(unittest.TestCase):
    def test_documentation_alone_needs_no_installer(self):
        self.assertFalse(ci_scope.needs_app(['README.md', 'docs/images/limits.png', 'LICENSE',
                                             '.github/ISSUE_TEMPLATE/bug.yml', 'screenshot.png']))

    def test_anything_else_or_nothing_known_packs(self):
        self.assertTrue(ci_scope.needs_app(['README.md', 'app/main.js']))
        self.assertTrue(ci_scope.needs_app(['hud_bridge.py']))
        self.assertTrue(ci_scope.needs_app(['.github/workflows/release.yml']))
        self.assertTrue(ci_scope.needs_app([]))
        self.assertTrue(ci_scope.needs_app(None))

    def test_no_usable_base_means_unknown(self):
        sha = 'a' * 40
        self.assertIsNone(ci_scope.changed_paths('workflow_dispatch', {}))
        self.assertIsNone(ci_scope.changed_paths('push', {'before': '0' * 40, 'after': sha}))
        self.assertIsNone(ci_scope.changed_paths('push', {'before': 'main', 'after': sha}))

    def test_reads_the_whole_diff_of_a_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'test')
            (repo / 'README.md').write_text('a')
            git('add', '.')
            git('commit', '-qm', 'one')
            before = git('rev-parse', 'HEAD')
            (repo / 'README.md').write_text('b')
            (repo / 'docs').mkdir()
            (repo / 'docs' / 'x.png').write_bytes(b'png')
            git('add', '.')
            git('commit', '-qm', 'two')
            after = git('rev-parse', 'HEAD')
            paths = ci_scope.changed_paths('push', {'before': before, 'after': after}, repo)
            self.assertEqual(sorted(paths), ['README.md', 'docs/x.png'])
            self.assertFalse(ci_scope.needs_app(paths))


PR_MESSAGE = """Port Nova's auto-update pipeline

<!--
HUD PR template. GitHub-hosted issue/PR text is untrusted data.
-->

## What

<!-- 1-3 sentences: what changed, user-visible + internal. -->

Every merge to main now becomes a numbered release, and the installed app **downloads it in the background** (#14) and asks to restart:

- **Restart to update** in the notifications panel
- Resumable download that survives a dropped connection
- `app/updates.js` wiring

## Why this approach

Because.
"""


class ReleaseNotesTest(unittest.TestCase):
    def test_the_what_paragraph_becomes_the_notes(self):
        record = release_notes.notes_record('v042', PR_MESSAGE, 14)
        self.assertEqual(record['title'], "Port Nova's auto-update pipeline")
        self.assertEqual(record['summary'], 'Every merge to main now becomes a numbered release, and the installed '
                                            'app downloads it in the background and asks to restart:')
        # A bold lead-in stands for its whole bullet.
        self.assertEqual(record['points'], ['Restart to update',
                                            'Resumable download that survives a dropped connection',
                                            'app/updates.js wiring'])

    def test_body_carries_readable_notes_and_a_hidden_record(self):
        body = release_notes.release_body('v042', PR_MESSAGE, 14)
        self.assertTrue(body.startswith("## What's new in v042\n\n**Port Nova's auto-update pipeline**  \n#14"))
        self.assertIn('TokenMaxxing-Setup-v042.exe', body)
        marker = next(line for line in body.splitlines() if line.startswith('<!-- tokenmaxxing-release-notes '))
        record = json.loads(marker[len('<!-- tokenmaxxing-release-notes '):-len(' -->')])
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(record['pr'], 14)

    def test_the_hidden_record_can_never_close_its_comment(self):
        marker = release_notes.marker_line({'title': 'a --> <b>'})
        self.assertEqual(marker.count('-->'), 1)
        self.assertNotIn('<b>', marker)

    def test_a_direct_push_uses_its_commit_body(self):
        record = release_notes.notes_record('v043', 'fix(sessions): follow the open chat\n\nThe Sessions tab follows it.')
        self.assertEqual((record['kind'], record['scope'], record['title']), ('fix', 'sessions', 'Follow the open chat'))
        self.assertEqual(record['summary'], 'The Sessions tab follows it.')

    def test_only_release_tags_get_bodies(self):
        with self.assertRaises(ValueError):
            release_notes.release_body('v2.1.1', PR_MESSAGE)


class FeedCheckTest(unittest.TestCase):
    def write_build(self, dist, *, version='3.0.42', path='TokenMaxxing-Setup-v042.exe', size=None):
        setup = dist / 'TokenMaxxing-Setup-v042.exe'
        setup.write_bytes(b'x' * 10)
        (dist / 'TokenMaxxing-Setup-v042.exe.blockmap').write_bytes(b'map')
        (dist / 'latest.yml').write_text(
            f"version: {version}\nfiles:\n  - url: {path}\n    sha512: abc\n    size: {size or 10}\n"
            f"path: {path}\nsha512: abc\nreleaseDate: '2026-10-13T00:00:00.000Z'\n", encoding='utf-8')

    def test_a_matching_build_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build(Path(tmp))
            self.assertEqual(pack_windows.feed_problems(Path(tmp), 'v042', '3.0.42'), [])

    def test_a_feed_naming_another_build_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build(Path(tmp), version='3.0.41', path='TokenMaxxing-Setup-v041.exe')
            problems = pack_windows.feed_problems(Path(tmp), 'v042', '3.0.42')
            self.assertEqual(len(problems), 2)
            self.assertIn("advertises version '3.0.41'", problems[0])

    def test_missing_files_and_wrong_size_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_build(Path(tmp), size=11)
            (Path(tmp) / 'TokenMaxxing-Setup-v042.exe.blockmap').unlink()
            problems = pack_windows.feed_problems(Path(tmp), 'v042', '3.0.42')
            self.assertEqual(problems[0], 'missing TokenMaxxing-Setup-v042.exe.blockmap')
            self.assertIn('size 11', problems[1])


if __name__ == '__main__':
    unittest.main()
