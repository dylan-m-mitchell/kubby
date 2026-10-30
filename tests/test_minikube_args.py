"""Tests for the settings → ``minikube`` argv translation."""
from __future__ import annotations

from kubby.installer.minikube import delete_args, start_args, stop_args


def test_empty_settings_emits_bare_start():
    assert start_args({}) == ["minikube", "start"]


def test_blank_values_are_omitted():
    settings = {
        "driver": "",
        "cpus": "  ",
        "memory": "",
        "kubernetes_version": "",
        "addons": [],
        "rootless": False,
    }
    assert start_args(settings) == ["minikube", "start"]


def test_full_settings_emits_every_flag():
    settings = {
        "driver": "podman",
        "container_runtime": "containerd",
        "rootless": True,
        "cpus": "4",
        "memory": "4g",
        "kubernetes_version": "v1.30.0",
        "addons": ["ingress", "metrics-server"],
    }
    assert start_args(settings) == [
        "minikube",
        "start",
        "--driver=podman",
        "--container-runtime=containerd",
        "--rootless",
        "--cpus=4",
        "--memory=4g",
        "--kubernetes-version=v1.30.0",
        "--addons=ingress,metrics-server",
    ]


def test_container_runtime_is_independent_of_driver():
    # The driver runs the node; the runtime runs containers inside it. Both
    # flags must be asked for explicitly so a rootless podman node does not
    # fall back to Docker-in-Docker.
    args = start_args({"driver": "podman", "container_runtime": "containerd"})
    assert args == [
        "minikube", "start",
        "--driver=podman",
        "--container-runtime=containerd",
    ]


def test_blank_container_runtime_lets_minikube_choose():
    assert start_args({"container_runtime": "  "}) == ["minikube", "start"]


def test_blank_container_runtime_asks_for_containerd_when_rootless():
    # minikube's own pick for rootless podman is Docker, which cannot start
    # a daemon inside the user namespace. This is the reported failure.
    settings = {"driver": "podman", "container_runtime": "", "rootless": True}
    assert start_args(settings) == [
        "minikube",
        "start",
        "--driver=podman",
        "--container-runtime=containerd",
        "--rootless",
    ]


def test_explicit_container_runtime_wins_even_when_rootless():
    settings = {"container_runtime": "cri-o", "rootless": True}
    assert start_args(settings) == [
        "minikube",
        "start",
        "--container-runtime=cri-o",
        "--rootless",
    ]


def test_whitespace_is_stripped_from_scalar_flags():
    settings = {"driver": " docker ", "cpus": "2", "memory": " 2g "}
    assert start_args(settings) == [
        "minikube",
        "start",
        "--driver=docker",
        "--cpus=2",
        "--memory=2g",
    ]


def test_blank_addon_entries_are_dropped():
    assert start_args({"addons": [" ingress ", "", "  "]}) == [
        "minikube",
        "start",
        "--addons=ingress",
    ]


def test_non_list_addons_are_ignored():
    assert start_args({"addons": "default"}) == ["minikube", "start"]


def test_non_string_addon_entries_are_stringified():
    assert start_args({"addons": [1, 2]}) == ["minikube", "start", "--addons=1,2"]


def test_missing_keys_are_treated_as_unset():
    assert start_args({"driver": None, "cpus": None, "addons": None}) == [
        "minikube",
        "start",
    ]


def test_stop_and_delete_argv():
    assert stop_args() == ["minikube", "stop"]
    assert delete_args() == ["minikube", "delete"]
