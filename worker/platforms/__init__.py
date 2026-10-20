"""Native integration adapters, imported lazily on the matching OS."""
from importlib import import_module

from .identity import current_target


def integration():
    target = current_target()
    module = 'windows' if target.startswith('windows-') else 'macos' if target.startswith('macos-') else 'linux'
    return import_module(f'{__name__}.{module}')
