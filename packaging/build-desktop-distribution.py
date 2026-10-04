#!/usr/bin/env python3
"""Build the review-only x86_64 AppImage inside the pinned AlmaLinux container.

The canonical release validator still owns Python/Rust package validation.
This script adds a separate desktop payload; it never publishes or runs hubd.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/distribution-lock.json'
HOST_LIBRARIES = {
    'libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
    'libresolv.so.2', 'libutil.so.1', 'ld-linux-x86-64.so.2',
    # The host unwind runtime belongs with libc and the graphics driver stack.
    # AppImage's core-library exclusion policy also excludes libgcc_s.
    'libgcc_s.so.1',
    # Graphics implementations must match the user's drivers, not the builder.
    'libGL.so.1', 'libEGL.so.1', 'libGLX.so.0', 'libGLdispatch.so.0',
}
DYNAMIC_LIBRARIES = [
    'libX11.so.6', 'libXcursor.so.1', 'libXi.so.6', 'libXrandr.so.2',
    'libxkbcommon.so.0', 'libxkbcommon-x11.so.0', 'libwayland-client.so.0',
    'libwayland-cursor.so.0', 'libwayland-egl.so.1', 'libusb-1.0.so.0',
]


def run(args, *, cwd=None, env=None, timeout=1800, capture=False):
    result = subprocess.run([os.fspath(a) for a in args], cwd=cwd, env=env,
                            timeout=timeout, check=True, text=True,
                            capture_output=capture, stdin=subprocess.DEVNULL)
    return result.stdout.strip() if capture else ''


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock(path=LOCK):
    lock = json.loads(path.read_text())
    if lock['version'] != '0.1.0' or lock['architecture'] != 'x86_64' or lock['glibc_floor'] != '2.34':
        raise ValueError('unsupported distribution identity')
    if not re.fullmatch(r'docker.io/library/almalinux@sha256:[0-9a-f]{64}', lock['container']):
        raise ValueError('container digest required')
    for entry in lock['assets'].values():
        if not re.fullmatch('[0-9a-f]{64}', entry['sha256']) or not entry['url'].startswith('https://'):
            raise ValueError('verified HTTPS assets required')
        if '/latest/' in entry['url'] or '/continuous/' in entry['url']:
            raise ValueError('floating tool releases are forbidden')
    return lock


def download(entry, cache):
    destination = cache / entry['sha256']
    if destination.exists():
        if sha256(destination) != entry['sha256']:
            raise ValueError('cached asset checksum mismatch')
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 900
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
        staging = Path(stream.name)
        try:
            with urllib.request.urlopen(entry['url'], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise TimeoutError('download deadline exceeded')
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            if sha256(staging) != entry['sha256']:
                raise ValueError('download checksum mismatch')
            staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)
    return destination


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            # Links are allowed only within this archive's extraction root.
            if Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                raise ValueError('unsafe archive member')
            if member.isdev() or member.isfifo():
                raise ValueError('unsupported archive member')
            if member.issym() or member.islnk():
                target = (destination / member.name).parent / member.linkname if member.issym() else destination / member.linkname
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError('archive link escapes extraction root')
        source.extractall(destination)


def elf_files(root):
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                if stream.read(4) == b'\x7fELF':
                    yield path


def glibc_requirements(path):
    output = run(['readelf', '--version-info', path], capture=True, timeout=30)
    versions = {tuple(map(int, value.split('.'))) for value in re.findall(r'GLIBC_(\d+(?:\.\d+)+)', output)}
    if any(version > (2, 34) for version in versions):
        raise ValueError('ELF exceeds glibc 2.34: ' + path.name)
    return sorted('.'.join(map(str, version)) for version in versions)


def needed(path):
    output = run(['readelf', '-d', path], capture=True, timeout=30)
    return re.findall(r'\(NEEDED\).*\[(.*?)\]', output)


def bundle_libraries(appdir):
    destination = appdir / 'usr/lib'
    destination.mkdir(parents=True, exist_ok=True)
    cache = run(['ldconfig', '-p'], capture=True)
    libraries = dict(re.findall(r'^\s*(\S+)\s+\([^\n]+\)\s+=>\s+(\S+)', cache, re.M))
    pending = set(DYNAMIC_LIBRARIES)
    for file in elf_files(appdir):
        for name in needed(file):
            if name.startswith('$ORIGIN/'):
                target = (file.parent / name[len('$ORIGIN/'):]).resolve()
                if not target.is_relative_to(appdir.resolve()) or not target.is_file():
                    raise ValueError('invalid payload-relative ELF dependency')
            else:
                pending.add(name)
    copied = []
    while pending:
        name = pending.pop()
        if name in HOST_LIBRARIES or (destination / name).exists():
            continue
        # Python owns a relocatable libpython next to its interpreter already.
        if any(appdir.glob('usr/lib/airpods-hr-linux/python/lib/' + name)):
            continue
        if name not in libraries:
            # Extension-local shared objects are carried by their wheel.
            if any(appdir.rglob(name)):
                continue
            raise ValueError('unresolved library: ' + name)
        source = Path(libraries[name]).resolve()
        shutil.copy2(source, destination / name)
        pending.update(needed(source))
        copied.append((source, destination / name))
    return copied


def collect_licenses(appdir, work, assets, bundled_libraries, env):
    target = appdir / 'usr/share/licenses/airpods-hr-linux'
    target.mkdir(parents=True)
    shutil.copy2(ROOT / 'LICENSE', target / 'LICENSE')
    shutil.copy2(ROOT / 'packaging/THIRD_PARTY_NOTICES.md', target / 'THIRD_PARTY_NOTICES.md')
    # The full PBS archive includes licenses for libraries linked into Python,
    # which the install-only archive does not carry.
    with subprocess.Popen(['zstd', '-dc', assets['python_full']], stdout=subprocess.PIPE) as decompressor:
        with tarfile.open(fileobj=decompressor.stdout, mode='r|') as archive:
            for member in archive:
                if member.isfile() and (member.name.startswith('python/licenses/') or member.name == 'python/PYTHON.json'):
                    path = target / 'python-runtime' / Path(member.name).name
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(archive.extractfile(member).read())
        if decompressor.wait(timeout=30) != 0:
            raise ValueError('Python license archive failed')
    # Installed wheel licenses stay in their original dist-info directories too.
    python_root = appdir / 'usr/lib/airpods-hr-linux/python'
    for path in python_root.rglob('*'):
        if path.is_file() and path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE', 'COPYRIGHT', 'AUTHORS')):
            dest = target / 'python-packages' / path.relative_to(python_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    # pySerial 3.5's wheel has SPDX headers but omits the full license text.
    # Copy that notice from its checksum-pinned matching source distribution.
    copy_pyserial_license(assets['pyserial_license'], target)
    # Retain complete dependency sources, including font notices and any source
    # obligations. Cargo verifies these downloads against Cargo.lock.
    vendor = work / 'rust-sources'
    run(['cargo', 'vendor', '--locked', '--versioned-dirs', vendor], cwd=ROOT, env=env, capture=True)
    with tarfile.open(target / 'rust-dependency-sources.tar.gz', 'w:gz') as archive:
        archive.add(vendor, arcname='rust-dependency-sources')
        archive.add(ROOT / 'Cargo.lock', arcname='Cargo.lock')
    run(['git', 'archive', '--format=tar.gz', '--output=' + str(target / 'project-source.tar.gz'), 'HEAD'], cwd=ROOT)
    sources = target / 'appimage-runtime-sources'
    sources.mkdir()
    for name, asset in assets.items():
        if name.endswith('_source'):
            suffix = '.tar.xz' if name == 'fuse_source' else '.tar.gz'
            shutil.copy2(asset, sources / (name + suffix))
    return collect_system_licenses(appdir, target, bundled_libraries)


RPM_FIELDS = ('name', 'epoch', 'version', 'release', 'arch')
RPM_FORMAT = '\t'.join('%{' + field.upper() + '}' if field != 'epoch' else '%{EPOCHNUM}'
                       for field in RPM_FIELDS)
# The pinned 9.7 container can retain libraries no longer in the active 9.x
# repositories. Make its official vault source repositories available too.
SOURCE_REPO_OPTIONS = [
    '--repofrompath=airpods-vault-' + repo.lower() + '-source,'
    + 'https://repo.almalinux.org/vault/9.7/' + repo + '/Source/'
    for repo in ('BaseOS', 'AppStream', 'CRB')
]


def parse_rpm_identity(record):
    fields = record.split('\t')
    if len(fields) != 5 or any('\n' in field for field in fields):
        raise ValueError('expected one RPM identity record')
    identity = dict(zip(RPM_FIELDS, fields))
    if not re.fullmatch(r'[0-9]+', identity['epoch']):
        raise ValueError('invalid RPM epoch')
    for field in ('name', 'version', 'release', 'arch'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+_.~^:-]*', identity[field]):
            raise ValueError('invalid RPM ' + field)
    return identity


def rpm_nevra(identity):
    epoch = identity['epoch'] + ':' if int(identity['epoch']) else ''
    return (identity['name'] + '-' + epoch + identity['version'] + '-'
            + identity['release'] + '.' + identity['arch'])


def source_filename(identity):
    return (identity['name'] + '-' + identity['version'] + '-'
            + identity['release'] + '.src.rpm')


def parse_sourcerpm(filename):
    if not filename.endswith('.src.rpm') or '/' in filename:
        raise ValueError('source RPM filename required')
    parts = filename[:-len('.src.rpm')].rsplit('-', 2)
    if len(parts) != 3:
        raise ValueError('invalid SOURCERPM identity')
    identity = parse_rpm_identity('\t'.join([parts[0], '0', parts[1], parts[2], 'src']))
    if source_filename(identity) != filename:
        raise ValueError('invalid SOURCERPM identity')
    # SOURCERPM does not encode the epoch. Resolve that from repository metadata.
    return identity


def installed_rpm(source):
    record = run(['rpm', '-qf', '--qf', RPM_FORMAT + '\t%{NEVRA}\t%{LICENSE}\t%{SOURCERPM}',
                  source], capture=True)
    fields = record.split('\t')
    if len(fields) != 8:
        raise ValueError('expected one owning binary RPM')
    identity = parse_rpm_identity('\t'.join(fields[:5]))
    identity.update(nevra=fields[5], license=fields[6], sourcerpm=fields[7])
    if identity['arch'] == 'src' or not identity['license'] or not identity['nevra']:
        raise ValueError('invalid owning binary RPM')
    parse_sourcerpm(identity['sourcerpm'])
    return identity


def resolve_source_rpm(filename):
    expected = parse_sourcerpm(filename)
    query_format = '\t'.join('%{' + field + '}' for field in RPM_FIELDS)
    output = run(['dnf', '-q', *SOURCE_REPO_OPTIONS, 'repoquery-nevra', '--available',
                  '--archlist=src', '--qf', query_format, filename[:-4]],
                 capture=True, timeout=600)
    matches = []
    for line in output.splitlines():
        identity = parse_rpm_identity(line)
        if identity['arch'] != 'src' or any(identity[k] != expected[k]
                                         for k in ('name', 'version', 'release')):
            raise ValueError('repository returned a mismatched source RPM for ' + filename)
        if identity not in matches:
            matches.append(identity)
    if len(matches) != 1:
        raise ValueError('exact source RPM unavailable or ambiguous: ' + filename)
    return matches[0]


def fetch_source_rpm(filename, destination):
    identity = resolve_source_rpm(filename)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination, prefix='.source-') as temporary:
        staging = Path(temporary)
        run(['dnf', *SOURCE_REPO_OPTIONS, 'download', '--source', '--destdir', staging,
             rpm_nevra(identity)], timeout=600)
        files = list(staging.iterdir())
        if len(files) != 1 or files[0].name != filename or not files[0].is_file() or files[0].is_symlink():
            raise ValueError('exact source RPM was not downloaded: ' + filename)
        # SRPM headers retain the build architecture (often x86_64), while
        # repository metadata calls their architecture src. Check SOURCEPACKAGE
        # rather than mistaking a renamed binary RPM for source.
        headers = run(['rpm', '-qp', '--qf', RPM_FORMAT + '\t%{SOURCEPACKAGE}', files[0]], capture=True).split('\t')
        if len(headers) != 6 or headers[5] != '1':
            raise ValueError('downloaded RPM is not a source package: ' + filename)
        actual = parse_rpm_identity('\t'.join(headers[:5]))
        if {**actual, 'arch': 'src'} != identity or source_filename(actual) != filename:
            raise ValueError('downloaded source RPM metadata mismatch: ' + filename)
        digest = sha256(files[0])
        files[0].replace(destination / filename)
    return {'identity': identity, 'rpm_header': actual, 'nevra': rpm_nevra(identity), 'filename': filename,
            'path': 'system-library-sources/' + filename, 'sha256': digest}


def collect_system_licenses(appdir, target, bundled_libraries):
    packages, sources, mappings = {}, {}, []
    for source, payload in sorted(bundled_libraries):
        binary = installed_rpm(source)
        key = binary['nevra']
        if key not in packages:
            filename = binary['sourcerpm']
            if filename not in sources:
                sources[filename] = fetch_source_rpm(filename, target / 'system-library-sources')
            packages[key] = {'binary': binary, 'source': sources[filename]}
            for item in run(['rpm', '-ql', key], capture=True).splitlines():
                file = Path(item)
                if file.is_file() and '/usr/share/licenses/' in item:
                    dest = target / 'system-libraries' / key / file.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, dest)
        mappings.append({'library': str(payload.relative_to(appdir)),
                         'library_sha256': sha256(payload), **packages[key]})
    inventory = {'schema_version': 1, 'libraries': mappings, 'packages': packages}
    (target / 'system-packages.json').write_text(json.dumps(inventory, indent=2, sort_keys=True) + '\n')
    return inventory


def copy_pyserial_license(archive_path, target):
    with tarfile.open(archive_path) as archive:
        member = archive.getmember('pyserial-3.5/LICENSE.txt')
        if not member.isfile() or member.size > 65536:
            raise ValueError('invalid pySerial license member')
        destination = target / 'python-packages/pyserial-3.5/LICENSE.txt'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.extractfile(member).read())


def smoke_demo(image, work):
    isolated = work / 'demo-home'
    isolated.mkdir()
    config, data, runtime = (isolated / name for name in ['config', 'data', 'runtime'])
    for directory in [config, data, runtime]:
        directory.mkdir(mode=0o700)
    env = {**os.environ, 'HOME': str(isolated), 'XDG_CONFIG_HOME': str(config),
           'XDG_DATA_HOME': str(data), 'XDG_RUNTIME_DIR': str(runtime),
           'DISPLAY': ':91', 'PATH': '/nonexistent', 'LIBGL_ALWAYS_SOFTWARE': '1'}
    env.pop('WAYLAND_DISPLAY', None)
    server = subprocess.Popen(['/usr/bin/Xvfb', ':91', '-screen', '0', '1280x820x24', '-nolisten', 'tcp'])
    app = None
    try:
        deadline = time.monotonic() + 5
        while not Path('/tmp/.X11-unix/X91').exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise ValueError('demo display could not start')
            time.sleep(0.1)
        # The container does not need FUSE privileges for this smoke run.
        # --demo bypasses packaged detection/bootstrap and never starts Python.
        app = subprocess.Popen([str(image), '--appimage-extract-and-run', '--demo'], env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        if app.poll() is not None:
            raise ValueError('bundled demo exited during startup')
        if any(config.rglob('*')) or any(data.rglob('*')):
            raise ValueError('demo mutated daemon configuration or data')
    finally:
        for process in [app, server]:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def build(output):
    lock = load_lock()
    if platform.machine() != 'x86_64' or run(['getconf', 'GNU_LIBC_VERSION'], capture=True) != 'glibc 2.34':
        raise ValueError('build only inside the pinned x86_64 glibc 2.34 container')
    os_release = Path('/etc/os-release').read_text()
    if 'ID="almalinux"' not in os_release and 'ID=almalinux\n' not in os_release:
        raise ValueError('AlmaLinux build environment required')
    if run(['git', 'status', '--porcelain=v1'], cwd=ROOT, capture=True):
        raise ValueError('clean committed source required')
    if output.exists() and any(output.iterdir()):
        raise ValueError('output directory must be empty')
    output.mkdir(parents=True, exist_ok=True)
    run(['dnf', 'install', '-y', 'gcc', 'gcc-c++', 'make', 'git', 'binutils', 'file', 'pkgconf-pkg-config',
         'libX11-devel', 'libXcursor', 'libXi', 'libXrandr', 'libxkbcommon-devel', 'libxkbcommon-x11',
         'wayland-devel', 'libusb1', 'libatomic', 'mesa-libGL', 'mesa-libEGL', 'openssl-devel', 'zstd',
         'appstream', 'desktop-file-utils', 'dnf-plugins-core', 'xorg-x11-server-Xvfb'])
    run(['dnf', 'config-manager', '--set-enabled', 'baseos-source', 'appstream-source', 'crb-source'])
    commit = run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture=True)
    with tempfile.TemporaryDirectory(prefix='airpods-distribution-') as temporary:
        work = Path(temporary)
        cache = ROOT / 'target/distribution-cache'
        assets = {key: download(value, cache) for key, value in lock['assets'].items()}
        rust = work / 'rust'
        extract(assets['rust'], rust)
        prefix = work / 'toolchain'
        run([next(rust.glob('*/install.sh')), '--prefix=' + str(prefix), '--without=rust-docs'])
        tools = work / 'tools'
        extract(assets['python'], tools)
        python = tools / 'python/bin/python3.14'
        env = {**os.environ, 'PATH': str(prefix / 'bin') + ':' + str(python.parent) + ':' + os.environ['PATH'],
               'SOURCE_DATE_EPOCH': run(['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=ROOT, capture=True),
               'RUSTFLAGS': '--remap-path-prefix=' + str(ROOT) + '=/airpods-source -C target-cpu=x86-64',
               'CARGO_TARGET_DIR': str(ROOT / 'target/distribution')}
        env.pop('PYTHONPATH', None)
        run([python, '-I', '-m', 'pip', 'install', '--disable-pip-version-check', 'build==1.3.0', 'maturin==1.15.0', 'setuptools==84.0.0', 'wheel==0.48.0'], env=env)
        # Preserve the canonical five-artifact release authority unchanged.
        release = work / 'canonical-release'
        run([python, ROOT / 'tools/validate_release.py', '--scope', 'artifacts', '--output-dir', release], cwd=ROOT, env=env, timeout=3600)
        wheel = next(release.glob('airpods_hr_linux-0.1.0-cp314-cp314-manylinux_2_34_x86_64.whl'))
        run(['cargo', 'build', '--release', '--locked', '-p', 'airpods-desktop', '--bins'], cwd=ROOT, env=env)
        appdir = work / 'AirPods-HR.AppDir'
        appdir.mkdir()
        run_dir = appdir / 'usr/bin'
        run_dir.mkdir(parents=True)
        desktop = Path(env['CARGO_TARGET_DIR']) / 'release/airpods-desktop'
        shutil.copy2(desktop, run_dir / 'airpods-desktop')
        shutil.copy2(desktop.with_name('airpods-apprun'), appdir / 'AppRun')
        (appdir / 'airpods-distribution').write_text('airpods-hr-linux AppImage v1\n0.1.0\n')
        payload = appdir / 'usr/lib/airpods-hr-linux'
        extract(assets['python'], payload)
        bundled_python = payload / 'python/bin/python3.14'
        run([bundled_python, '-I', '-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:', '-r', ROOT / 'packaging/python-requirements.lock'], env=env)
        run([bundled_python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel], env=env)
        run([bundled_python, '-I', '-m', 'pip', 'check'], env=env)
        run([bundled_python, '-I', '-c', 'import airpods_hr._airpods_aap_core, airpods_hr.service_installer, bumble, dbus_next'], env=env)
        for directory, filename in [('usr/share/applications', 'AirPods-HR.desktop'), ('usr/share/icons/hicolor/scalable/apps', 'AirPods-HR.svg'), ('usr/share/metainfo', 'io.github.ozankenangungor.AirPodsHR.metainfo.xml')]:
            destination = appdir / directory
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / 'packaging' / filename, destination / filename)
        shutil.copy2(ROOT / 'packaging/AirPods-HR.desktop', appdir / 'AirPods-HR.desktop')
        shutil.copy2(ROOT / 'packaging/AirPods-HR.svg', appdir / 'AirPods-HR.svg')
        run(['desktop-file-validate', appdir / 'AirPods-HR.desktop'])
        run(['appstreamcli', 'validate', '--no-net', appdir / 'usr/share/metainfo/io.github.ozankenangungor.AirPodsHR.metainfo.xml'])
        libraries = bundle_libraries(appdir)
        system_packages = collect_licenses(appdir, work, assets, libraries, env)
        versions = json.loads(run([bundled_python, '-I', '-c', 'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))'], capture=True, env=env))
        elf_audit = {str(path.relative_to(appdir)): glibc_requirements(path) for path in elf_files(appdir)}
        # Offline import check after relocation, without host Python paths.
        relocated = work / 'relocated payload'
        appdir.rename(relocated)
        run([relocated / 'usr/lib/airpods-hr-linux/python/bin/python3.14', '-I', '-c', 'import airpods_hr._airpods_aap_core, bumble, dbus_next'], cwd=work, env=env)
        relocated.rename(appdir)
        tool = work / 'appimage-tool'
        tool.mkdir()
        assets['appimagetool'].chmod(0o700)
        run([assets['appimagetool'], '--appimage-extract'], cwd=tool, capture=True)
        image = output / 'AirPods-HR-0.1.0-x86_64.AppImage'
        run([tool / 'squashfs-root/AppRun', '--runtime-file', assets['runtime'], appdir, image], env={**env, 'ARCH': 'x86_64'}, timeout=600)
        smoke_demo(image, work)
        if run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture=True) != commit or run(['git', 'status', '--porcelain=v1'], cwd=ROOT, capture=True):
            raise ValueError('source changed during distribution build')
        manifest = {'schema_version': 1, 'version': lock['version'], 'git_commit': commit, 'clean_tree': True,
                    'architecture': 'x86_64', 'glibc_floor': '2.34', 'container': lock['container'],
                    'desktop_sha256': sha256(desktop), 'python_runtime': lock['assets']['python'],
                    'production_wheel_sha256': sha256(wheel), 'appimage_sha256': sha256(image),
                    'tools': {k: v for k, v in lock['assets'].items() if k in ['rust', 'appimagetool', 'runtime']},
                    'bundled_packages': versions, 'system_packages': system_packages, 'elf_glibc_versions': elf_audit,
                    'reproducibility_claimed': False}
        (output / 'distribution-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        (output / 'SHA256SUMS').write_text(sha256(image) + '  ' + image.name + '\n')
        print('Review artifact built:', image.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/desktop')
    parser.add_argument('--check-lock', action='store_true', help='validate pins without downloads or mutations')
    args = parser.parse_args()
    try:
        if args.check_lock:
            load_lock()
            print('distribution lock: PASS')
        else:
            build(args.output.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, 'distribution build failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
