"""Build the unreleased native runtime from reviewed, hash-checked sources.

Run on disposable native build hosts. Never installs into the host's Python or
system directories. This produces build inputs, not a releasable companion.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from worker.platforms.identity import current_target

TARGETS = {'macos-arm64', 'macos-x86_64', 'linux-x86_64'}


def read_pins():
    pins = json.loads((ROOT/'packaging/platforms/runtime-sources.json').read_text())
    if set(pins) != {'python', 'openssl', 'libffi'}:
        raise ValueError('Incomplete reviewed runtime source inventory.')
    for record in pins.values():
        url = urllib.parse.urlsplit(record['url'])
        if (url.scheme != 'https' or url.hostname not in {'www.python.org', 'github.com'}
                or url.username or url.password or url.query or url.fragment
                or not re.fullmatch(r'[a-f0-9]{64}', record['sha256'])
                or type(record['sizeBytes']) is not int or not 0 < record['sizeBytes'] <= 100_000_000):
            raise ValueError('Invalid reviewed runtime source record.')
    return pins


def verify_source(path, record):
    if path.is_symlink() or path.stat().st_size != record['sizeBytes']:
        raise ValueError('Runtime source size or path mismatch.')
    with path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    if checksum != record['sha256']:
        raise ValueError('Runtime source hash mismatch.')


class SourceRedirects(urllib.request.HTTPRedirectHandler):
    hosts=frozenset({'www.python.org','github.com','release-assets.githubusercontent.com'})
    def redirect_request(self, request, file, code, message, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if url.scheme != 'https' or url.hostname not in self.hosts:
            raise ValueError('Unapproved source download redirect.')
        return super().redirect_request(request, file, code, message, headers, newurl)


def download(record, path, *, redirect_handler=None):
    request = urllib.request.Request(record['url'], headers={'User-Agent':'ClipBot-native-source-build'})
    try:
        output = path.open('xb')
    except FileExistsError:
        raise ValueError('Source destination already exists.') from None
    try:
        with output, urllib.request.build_opener(redirect_handler or SourceRedirects()).open(request, timeout=60) as response:
            size = 0
            while chunk := response.read(1024*1024):
                size += len(chunk)
                if size > record['sizeBytes']:
                    raise ValueError('Runtime source download exceeds reviewed size.')
                output.write(chunk)
        verify_source(path, record)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def extract_source(path, destination):
    """Avoid host tar extraction and all symlink/hardlink/special-file semantics."""
    if destination.exists():
        raise ValueError('Use a new source extraction directory.')
    destination.mkdir(mode=0o700, parents=True)
    seen, roots, size = set(), set(), 0
    with tarfile.open(path, 'r:*') as archive:
        for index, entry in enumerate(archive):
            name = entry.name.rstrip('/')
            parts = name.split('/')
            if (index >= 50000 or not name or '\\' in name or ':' in name
                    or PurePosixPath(name).is_absolute() or any(part in {'', '.', '..'} for part in parts)
                    or any(part.rstrip(' .') != part for part in parts)
                    or not (entry.isdir() or entry.isfile())):
                raise ValueError('Unsafe runtime source archive member.')
            if name.casefold() in seen:
                raise ValueError('Duplicate runtime source archive member.')
            seen.add(name.casefold())
            roots.add(parts[0])
            size += entry.size
            if entry.size < 0 or entry.size > 200_000_000 or size > 1_000_000_000:
                raise ValueError('Runtime source archive exceeds limits.')
            target = destination.joinpath(*parts)
            if entry.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with archive.extractfile(entry) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)
    if len(roots) != 1 or not (destination/next(iter(roots))).is_dir():
        raise ValueError('Runtime source archive needs one source directory.')
    return destination/next(iter(roots))


def install_build_ca_bundle(prefix, *, candidates=None):
    """Copy the build host's public roots into our private OpenSSL prefix."""
    if candidates is None:
        candidates = [Path('/etc/ssl/cert.pem')] if sys.platform=='darwin' else [Path('/etc/ssl/certs/ca-certificates.crt')]
    selected = None
    for source in candidates:
        source = Path(source)
        if not source.is_file() or not 0 < source.stat().st_size <= 5_000_000:
            continue
        try:
            context = ssl.create_default_context(cafile=source)
        except (OSError,ssl.SSLError):
            continue
        if context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname and context.cert_store_stats()['x509_ca'] > 0:
            selected = source
            break
    if selected is None:
        raise ValueError('A valid build-host certificate trust bundle is required; TLS verification stays enabled.')
    prefix = Path(prefix).absolute()
    destination = prefix/'ssl/cert.pem'
    if destination.exists() or destination.is_symlink() or (prefix/'ssl').is_symlink():
        raise ValueError('Build certificate destination already exists or is unsafe.')
    destination.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    with selected.open('rb') as source, destination.open('xb') as output:
        shutil.copyfileobj(source,output)
    destination.chmod(0o644)
    return {'source':str(selected),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'sizeBytes':destination.stat().st_size,'caCertificates':context.cert_store_stats()['x509_ca']}


def build(target, output, *, host_target=None):
    if target not in TARGETS or (host_target or current_target()) != target:
        raise ValueError('Runtime build requires a matching native host and processor.')
    pins = read_pins()
    output = Path(output).absolute()
    if output.exists():
        raise ValueError('Runtime builds require a new output directory.')
    output.mkdir(mode=0o700, parents=True)
    # macOS runner temp paths may use the system /var -> /private/var alias.
    output = output.resolve()
    sources = output/'sources'
    sources.mkdir()
    directories = {}
    for name, record in pins.items():
        path = sources/Path(urllib.parse.urlsplit(record['url']).path).name
        download(record, path)
        directories[name] = extract_source(path, output/('source-'+name))
    environment = {key:value for key,value in os.environ.items() if key not in {
        'PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','CONDA_PREFIX','CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS',
        'CPATH','LIBRARY_PATH','DYLD_LIBRARY_PATH','LD_LIBRARY_PATH','CONFIG_SITE','PKG_CONFIG_PATH'}}
    prefix = output/'runtime'
    dependencies = output/'dependencies'
    environment.update(PYTHONNOUSERSITE='1', MACOSX_DEPLOYMENT_TARGET='14.0')
    jobs = str(min(os.cpu_count() or 1, 4))
    commands = []
    log_path = output/'build.log'

    def run(args, cwd):
        commands.append({'cwd':str(cwd.relative_to(output)), 'args':list(map(str, args))})
        with log_path.open('ab') as log:
            subprocess.run(list(map(str, args)), cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1200)

    openssl_target = {'macos-arm64':'darwin64-arm64-cc','macos-x86_64':'darwin64-x86_64-cc','linux-x86_64':'linux-x86_64'}[target]
    run(['perl','./Configure',openssl_target,'shared','no-tests','no-docs',f'--prefix={dependencies}',f'--openssldir={dependencies}/ssl','--libdir=lib'], directories['openssl'])
    for command in (['make','-j'+jobs], ['make','install_sw']):
        run(command, directories['openssl'])
    trust_bundle = install_build_ca_bundle(dependencies)
    run(['./configure',f'--prefix={dependencies}','--disable-docs','--enable-shared','--disable-static'], directories['libffi'])
    for command in (['make','-j'+jobs], ['make','install']):
        run(command, directories['libffi'])
    environment.update(CPPFLAGS=f'-I{dependencies}/include',
                       LDFLAGS=f'-L{dependencies}/lib -Wl,-rpath,{dependencies}/lib -Wl,-rpath,{prefix}/lib',
                       PKG_CONFIG_PATH=str(dependencies/'lib/pkgconfig'))
    if target == 'linux-x86_64':
        environment['LD_LIBRARY_PATH'] = str(dependencies/'lib')+':'+str(prefix/'lib')
    run(['./configure', f'--prefix={prefix}', '--enable-shared','--with-ensurepip=install',
         f'--with-openssl={dependencies}', '--with-openssl-rpath=auto', '--without-static-libpython'], directories['python'])
    for command in (['make','-j'+jobs], ['make','install']):
        run(command, directories['python'])
    python = prefix/'bin/python3.12'
    diagnostics = output/'runtime-diagnostics.json'
    run([python,'-I','-c',
         'import sys,ssl,ctypes,zlib,bz2,lzma,sqlite3,hashlib,json,struct; '
         'assert sys.version_info[:3]==(3,12,15); assert struct.calcsize("P")==8; '
         'assert ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "); '
         'ctx=ssl.create_default_context(); assert ctx.verify_mode==ssl.CERT_REQUIRED and ctx.check_hostname and ctx.cert_store_stats()["x509_ca"]>0; '
         'print(json.dumps({"python":sys.version,"openssl":ssl.OPENSSL_VERSION,"sqlite":sqlite3.sqlite_version}))'], output)
    # Keep source archives, actual configure output and toolchain receipt for review.
    toolchain = {}
    for command in (['cc','--version'],['make','--version'],['perl','-v']):
        toolchain[command[0]] = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout[:8000]
    if target.startswith('macos-'):
        toolchain['sdk'] = subprocess.run(['/usr/bin/xcrun','--show-sdk-version'], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    receipt = {'target':target,'python':pins['python']['version'],'sources':pins,'commands':commands,
               'toolchain':toolchain,'buildTrustBundle':trust_bundle,'packagedAcceptance':False,'publicReleaseApproved':False}
    diagnostics.write_text(json.dumps(receipt, indent=2))
    print('Native runtime built and module checks passed. Companion packaging remains separate.')
    print('Runtime:', python)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=sorted(TARGETS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.target, args.output)
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as error:
        print('Native runtime blocked:', str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
