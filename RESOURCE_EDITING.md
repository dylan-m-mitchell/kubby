# Resource editing — the phases after Phase 0

This is the implementation plan for letting kubby **edit and add Kubernetes
resources from inside the TUI**. It is written to be picked up cold by an
agent (or a person) who has not done the investigation that produced it.

Read `AGENTS.md` first — it holds the project rules. This file holds the
feature plan and the facts that were expensive to establish, so they do not
have to be established twice.

## Where this stands

**Phase 0 is done** (`feat/tui: name the object behind each box, and give the
picture a cursor`). It is read-only and ships on its own:

* `kubby.cluster.ResourceRef` — the identity of one Kubernetes object.
* `kubby.tui.diagram.Node.resource` — every box knows which object it is.
* `kubby.tui.place.nearest_box()` — pure cursor geometry over a layout.
* `kubby.tui.paint.SELECTED_STYLE` — how the cursor is drawn.
* `kubby.tui.panels.GraphPanel` — `hjkl`/arrows move the cursor; the subtitle
  names the object under it.

Phases 1–3 are **not started**. Phase 1 is where kubby first mutates a
cluster, so it is the one that needs care.

## Scope, already decided

These were settled deliberately. Do not re-open them without asking.

| Question | Decision |
|---|---|
| Editing surface | **Raw YAML in a `TextArea` first**, guided forms for a few kinds later |
| Verbs | Edit, create, delete, scale, rollout-restart |
| Kinds | What the graph already draws (Deployment/StatefulSet/DaemonSet/Service/Ingress/Pod/Node) **plus** ConfigMap, Secret, PersistentVolumeClaim, Namespace |
| Safety | **Server dry-run + diff + confirm** on every save |

## Facts established against a real cluster

Verified with kubectl against a live minikube (v1.35.1). Re-derive only if
something looks wrong — these were checked, not assumed.

**Fetching one object**

* `kubectl get <kind> <name> -n <ns> -o yaml` returns a **bare object**.
* `kubectl get -A -o yaml` wraps everything in a `kind: List` with an `items`
  array. **The editor must not use the `-A` form** — the graph's own fetch
  does, and copying it here would hand the editor a List to apply.
* Top-level keys come out alphabetically sorted: `apiVersion`, `kind`,
  `metadata`, `spec`, `status`. Exactly one `status:`, always last.
* `metadata` carries `annotations`, `creationTimestamp`, `generation`,
  `labels`, `name`, `namespace`, `resourceVersion`, `uid`.
* **`managedFields` is already absent** — modern kubectl hides it by default.
  Pass `--show-managed-fields=false` explicitly anyway so an older kubectl
  cannot put it back.
* Indent is 2 spaces and list items use `- `; the YAML is emitted from JSON,
  so it is machine-generated and stable. That is what makes line-based
  surgery safe here and nowhere else.

**Diffing**

* `kubectl diff -f -` does a **server-side dry run and a unified diff in one
  call**. It is not two round trips.
* **Exit codes: `0` = no differences, `1` = differences found, `>1` = error.**
  Treating `1` as a failure breaks the happy path — this is the single most
  likely bug in Phase 1.
* Output is always YAML, but the diff text comes from the *external* `diff`
  binary and honours `KUBECTL_EXTERNAL_DIFF`. **Never parse it**; render it
  as text.
* It works for an object that does not exist yet, reporting the whole thing
  as added. So one code path serves both edit and create.

**Flags that exist**

* `kubectl apply --dry-run=server` — validates without persisting.
* `--show-managed-fields=false` on `get`.

## Architecture

New module **`kubby/resources.py`**, following the pattern
`kubby/installer/minikube.py` already documents: *pure argv builders, no I/O,
no shelling out, no disk reads*. Everything here is directly unit-testable.

```python
# kubby/resources.py — shapes, not final signatures
get_yaml_args(ref) -> list[str]        # single object; no -A
diff_args() -> list[str]               # kubectl diff -f -
apply_args() -> list[str]              # kubectl apply -f -
delete_args(ref) -> list[str]
scale_args(ref, replicas) -> list[str]
rollout_restart_args(ref) -> list[str]
sanitize(yaml_text) -> str             # the only string surgery
```

`Service` gains the I/O that runs those:

```python
get_resource_yaml(ref) -> {"ok": bool, "yaml": str, "error": str | None}
```

and the mutating verbs, each returning the existing completion shape.

### `sanitize` — the riskiest pure function

Removes what the server owns so the editor shows what is editable and so
`apply` is not fighting a stale `resourceVersion`:

* the whole top-level `status:` block (last key, so drop to the next
  `^[A-Za-z]` line or EOF);
* inside the `metadata:` block only, the 2-space-indented `creationTimestamp:`,
  `generation:`, `resourceVersion:` and `uid:`.

Scope the metadata removals to the metadata block — an unanchored
`^  generation:` would eventually eat a field inside `spec`. No YAML parser:
`pyproject.toml` has exactly one runtime dependency and this is not worth
being the second. Fixture-test it against a captured real `kubectl get`
payload, including one with a list-valued field and one with nested maps.

## Phase 1 — edit an existing resource

1. **Editor modal** in `kubby/tui/popups.py` (that is where every modal lives).
   A `ModalScreen` whose body is a `TextArea`, with the header showing
   `kind/name`, the namespace, **and the active context**.
   `ModalScreen` suspends the app's binding chain, so the editor must bind
   its own quit/close keys — that is the documented pattern in that file.
2. **Open it** from the graph cursor: `GraphPanel` sends a `PanelAction`,
   `KubbyApp.on_panel_action` dispatches it. Panels *ask*, the app *does* —
   never fetch from the panel.
3. **Fetch in a worker**: `@work(thread=True)` plus `call_from_thread`, the
   same shape as `_load_settings` / `_show_settings`.
4. **Save flow**: `kubectl diff -f -` → branch on the exit code → show the
   diff in a confirm step → `kubectl apply -f -` on confirm.
5. **On success**, `refresh_data()` so the picture reflects reality.

Pass the YAML on **stdin**, never in argv. The service echoes commands into
the log panel (`$ {' '.join(argv)}`), so argv is a place a Secret's contents
must never appear.

## Phase 2 — create

* `n` opens a kind picker (the decided list) → a hand-written template per
  kind → the **same** diff/apply path. Templates are Python constants, in the
  spirit of `ADDON_OPTIONS` in `popups.py`.
* Namespace picker fed from the live namespace list, not a hardcoded default.
* Validate the obvious things locally (a kind, a name, a namespace) so the
  user is not charged a round trip to be told the name is missing.

## Phase 3 — delete, scale, restart

Narrow and safe compared to a full edit, and cheap once Phase 1 exists.

* **Delete** — reuse `popups.ConfirmModal`. Pass `--wait=false`: a finalizer
  can block `kubectl delete` indefinitely, and the worker timeout would leave
  the UI showing a job that is not going to finish.
* **Scale** — `kubectl scale <kind>/<name> --replicas=N`. Only meaningful for
  Deployment/StatefulSet/ReplicaSet/ReplicationController; hide it elsewhere.
* **Restart** — `kubectl rollout restart <kind>/<name>`. Deployment,
  DaemonSet, StatefulSet only.

## Cross-cutting

### The single job slot

`knowledge.md` and `KubbyService` both pin **one global job slot**: minikube
start/stop/delete share it, and a mutation must join it rather than run
beside it. `_kick_job(action)` currently maps a verb to minikube argv
internally, so it needs to accept a prebuilt argv plus a label.
`_run_minikube_job` also hardcodes two things worth generalising: the
`✓ minikube {action} succeeded` wording and the 15-minute timeout. A
`kubectl apply` should not inherit a 15-minute budget meant for
`minikube start`.

### Captured output, not streamed

Minikube jobs stream every line into the RichLog through `on_log`. **The
editor must not use that path.** A diff of a Secret contains its values, and
the log panel keeps `history` for the whole session. Editor operations should
capture output and return it to the modal (a request/response call), while
still taking the job lock.

### Secrets

`kubectl get secret -o yaml` returns base64 under `data`; `stringData` is
write-only and the server folds it into `data`. For v1 show the base64 as-is
and say so in the editor; if you decode for display you must re-encode on
save, and getting that wrong silently corrupts the Secret. Whatever you
choose, a Secret's content must never reach the log panel or `history`.

## Open decisions — need a human

1. **Context guard (blocks Phase 1).** kubby targets minikube, but `kubectl`
   acts on whatever the *current context* is. If a kubeconfig points at
   production, an `e` key is a footgun. Neither a minikube context name nor a
   loopback API server host is sufficient to authorize mutations: names can
   be reused, and a local proxy can reach a remote cluster. Require a stronger
   check that the actual target cluster is the intended local minikube
   cluster, or explicit user approval for that target. Otherwise keep
   mutations disabled with a visible reason. This must be settled before
   any mutation lands.
2. **Helm-managed objects.** `kubectl apply` reassigns the field manager, so
   editing a Helm-managed resource can make a later `helm upgrade` conflict or
   silently revert it. Detect `app.kubernetes.io/managed-by: Helm` (and
   `meta.helm.sh/release-name`) and warn in the editor. This is not
   hypothetical — it was observed on a live cluster.
3. **`ResourceRef.name` is a display name, not a verified one.** Some boxes
   are labelled with a hash-stripped stem. Verify with a `get` before acting
   rather than trusting it; a failed fetch is the honest outcome.

## House rules that will bite

From `AGENTS.md`, plus what came up during Phase 0:

* **Textual *replaces* `BINDINGS` along the MRO.** A panel must list its
  bindings explicitly and must copy a shared list (`list(LIST_NAV_BINDINGS) +
  [...]`), never alias it.
* **The keybar has roughly 27 characters of headroom.** New graph keys should
  be `show=False` and documented in the help overlay via
  `KubbyApp.help_sections()`. Adding five visible keys overflows the bar.
* **Focus with `widget.focus()`**, not `screen.focus = widget`.
* **Poll for worker results** (`tests/helpers.wait_until`); asserting
  immediately races the worker.
* **Tests do not touch module privates.** No test in this suite reaches for a
  leading-underscore name on a `kubby` object; test through the public
  surface. (Phase 0's tests had to be rewritten to honour this.)
* `asyncio_mode = "auto"` — async tests need no decorator.
* Blocking work goes in a Textual worker and returns to the UI thread.
* `knowledge.md` is stale (it describes the pre-Textual GUI) and is
  gitignored. Trust `AGENTS.md` and the code.

## Testing

* Pure: `resources.py` argv builders; `sanitize` against captured real YAML.
* The exit-code trap: a test that `kubectl diff` returning **1** proceeds to
  confirmation rather than being reported as an error.
* Pilot: editor opens with the fetched YAML; cancel writes nothing; confirm
  applies; a failed diff keeps the editor open and surfaces stderr.
* `FakeService` needs the new methods, and every panel/modal test must keep
  passing without a host, a network, or a cluster.
* Structure a test so the "did anything mutate?" question is answerable: the
  fake should record the payload it was asked to apply.

## Verification environment

`kubectl` is installed and may have a live minikube. Check with:

```bash
kubectl config current-context && kubectl get deploy -A
```

A real cluster is worth using for the `sanitize` fixture and for confirming
the `kubectl diff` exit-code behaviour, which is not reproducible with a
fake. Do not test mutations against anything but a local minikube, and never
against a context you did not check first.
