"""Installer subpackage: detects and installs the managed tools.

Submodules are imported via relative ``from . import ...``. The
fully-qualified ``from kubby.installer import X`` form also works,
but routes through a fromlist-lookup against the partially-initialized
package object during ``__init__`` execution — which surfaces a
misleading ``ImportError: cannot import name 'X' from partially
initialized module 'kubby.installer' (most likely due to a circular
import)`` whenever the referenced submodule file is missing. Relative
imports surface a clearer ``ModuleNotFoundError`` instead, naming the
exact missing file.
"""

from . import detector, linux, minikube, tools

__all__ = ["detector", "linux", "minikube", "tools"]
