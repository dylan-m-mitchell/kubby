"""Installer subpackage: detects and installs the managed tools."""

from kubby.installer import detector, linux, minikube, tools

__all__ = ["detector", "linux", "minikube", "tools"]
