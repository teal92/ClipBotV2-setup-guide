"""Prepare reviewable native dependency evidence, never install or auto-approve it."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.tags import compatible_tags, cpython_tags, mac_platforms
    from packaging.utils import canonicalize_name, parse_wheel_filename, parse_sdist_filename
except ModuleNotFoundError as error:
    namespace = sys.modules.get('packaging')
    if (error.name not in {'packaging','packaging.markers'}
            or (namespace is not None and getattr(namespace,'__file__',None) is not None)):
        raise
    # The hash-checked CPython source bundles ensurepip. Use its vendored pure
    # parser only to bootstrap the exact reviewed build environment offline.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.tags import compatible_tags, cpython_tags, mac_platforms
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename, parse_sdist_filename

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'macos-arm64','macos-x86_64','linux-x86_64'}
SOURCE_BUILDS = {'av', 'pygobject', 'pycairo'}
ROOT_PACKAGES = {'fastapi','uvicorn','httpx','faster-whisper','pystray','pillow','psutil','yt-dlp',
                 'pyinstaller','cython','setuptools','wheel','packaging','pip','pytest','pyyaml'}


def supported_tags(target):
    if target not in TARGETS:
        raise ValueError('Unsupported native dependency target.')
    if target.startswith('macos-'):
        platforms = list(mac_platforms((14,0), 'arm64' if target.endswith('arm64') else 'x86_64'))
    else:
        platforms = [f'manylinux_2_{minor}_x86_64' for minor in range(39,16,-1)]
        platforms += ['manylinux2014_x86_64']
    return list(cpython_tags((3,12), ['cp312'], platforms)) + list(compatible_tags((3,12), 'cp312', platforms))


def select_artifact(metadata, name, version, target, *, source=False):
    info = metadata['info']
    if canonicalize_name(info['name']) != canonicalize_name(name) or info['version'] != version:
        raise ValueError('Registry metadata differs from reviewed package name/version.')
    if info.get('requires_python') and '3.12.15' not in SpecifierSet(info['requires_python']):
        raise ValueError('Dependency does not support the reviewed Python runtime.')
    priorities = {tag:index for index,tag in enumerate(supported_tags(target))}
    candidates = []
    for entry in metadata['urls']:
        filename = entry['filename']
        if entry.get('yanked'):
            continue
        if source:
            if not filename.endswith('.tar.gz'):
                continue
            source_name, source_version = parse_sdist_filename(filename)
            if canonicalize_name(source_name) != canonicalize_name(name) or str(source_version) != version:
                raise ValueError('Source identity differs from reviewed dependency.')
            priority = 0
        else:
            if not filename.endswith('.whl'):
                continue
            wheel_name, wheel_version, _, tags = parse_wheel_filename(filename)
            if canonicalize_name(wheel_name) != canonicalize_name(name) or str(wheel_version) != version:
                raise ValueError('Wheel identity differs from reviewed dependency.')
            matching = tags & priorities.keys()
            if not matching:
                continue
            priority = min(priorities[tag] for tag in matching)
        url = urllib.parse.urlsplit(entry['url'])
        digest = entry['digests']['sha256']
        if (not re.fullmatch(r'[A-Za-z0-9_.+-]{1,230}', filename)
                or url.scheme != 'https' or url.hostname != 'files.pythonhosted.org'
                or url.username or url.password or url.query or url.fragment
                or urllib.parse.unquote(url.path).split('/')[-1] != filename
                or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or type(entry['size']) is not int or not 0 < entry['size'] <= 200_000_000):
            raise ValueError('Untrusted dependency artifact metadata.')
        candidates.append((priority, filename, {'filename':filename,'url':entry['url'],
            'sha256':digest,'sizeBytes':entry['size'],'kind':'source' if source else 'wheel',
            'nativeBuildAccepted':False}))
    if not candidates:
        raise ValueError('No reviewed source archive.' if source else 'No compatible wheel for target/minimum OS.')
    return sorted(candidates, key=lambda item:item[:2])[0][2]


def target_environment(target):
    if target not in TARGETS:
        raise ValueError('Unsupported native dependency target.')
    return {**default_environment(), 'python_version':'3.12','python_full_version':'3.12.15',
        'sys_platform':'darwin' if target.startswith('macos-') else 'linux',
        'platform_system':'Darwin' if target.startswith('macos-') else 'Linux',
        'platform_machine':'arm64' if target == 'macos-arm64' else 'x86_64',
        'os_name':'posix','implementation_name':'cpython','extra':''}


def validate_dependencies(requirements, versions, target):
    versions = {canonicalize_name(name):version for name,version in versions.items()}
    environment = target_environment(target)
    errors = []
    for value in requirements or []:
        requirement = Requirement(value)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        key = canonicalize_name(requirement.name)
        if requirement.url or key not in versions or versions[key] not in requirement.specifier:
            errors.append(value)
    return errors


def dependency_closure(packages, target, *, roots=None):
    records = {canonicalize_name(item['name']):item for item in packages}
    environment = target_environment(target)
    pending = list(ROOT_PACKAGES if roots is None else roots)
    if roots is None:
        pending += ['pyobjc-framework-Quartz','macholib','delocate'] if target.startswith('macos-') else ['PyGObject','auditwheel','meson-python','meson','ninja']
    selected = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in selected:
            continue
        if name not in records:
            selected[name] = {'name':name,'blocked':'Dependency missing from reviewed version inventory.'}
            continue
        item = records[name]
        selected[name] = item
        # Marker evaluation uses the target, not the Windows review host.
        for value in item.get('requires', []):
            requirement = Requirement(value)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            pending.append(requirement.name)
    return [selected[name] for name in sorted(selected)]


def reviewed_versions(target):
    if target not in TARGETS:
        raise ValueError('Unsupported native dependency target.')
    # Preserve existing reviewed versions, excluding Windows-only build support.
    versions = json.loads((ROOT/'packaging/approved-worker-build.json').read_text())
    versions = {name:version for name,version in versions.items()
                if canonicalize_name(name) not in {'pefile','pywin32-ctypes','colorama'}}
    versions.update({'Cython':'3.3.0','wheel':'0.48.0'})
    desktop = json.loads((ROOT/'packaging/platforms/desktop-package-versions.json').read_text())
    versions.update(desktop['macos' if target.startswith('macos-') else 'linux'])
    return versions


def snapshot(target, output):
    versions = reviewed_versions(target)
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new dependency evidence path.')

    def fetch(item):
        name, version = item
        requires = []
        try:
            request = urllib.request.Request(f'https://pypi.org/pypi/{name}/{version}/json', headers={'User-Agent':'ClipBot-dependency-review'})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Registry metadata exceeds limit.')
            value = json.loads(data)
            if canonicalize_name(value['info']['name']) != canonicalize_name(name) or value['info']['version'] != version:
                raise ValueError('Registry metadata differs from reviewed package name/version.')
            requires = value['info'].get('requires_dist') or []
            artifact = select_artifact(value, name, version, target, source=canonicalize_name(name) in SOURCE_BUILDS)
            return {'name':name,'version':version,'artifact':artifact,
                    'requires':value['info'].get('requires_dist') or [],
                    'unreviewedDependencies':validate_dependencies(value['info'].get('requires_dist'), versions, target)}
        except (OSError, ValueError, KeyError) as error:
            return {'name':name,'version':version,'requires':requires,'blocked':str(error)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(fetch, sorted(versions.items())))
    packages = dependency_closure(packages, target)
    result = {'target':target,'python':'3.12.15','dependencyLockComplete':False,
              'nativeBuildAccepted':False,'publicReleaseApproved':False,'packages':packages}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    blocked = [item for item in packages if item.get('blocked') or item.get('unreviewedDependencies')]
    print(f'Review evidence saved: {len(packages)} packages, {len(blocked)} blocked records; source builds and review still required.')
    for item in blocked:
        print(item['name'], item.get('blocked') or item['unreviewedDependencies'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=sorted(TARGETS), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        snapshot(args.target, args.output)
    except (OSError, ValueError) as error:
        print('Dependency review blocked:', str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
