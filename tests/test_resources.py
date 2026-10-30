"""Pure tests for the resource argv builders and YAML sanitising.

The fixture is shaped like a real ``kubectl get deployment -o yaml``: keys
alphabetical, two-space indent, list items ``- ``, ``status`` last. That shape
is what makes line-based surgery safe; the tests pin it so a future parser can
be judged against the same payload.
"""

from __future__ import annotations

from kubby.cluster import ResourceRef
from kubby.resources import (
    DIFF_CHANGED,
    DIFF_ERROR,
    DIFF_UNCHANGED,
    apply_args,
    diff_args,
    diff_outcome,
    get_yaml_args,
    sanitize,
)

#: One bare object, as the editor receives it: no List wrapper, no
#: managedFields (kubectl hides them by default).
RAW_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    deployment.kubernetes.io/revision: "2"
  creationTimestamp: "2026-01-02T03:04:05Z"
  generation: 7
  labels:
    app: web
  name: web
  namespace: default
  resourceVersion: "987654"
  uid: 0b9e4f8a-1111-2222-3333-444455556666
spec:
  generation: keep-me
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.27
          args:
            - --listen=:8080
            - --verbose
          ports:
            - containerPort: 80
  strategy:
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
status:
  availableReplicas: 2
  conditions:
    - type: Available
      status: "True"
  observedGeneration: 7
"""

CLEAN_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  annotations:
    deployment.kubernetes.io/revision: "2"
  labels:
    app: web
  name: web
  namespace: default
spec:
  generation: keep-me
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.27
          args:
            - --listen=:8080
            - --verbose
          ports:
            - containerPort: 80
  strategy:
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
"""


class TestArgv:
    def test_get_reads_one_object_and_never_the_list_form(self):
        assert get_yaml_args(ResourceRef("Deployment", "web", "default")) == [
            "kubectl", "get", "deployment", "web", "-n", "default",
            "-o", "yaml", "--show-managed-fields=false",
        ]

    def test_get_omits_namespace_for_a_cluster_scoped_object(self):
        assert get_yaml_args(ResourceRef("Node", "minikube")) == [
            "kubectl", "get", "node", "minikube",
            "-o", "yaml", "--show-managed-fields=false",
        ]
        assert "-A" not in get_yaml_args(ResourceRef("Node", "minikube"))

    def test_diff_and_apply_read_yaml_from_stdin(self):
        assert diff_args() == ["kubectl", "diff", "-f", "-"]
        assert apply_args() == ["kubectl", "apply", "-f", "-"]
        # `-f -` is the stdin marker; a filename or inline YAML would put the
        # object (and a Secret's values) into argv.
        assert all(args[-1] == "-" for args in (diff_args(), apply_args()))


class TestDiffOutcome:
    def test_zero_means_no_differences(self):
        assert diff_outcome(0) == DIFF_UNCHANGED

    def test_one_means_differences_not_failure(self):
        # The trap: exit 1 is the happy path of `kubectl diff`.
        assert diff_outcome(1) == DIFF_CHANGED

    def test_anything_else_is_an_error(self):
        for code in (2, 127, -9):
            assert diff_outcome(code) == DIFF_ERROR


class TestSanitize:
    def test_drops_server_owned_fields_and_the_status_block(self):
        assert sanitize(RAW_DEPLOYMENT) == CLEAN_DEPLOYMENT

    def test_same_named_spec_field_survives(self):
        """`spec.generation` is the user's, not the server's. An unanchored
        metadata removal would eat it."""
        assert "  generation: keep-me\n" in sanitize(RAW_DEPLOYMENT)

    def test_lists_and_nested_maps_are_left_alone(self):
        cleaned = sanitize(RAW_DEPLOYMENT)
        assert "      app: web\n" in cleaned
        assert "            - --listen=:8080\n" in cleaned
        assert "            - containerPort: 80\n" in cleaned
        assert "  annotations:\n" in cleaned

    def test_status_removal_does_not_depend_on_it_being_last(self):
        text = (
            "apiVersion: v1\n"
            "status:\n"
            "  phase: Running\n"
            "kind: Pod\n"
            "metadata:\n"
            "  name: one\n"
        )
        cleaned = sanitize(text)
        assert "status:" not in cleaned
        assert "phase:" not in cleaned
        assert "kind: Pod\n" in cleaned
        assert "  name: one\n" in cleaned

    def test_metadata_removal_is_scoped_to_the_metadata_block(self):
        text = (
            "apiVersion: v1\n"
            "kind: Pod\n"
            "metadata:\n"
            "  name: one\n"
            "  generation: 3\n"
            "spec:\n"
            "  generation: 9\n"
        )
        cleaned = sanitize(text)
        assert "  name: one\n" in cleaned
        assert "  generation: 9\n" in cleaned
        assert "  generation: 3\n" not in cleaned
