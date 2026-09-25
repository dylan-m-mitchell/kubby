"""Installer subpackage: knows about the managed tools and how to find them.

Despite the name, nothing here installs anything. What survives is the
registry of known tools, detection on ``PATH``, and the read-only host
facts the status line reports. The subpackage name is kept because
renaming it would churn every import for no benefit.

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
