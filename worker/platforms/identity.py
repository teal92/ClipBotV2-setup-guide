"""Explicit supported package identities; never infer entitlement from a target."""
import platform

TARGETS = frozenset({'windows-x86_64', 'macos-arm64', 'macos-x86_64', 'linux-x86_64'})


def platform_target(system: str, machine: str) -> str:
    system, machine = system.lower(), machine.lower()
    if machine in ('amd64', 'x86_64'):
        os_name = {'windows': 'windows', 'darwin': 'macos', 'linux': 'linux'}.get(system)
        if os_name:
            return os_name + '-x86_64'
    if system == 'darwin' and machine in ('arm64', 'aarch64'):
        return 'macos-arm64'
    raise ValueError('This operating system or processor is not supported by ClipBot Companion.')


def current_target() -> str:
    return platform_target(platform.system(), platform.machine())
