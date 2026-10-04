"""Offline build-policy tests; no container, user service, or hardware actions."""
import importlib.util
import io
import json
import re
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_distribution', ROOT / 'packaging/build-desktop-distribution.py')
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


class DistributionTests(unittest.TestCase):
    def test_workflow_trusts_explicit_source_before_build_and_names_its_revision(self):
        workflow = (ROOT / '.github/workflows/distribution.yml').read_text()
        triggers = workflow.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(re.findall(r'^  ([\w_]+):', triggers, re.M), ['workflow_dispatch'])
        self.assertIn('default: v0.1.0', triggers)
        self.assertIn('required: true', triggers)
        permissions = re.findall(r'^\s+([\w-]+):\s+(read|write|none)\s*$', workflow, re.M)
        self.assertEqual(permissions, [('contents', 'read')])
        for action in re.findall(r'^\s*- uses:\s*(\S+)', workflow, re.M):
            self.assertRegex(action, r'^[\w./-]+@[0-9a-f]{40}$')
        checkout = workflow.split('- uses: actions/checkout@', 1)[1].split('\n      - ', 1)[0]
        self.assertIn('persist-credentials: false', checkout)
        self.assertIn('fetch-depth: 0', checkout)
        self.assertIn('ref: ${{ inputs.source_ref }}', checkout)
        trust = 'git config --global --add safe.directory "$GITHUB_WORKSPACE"'
        self.assertEqual(workflow.count('safe.directory'), 1)
        self.assertIn(trust, workflow)
        self.assertLess(workflow.index('- uses: actions/checkout@'), workflow.index(trust))
        self.assertLess(workflow.index(trust), workflow.index('git rev-parse HEAD'))
        self.assertLess(workflow.index('git rev-parse HEAD'), workflow.index('python3 packaging/'))
        self.assertIn('id: source', workflow)
        self.assertIn('SOURCE_REF: ${{ inputs.source_ref }}', workflow)
        self.assertIn('name: airpods-hr-appimage-review-${{ steps.source.outputs.sha }}', workflow)
        self.assertNotIn('github.sha', workflow)

    def test_source_revision_step_rejects_changed_release_and_treats_ref_as_data(self):
        workflow = (ROOT / '.github/workflows/distribution.yml').read_text()
        step = workflow.split('- name: Record source revision\n', 1)[1].split('\n      - ', 1)[0]
        script = textwrap.dedent(step.split('run: |\n', 1)[1])
        release = 'bafc22b7e91f204fa6f84b33f2c96f57f828171b'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git = root / 'git'
            git.write_text('#!/bin/sh\n[ "$*" = "rev-parse HEAD" ] || exit 2\nprintf "%s\\n" "$TEST_SOURCE_SHA"\n')
            git.chmod(0o700)
            for ref, sha, success in [
                ('v0.1.0', release, True),
                ('v0.1.0', '0' * 40, False),
                ('main; touch injected', '1' * 40, True),
            ]:
                with self.subTest(ref=ref, sha=sha):
                    output = root / 'output'
                    output.unlink(missing_ok=True)
                    result = subprocess.run(
                        ['/bin/sh', '-e', '-c', script], cwd=root,
                        env={'PATH': str(root), 'SOURCE_REF': ref,
                             'TEST_SOURCE_SHA': sha, 'GITHUB_OUTPUT': str(output)},
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    self.assertIn('Requested source ref: ' + ref, result.stdout)
                    self.assertIn('Resolved source SHA: ' + sha, result.stdout)
                    if success:
                        self.assertEqual(output.read_text(), 'sha=' + sha + '\n')
                    else:
                        self.assertFalse(output.exists())
                    self.assertFalse((root / 'injected').exists())

    def test_lock_pins_container_and_tools_and_workflow_does_not_publish(self):
        lock = distribution.load_lock()
        workflow = (ROOT / '.github/workflows/distribution.yml').read_text()
        self.assertIn(lock['container'], workflow)
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('contents: read', workflow)
        for forbidden in ['contents: write', 'id-token: write', 'cargo publish', 'twine upload', 'gh release']:
            self.assertNotIn(forbidden, workflow)
        self.assertEqual(lock['assets']['python']['version'], '3.14.7')

    def test_floating_assets_and_wrong_hash_are_rejected(self):
        lock = distribution.load_lock()
        lock['assets']['python']['url'] = 'https://example.com/latest/python.tar.gz'
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary) / 'lock.json'; p.write_text(json.dumps(lock))
            with self.assertRaises(ValueError):
                distribution.load_lock(p)
            entry = {'url': 'https://example.com/test', 'sha256': '0' * 64}
            cache = Path(temporary) / 'cache'; cache.mkdir()
            with patch.object(distribution.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong content')):
                with self.assertRaises(ValueError):
                    distribution.download(entry, cache)
            self.assertEqual(list(cache.iterdir()), [])

    def test_cached_asset_is_verified_before_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary); content = cache / 'source'; content.write_bytes(b'correct bytes')
            sha = distribution.sha256(content); content.rename(cache / sha)
            entry = {'url': 'https://example.com/not-accessed', 'sha256': sha}
            with patch.object(distribution.urllib.request, 'urlopen', side_effect=AssertionError('no network')):
                self.assertEqual(distribution.download(entry, cache), cache / sha)
                (cache / sha).write_bytes(b'tampered')
                with self.assertRaises(ValueError):
                    distribution.download(entry, cache)

    def test_archive_traversal_and_external_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, target in [('../escape', None), ('safe/link', '/etc/passwd'), ('safe/link', '../../escape')]:
                archive = root / 'input.tar'
                with tarfile.open(archive, 'w') as stream:
                    member = tarfile.TarInfo(name)
                    if target:
                        member.type = tarfile.SYMTYPE; member.linkname = target
                    stream.addfile(member)
                with self.assertRaises(ValueError):
                    distribution.extract(archive, root / 'output')

    def test_glibc_floor_rejects_newer_symbols(self):
        with patch.object(distribution, 'run', return_value='Name: GLIBC_2.17 Name: GLIBC_2.34'):
            self.assertEqual(distribution.glibc_requirements(Path('/fake')), ['2.17', '2.34'])
        with patch.object(distribution, 'run', return_value='Name: GLIBC_2.35'):
            with self.assertRaises(ValueError):
                distribution.glibc_requirements(Path('/fake'))

    def test_pyserial_notice_is_copied_from_source_and_must_be_regular(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'source.tar.gz'
            notice = b'upstream copyright and license text'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('pyserial-3.5/LICENSE.txt')
                member.size = len(notice)
                stream.addfile(member, io.BytesIO(notice))
            distribution.copy_pyserial_license(archive, root / 'notices')
            self.assertEqual((root / 'notices/python-packages/pyserial-3.5/LICENSE.txt').read_bytes(), notice)
            with tarfile.open(archive, 'w:gz') as stream:
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                stream.addfile(member)
            with self.assertRaises(ValueError):
                distribution.copy_pyserial_license(archive, root / 'rejected')
            self.assertFalse((root / 'rejected').exists())

    def test_python_origin_dependency_stays_inside_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / 'python/bin/python'; python.parent.mkdir(parents=True)
            library = root / 'python/lib/libpython.so'; library.parent.mkdir(); library.write_bytes(b'library')
            python.write_bytes(b'executable')
            with patch.object(distribution, 'DYNAMIC_LIBRARIES', []), patch.object(distribution, 'elf_files', return_value=[python]), patch.object(distribution, 'run', return_value=''), patch.object(distribution, 'needed', return_value=['$ORIGIN/../lib/libpython.so']):
                self.assertEqual(distribution.bundle_libraries(root), [])
            with patch.object(distribution, 'DYNAMIC_LIBRARIES', []), patch.object(distribution, 'elf_files', return_value=[python]), patch.object(distribution, 'run', return_value=''), patch.object(distribution, 'needed', return_value=['$ORIGIN/../../../../escape.so']):
                with self.assertRaises(ValueError):
                    distribution.bundle_libraries(root)

    def test_host_build_fails_before_any_mutation_or_download(self):
        with patch.object(distribution, 'run', return_value='glibc 2.44') as commands:
            with self.assertRaisesRegex(ValueError, 'pinned'):
                distribution.build(Path('/unused'))
            self.assertEqual(commands.call_count, 1)
            self.assertEqual(commands.call_args.args[0], ['getconf', 'GNU_LIBC_VERSION'])

    def test_sourcerpm_parsing_preserves_exact_nvr(self):
        source = distribution.parse_sourcerpm('systemd-252-55.el9_7.9.alma.1.src.rpm')
        self.assertEqual(source, {'name': 'systemd', 'epoch': '0', 'version': '252',
                                  'release': '55.el9_7.9.alma.1', 'arch': 'src'})
        source['epoch'] = '2'
        self.assertEqual(distribution.rpm_nevra(source), 'systemd-2:252-55.el9_7.9.alma.1.src')
        self.assertEqual(distribution.source_filename(source), 'systemd-252-55.el9_7.9.alma.1.src.rpm')
        self.assertEqual(distribution.parse_sourcerpm('lib-with-hyphens-1.2-3.el9.src.rpm')['name'], 'lib-with-hyphens')
        for invalid in ['../systemd-252-55.src.rpm', 'systemd.src.rpm', 'systemd-252-55.x86_64.rpm', '(none)']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                distribution.parse_sourcerpm(invalid)

    def test_installed_binary_identity_comes_from_owner_metadata(self):
        record = 'systemd-libs\t0\t252\t55.el9_7.9.alma.1\tx86_64\tsystemd-libs-252-55.el9_7.9.alma.1.x86_64\tLGPLv2+\tsystemd-252-55.el9_7.9.alma.1.src.rpm'
        with patch.object(distribution, 'run', return_value=record) as command:
            actual = distribution.installed_rpm(Path('/usr/lib64/libudev.so.1.7.5'))
        self.assertEqual(actual['sourcerpm'], 'systemd-252-55.el9_7.9.alma.1.src.rpm')
        self.assertEqual(actual['name'], 'systemd-libs')
        self.assertIn('%{SOURCERPM}', command.call_args.args[0][3])
        self.assertIn('-qf', command.call_args.args[0])
        for invalid in [record + '\n' + record, record.replace('systemd-252-55.el9_7.9.alma.1.src.rpm', '(none)')]:
            with patch.object(distribution, 'run', return_value=invalid), self.assertRaises(ValueError):
                distribution.installed_rpm(Path('/fake'))

    def test_source_query_requires_exact_nvr_and_unambiguous_epoch(self):
        filename = 'systemd-252-55.el9_7.9.alma.1.src.rpm'
        correct = 'systemd\t0\t252\t55.el9_7.9.alma.1\tsrc'
        with patch.object(distribution, 'run', return_value=correct + '\n' + correct) as command:
            self.assertEqual(distribution.resolve_source_rpm(filename)['release'], '55.el9_7.9.alma.1')
            self.assertIn(filename[:-4], command.call_args.args[0])
            self.assertIn('repoquery-nevra', command.call_args.args[0])
        for output in ['', correct.replace('55.el9_7.9', '67.el9_8.6'), correct + '\n' + correct.replace('\t0\t', '\t1\t')]:
            with self.subTest(output=output), patch.object(distribution, 'run', return_value=output), self.assertRaises(ValueError):
                distribution.resolve_source_rpm(filename)

    def test_source_fetch_verifies_headers_and_rejects_unavailable_or_mismatched_rpm(self):
        filename = 'systemd-252-55.el9_7.9.alma.1.src.rpm'
        record = 'systemd\t0\t252\t55.el9_7.9.alma.1\tsrc'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for output, download_name, headers, success in [
                (record, filename, record.replace('\tsrc', '\tx86_64') + '\t1', True),
                ('', filename, record + '\t1', False),
                (record, filename, record.replace('55.el9_7.9', '67.el9_8.6') + '\t1', False),
                (record, filename, record.replace('\tsrc', '\tx86_64') + '\t(none)', False),
                (record, 'systemd-252-67.el9_8.6.alma.1.src.rpm', record + '\t1', False),
            ]:
                dest = root / str(len(list(root.iterdir())))
                calls = []
                def command(args, **kwargs):
                    calls.append(args)
                    if 'repoquery-nevra' in args:
                        return output
                    if 'download' in args:
                        (Path(args[args.index('--destdir') + 1]) / download_name).write_bytes(b'source RPM bytes')
                        return ''
                    if '-qp' in args:
                        return headers
                    raise AssertionError(args)
                with self.subTest(success=success, headers=headers), patch.object(distribution, 'run', side_effect=command):
                    if success:
                        mapping = distribution.fetch_source_rpm(filename, dest)
                        self.assertEqual(mapping['sha256'], distribution.sha256(dest / filename))
                        self.assertEqual(mapping['filename'], filename)
                        self.assertEqual(mapping['nevra'], filename[:-4])
                    else:
                        with self.assertRaises(ValueError):
                            distribution.fetch_source_rpm(filename, dest)
                        self.assertFalse((dest / filename).exists())
                        if not output:
                            self.assertFalse(any('download' in args for args in calls))
                    self.assertFalse(list(dest.glob('.source-*')))

    def test_system_inventory_maps_each_library_and_reuses_matching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            appdir = Path(temporary)
            target = appdir / 'licenses'; target.mkdir()
            libraries = []
            for name in ['libudev.so.1', 'libsystemd.so.0']:
                payload = appdir / 'usr/lib' / name; payload.parent.mkdir(parents=True, exist_ok=True)
                payload.write_bytes(name.encode())
                libraries.append((Path('/usr/lib64') / name, payload))
            binary = {'name': 'systemd-libs', 'epoch': '0', 'version': '252', 'release': '55.el9_7.9.alma.1',
                      'arch': 'x86_64', 'nevra': 'systemd-libs-252-55.el9_7.9.alma.1.x86_64',
                      'license': 'LGPLv2+', 'sourcerpm': 'systemd-252-55.el9_7.9.alma.1.src.rpm'}
            source = {'filename': binary['sourcerpm'], 'sha256': 'a' * 64}
            license_file = appdir / 'usr/share/licenses/systemd/COPYING'; license_file.parent.mkdir(parents=True)
            license_file.write_text('installed license text')
            with patch.object(distribution, 'installed_rpm', return_value=binary), patch.object(distribution, 'fetch_source_rpm', return_value=source) as fetch, patch.object(distribution, 'run', return_value=str(license_file)):
                inventory = distribution.collect_system_licenses(appdir, target, libraries)
            fetch.assert_called_once_with(binary['sourcerpm'], target / 'system-library-sources')
            self.assertEqual(len(inventory['libraries']), 2)
            self.assertEqual(inventory['libraries'][0]['binary'], binary)
            self.assertEqual(inventory['libraries'][0]['source']['sha256'], 'a' * 64)
            self.assertEqual(json.loads((target / 'system-packages.json').read_text()), inventory)
            self.assertEqual((target / 'system-libraries' / binary['nevra'] / 'COPYING').read_text(), 'installed license text')
            self.assertNotIn(str(appdir), json.dumps(inventory))
