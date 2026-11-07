# Reference

This page covers the spec files, the platform files and platform software, the review rules, what berth renders, who may do what, the drift report, and the CLI. It is written from `src/berth/model.py`, `src/berth/review.py`, `src/berth/render/`, `src/berth/drift.py` and `berth --help`. The JSON Schemas in `platform/schema/` are generated from the same models, and are what editors use. Unknown fields are errors in every file.

## app.yaml

`apps/<app>/app.yaml` describes the app in every environment.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Must equal the directory name. Lower-case letters, digits and `-`, at most 40 characters, starting with a letter. |
| `description` | string | required | 1–200 characters. Shown on Control-M folders. |
| `owners` | list of identities | required | At least two, no duplicates. Lower-case Azure DevOps or Entra sign-in names (`name@domain`). Groups are not accepted. Owners also get read access in Kubernetes; see [Access](#access). |
| `source` | URL | required | `https://` URL of the repository that builds the image. Shown on the namespace. |
| `image` | string | `name` | Repository in the platform registry. The full image is `<registry>/<image>:<version>`. |
| `config` | map of string to string | `{}` | Environment variables for every container. Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings, so quote numbers and booleans. |
| `files` | map of file name to string | `{}` | Files mounted read-only at `/config/<name>`. |
| `secrets` | list of names | `[]` | Key Vault secrets read from `<app>--<name>` and exposed as the environment variable `NAME` (upper case, `-` becomes `_`). |
| `talks_to` | list of `app/service` | `[]` | Services this app calls. Each one injects `<APP>_<SERVICE>_HOST`, `_PORT` and `_URL`, and admits this app through the target's network policy. The target must run in every environment this app runs in, and must have a `port`. An app may not list itself. |
| `calls` | list of `host` or `host:port` | `[]` | Systems outside the cluster the app depends on, such as a database. Recorded on the namespace as `berth.contoso.io/app-calls`; no effect on traffic. |
| `azure_identity` | bool | `false` | Gives pods a workload identity. Needs an entry for `<app>/<env>` in `platform.yaml` `identities` for each environment. |
| `services` | map of name to [service](#services) | `{}` | Long-running processes. Names are 1–20 characters. |
| `workflows` | map of name to [workflow](#workflows) | `{}` | Scheduled or triggered jobs. Names must not repeat a service name. |
| `overrides` | list of [overrides](#overrides) | `[]` | Patches to rendered objects, applied in every environment. |

An app must have at least one service or workflow.

### Services

| Field | Type | Default | Description |
|---|---|---|---|
| `command` | list of strings | image entrypoint | Replaces the image's entrypoint. |
| `args` | list of strings | image command | Replaces the image's arguments. |
| `port` | int | none | 1024–65535. Without a port the service is a worker: no Kubernetes Service, no probes. |
| `health` | `{path}` | TCP check on `port` | HTTP path for readiness and liveness probes. Needs `port`. |
| `size` | size name | `s` | One of `platform.yaml` `sizes`. |
| `replicas` | int | the environment's `default_replicas` | Fixed pod count. Not with `autoscale`. |
| `autoscale` | `{min, max, cpu_percent}` | none | Horizontal autoscaling on CPU. `min` ≤ `max`; `cpu_percent` is 10–95, default 70. |
| `expose` | `internal` or `company` | `internal` | `company` adds an HTTPRoute on the cluster's Gateway, so callers on the company network reach the service by hostname. Needs `port` and `hostname`. |
| `hostname` | DNS label | none | With `expose: company` only. The environment's `domain` is appended, giving `<hostname>.<domain>`. Unique per environment. |
| `volumes` | map of name to `{path, size_gi}` | `{}` | Persistent disks (ReadWriteOnce, 1–1024 GiB). A service with volumes runs one replica, rolls out with `Recreate`, and defaults to 1 replica. Never pruned. |
| `writable` | list of absolute paths | `[]` | Directories the container may write to, as empty directories, for images that expect to write outside `/tmp`. Tasks accept it too. |

### Workflows

| Field | Type | Default | Description |
|---|---|---|---|
| `orchestrator` | `airflow`, `controlm` or `kubernetes` | required | What schedules the workflow. `kubernetes` renders a CronJob and needs exactly one task and a `schedule`. |
| `schedule` | five-field cron | none | Without one, the workflow runs only when triggered. For `controlm`, it must be one fixed time a day on listed weekdays or month days, with no month restriction and no steps. |
| `timezone` | IANA zone | `UTC` | For `controlm`, must equal the Control-M server's zone in `platform.yaml`. |
| `tasks` | map of name to task | required | At least one. Dependencies must name sibling tasks and must not form a cycle. |

| Task field | Type | Default | Description |
|---|---|---|---|
| `command`, `args` | list of strings | image defaults | As for services. |
| `size` | size name | `s` | As for services. |
| `timeout` | duration (`90s`, `30m`, `2h`) | `1h` | Airflow `execution_timeout`; Kubernetes Job `activeDeadlineSeconds` for `controlm` and `kubernetes`. |
| `retries` | int, 0–5 | `0` | Airflow task retries; Job `backoffLimit` for `controlm` and `kubernetes`. |
| `depends_on` | list of task names | `[]` | Tasks that must finish first. |

### Overrides

| Field | Type | Description |
|---|---|---|
| `target` | `{kind, name}` | A rendered object in the app's namespace, named as in `rendered/`. Rendering fails if no object matches. |
| `patch` | mapping | An RFC 7386 JSON merge patch: mappings merge, `null` deletes, lists and scalars replace. |
| `reason` | string | At least 10 characters. Shown to the platform reviewer. |

## Environment files

`apps/<app>/<env>.yaml` deploys the app to `env`, which must be a key of `platform.yaml` `environments`. Without the file, the app does not run in that environment.

| Field | Type | Description |
|---|---|---|
| `version` | `tag@sha256:<64 hex>` | Required. The image to run. |
| `config` | map | Merged over `app.yaml` `config`, key by key. |
| `services.<name>` | `{size, replicas, autoscale}` | Replaces those fields for one service. Setting `replicas` clears `autoscale`, and the reverse. |
| `workflows.<name>` | `{schedule, enabled}` | Replaces the schedule; `enabled: false` leaves the workflow out of this environment. |
| `overrides` | list | Applied after the overrides in `app.yaml`. |

## platform.yaml

`platform/platform.yaml` holds the platform team's settings. Every change to it is platform-tier.

| Field | Description |
|---|---|
| `registry` | Registry host for every image. |
| `tenant_id` | Entra tenant of the Key Vaults. |
| `repo_url` | This repository's URL, for links to commits. |
| `release_bot` | Identity whose pull requests may change versions only. |
| `kubectl_writers` | Entra group object IDs whose members get `edit` in every app namespace, for break-glass. |
| `platform_approvers` | Individuals, any one of whom satisfies a platform-tier review. |
| `max_replicas_without_review` | `replicas` or `autoscale.max` above this needs platform review. |
| `storage_class` | Storage class for `volumes`. |
| `clusters.<cluster>` | `key_vault` (the platform vault, for the Gateway's certificates), `gateway_ip` (the reserved private address of the cluster's Gateway, where each environment's wildcard DNS record points) and `networks` (pod and service ranges, never sent to the proxy). Every environment's `cluster` must be listed. |
| `gateway` | `namespace`, where the Gateway lives and its proxies run, and `controller`, the GatewayClass controller name. |
| `links.<name>` | An `https://` URL template shown by `berth explain`. Placeholders: `{app}`, `{env}`, `{namespace}`, `{service}` (`<app>-<component>`) and `{cluster}`. |
| `environments.<env>` | `cluster`, `key_vault`, `production` (default `false`), `promotes_from`, `default_replicas`, `domain` (company hostnames are `<hostname>.<domain>`), `proxy` (`url`, and `no_proxy` entries added to berth's own), `otlp_endpoint` (optional; may use `$(HOST_IP)`), `airflow` (`namespace`, `service_account`) and `controlm` (`server`, `connection_profile`, `timezone`, `agent_namespace`, `agent_service_account`). |
| `sizes.<name>` | `cpu`, `memory`, and `platform_review` (default `false`). The memory limit equals `memory`; there is no CPU limit. |
| `identities` | `<app>/<env>` to workload identity client ID. |
| `freezes` | `start`, `end` (time zone required) and `reason`. Production changes wait while a freeze is active. |

## Platform software

`platform/software/<name>.yaml` installs one Helm chart: cluster software with CRDs, controllers or webhooks. Every change is platform-tier. `berth deploy` renders each chart with `helm template` and applies it with kubectl; Helm never installs anything, and chart output is not committed.

| Field | Type | Default | Description |
|---|---|---|---|
| `description` | string | required | 1–200 characters. |
| `repo` | URL | required | `https://` chart repository, or `oci://` registry path. |
| `chart` | string | required | Chart name. |
| `version` | exact version | required | `1.2.3` or `v1.2.3`. Ranges are refused. |
| `order` | int, 0–99 | `0` | Lower deploys first, for software whose CRDs others need. |
| `values` | mapping | `{}` | Helm values for every install. |
| `installs.<release>` | `{cluster, namespace, version, values, secrets}` | required | One install per entry; the key is the Helm release name, unique per cluster. The namespace must not be an app namespace. `version`, if set, overrides the shared version for this install, to upgrade one cluster first. `values` is merged over the shared values as an RFC 7386 merge patch. |
| `installs.<release>.secrets.<name>` | `{type, data}` | `{}` | A Kubernetes Secret in the install's namespace, filled by External Secrets Operator from the cluster's platform vault. `type` is `Opaque` (default) or `kubernetes.io/tls`; `data` maps Secret keys to vault secret names. |

String values may contain these placeholders, filled per install: `${cluster}`, `${tenant_id}`, `${registry}`, `${key_vault}` and `${gateway_namespace}`. Any other `${…}` is an error.

`berth check` also requires that, on every cluster with platform software, an install in the gateway namespace declares the certificate for each environment's listener: `berth-<env>-tls`.

## Review rules

`platform/review.yaml` lists rules. Each rule has a `reason`, `paths`, and a `tier`, which is either one tier or a map with a tier for every environment. A change's path is a dotted path into the app's merged spec for one environment, for example `services.api.size` or `config.LOG_LEVEL`. `*` matches one segment and `**` any number. The first matching rule decides. Synthetic paths cover changes that have no field:

| Path | Meaning |
|---|---|
| `app` | the app was added or removed |
| `environment` | an environment file was added or removed |
| `repo_file.<path>` | another file in the app's directory changed |
| `platform_file.<path>` | a file outside `apps/` and `rendered/` changed |

### Tiers

| Tier | Satisfied when |
|---|---|
| `auto` | An owner of the app authored the change, or the release bot did. Otherwise it needs `owner` approval. |
| `owner` | An owner other than the author approves. |
| `platform` | An owner other than the author approves, and a platform approver other than the author approves. For `platform_file` changes, only the platform approver. |

Adjustments made after the table:

- A path no rule matches is `platform`.
- An environment missing from the target branch's `platform.yaml` is `platform`.
- These raise a change to `platform`: a new `size` with `platform_review: true`; `replicas` or `autoscale.max` above `max_replicas_without_review`; a `version` not found in the history of the `promotes_from` environment's file on the target branch.
- A new app also counts as starting in each of its environments (`environment` added), so its first prod deploy needs the platform team.
- A change in an environment with `production: true` is at least `owner`.
- An app's tier is the highest tier among its changes. Owners are read from the target branch, or from the change itself for a new app.

### Gate decision

The gate's state is the `berth/review` status.

| State | When |
|---|---|
| `failed` | `berth check` fails on the pull request's tree; or an owner of an affected app, or a platform approver, voted reject (−10); or the release bot changed anything but a version. |
| `pending` | A requirement is unmet, or a production change falls in a freeze. With the `break-glass` label: until one owner or platform approver other than the author approves. |
| `succeeded` | Every requirement is met. With `break-glass`: one owner or platform approver other than the author approved, whatever the tiers. |

Votes of 10 (approved) and 5 (approved with suggestions) count as approval. Group votes are ignored. Policy, approvers, owners and the release bot are all read from the target branch.

## Rendered output

`berth render` writes these files, and `berth check` fails if any differ:

| Path | Contents |
|---|---|
| `rendered/k8s/<cluster>/<app>-<env>/manifests.yaml` | Namespace, ServiceAccount, the `berth-owners` and `berth-writers` RoleBindings, ConfigMaps, ExternalSecret, and per service a Deployment, Service, HPA, PodDisruptionBudget, HTTPRoute and PersistentVolumeClaims as applicable; CronJobs; scheduler RoleBindings; NetworkPolicy. |
| `rendered/k8s/<cluster>/_platform/namespaces.yaml` | Namespaces for platform software. Not labelled as berth's, so app policies and app drift checks leave them alone. |
| `rendered/k8s/<cluster>/_platform/platform.yaml` | ClusterRoles for Airflow and Control-M, a ClusterSecretStore per environment and one for the platform vault, the GatewayClass, the cluster's Gateway with a listener per environment, the HTTP-to-HTTPS redirect, and ExternalSecrets for platform software. |
| `rendered/k8s/<cluster>/_platform/admission.yaml` | The ValidatingAdmissionPolicies and their bindings; see [Admission policies](#admission-policies). |
| `rendered/airflow/<env>/<app>__<workflow>.py` | One DAG per Airflow workflow; DAG ID `<app>__<workflow>`. |
| `rendered/controlm/<env>/<app>.json` | Automation API folders `<app>-<workflow>`, one `Job:Kubernetes` per task. |

What every app environment gets, whatever its spec says:

- A namespace `<app>-<env>`, with the `restricted` pod security standard, the owners, source and `calls` as annotations, and `berth.contoso.io/keep: "true"`, so prune never deletes it. Persistent volume claims carry the same annotation.
- Pods that run as UID 10001, non-root, with a read-only root filesystem, no privilege escalation, all capabilities dropped, the `RuntimeDefault` seccomp profile, and an emptyDir at `/tmp`.
- Images pinned by digest.
- `BERTH_APP`, `BERTH_ENV` and `BERTH_VERSION` in every container.
- When the environment sets `proxy`: `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, and their lower-case forms. `NO_PROXY` always holds `localhost`, `127.0.0.1`, `.svc`, `.cluster.local`, the cluster's `networks` and `.<domain>`, then the environment's own `no_proxy` entries.
- Telemetry that agrees with the spec. Every container gets `OTEL_SERVICE_NAME` (`<app>-<component>`) and `OTEL_RESOURCE_ATTRIBUTES` (`deployment.environment.name`, `service.version`, `service.namespace`). When the environment sets `otlp_endpoint`, it also gets `OTEL_EXPORTER_OTLP_ENDPOINT`, plus `HOST_IP` from the node when the endpoint uses `$(HOST_IP)`. Pods carry `tags.datadoghq.com/env`, `service` and `version`; Deployments and CronJobs carry `env` and `service`. The component is the service name, or `<workflow>-<task>` for a task.
- Config maps named by content hash, so a change restarts pods.
- Rolling updates with `maxUnavailable: 0`.
- A NetworkPolicy that admits traffic only from the same namespace, from apps that list it in `talks_to`, and from the gateway namespace for services exposed to the company.

In production, services also spread across zones, and get a disruption budget when they have two or more pods.

## Access

Rendered from `owners` and `kubectl_writers` into each app namespace.

| Who | Kubernetes |
|---|---|
| An app's owners | `view` on the app's namespaces (`berth-owners`): everything except Secrets, including pod logs. |
| Members of the `kubectl_writers` groups | `edit` on every app namespace (`berth-writers`), for break-glass. Nothing outside app namespaces. |
| Airflow's worker service account | Create pods in namespaces of apps with Airflow workflows (`berth-airflow`). |
| The Control-M agent's service account | Create jobs in namespaces of apps with Control-M workflows (`berth-controlm`). |

## Admission policies

ValidatingAdmissionPolicies with `failurePolicy: Fail` and `validationActions: [Deny]`, bound to namespaces labelled `app.kubernetes.io/managed-by: berth`.

| Policy | Refuses |
|---|---|
| `berth-images-pods`, `-templates`, `-cronjobs` | a container or init container whose image is not `<registry>/…@sha256:…` |
| `berth-resources-pods`, `-templates`, `-cronjobs` | a container without CPU and memory requests and a memory limit |
| `berth-external-secrets` | `dataFrom`; a store other than `berth-<env>` for the namespace's environment; a key outside `<app>--` |
| `berth-routes` | a hostname outside the namespace's environment `domain`; a parent other than the `berth` Gateway |
| `berth-owners` | an app namespace whose owners annotation names fewer than two people |

The `-templates` policies cover Deployments, StatefulSets, DaemonSets, ReplicaSets and Jobs.

## Deploying and drift

`berth deploy` applies with `kubectl apply --server-side --field-manager=berth`, in this order: `_platform/namespaces.yaml`, platform software by `order`, `_platform/platform.yaml`, `_platform/admission.yaml`, then each selected app namespace. After applying a namespace it:

- reports fields set by another field manager, other than `berth`, `berth-deploy`, `berth-break-glass` and `kube-controller-manager`, ignoring status, `kubectl.kubernetes.io/restartedAt`, and fields berth also sets (a change adopted into git); with `--revert`, removes them with a strategic merge patch (built-in kinds only);
- lists objects labelled as berth's that git no longer renders; with `--prune`, deletes them, except Namespaces, PersistentVolumeClaims and anything annotated `berth.contoso.io/keep: "true"`;
- waits up to 600 seconds for each Deployment's rollout, unless `--no-wait`;
- annotates the namespace with `berth.contoso.io/deployed-commit` and `berth.contoso.io/deployed-at`, as field manager `berth-deploy`.

A namespace annotated `berth.contoso.io/break-glass` with an expiry in the future is skipped, unless `--include-break-glass`. An apply that conflicts with another field manager fails that namespace only, and names the field.

`berth drift` reports, per app namespace on each cluster:

| Line | Meaning |
|---|---|
| `differs from git: Kind/name` | `kubectl diff --server-side` shows a change |
| `changed by hand: Kind/name: manager: path` | a field set by a field manager other than berth or Kubernetes' controllers |
| `made by hand: Kind/name` | an object without berth's label, not owned by a controller, and not one Kubernetes creates (`default` ServiceAccount, `kube-root-ca.crt`) |
| `Kind/name (removed from git)` | an object berth made that git no longer renders |
| `on the cluster but no longer in git` | a berth namespace with no environment file |
| `break-glass by … until …` / `EXPIRED` | the namespace's break-glass mark |
| `could not check: …` | kubectl failed for that namespace |

## CLI

```text
berth [--root ROOT] COMMAND
```

`--root` is the repository root; the default is the current directory.

| Command | Arguments | Behaviour |
|---|---|---|
| `check` | | Validates every spec and cross-reference; fails if `rendered/` or `platform/schema/` is stale. Prints `ok: N apps, M app environments` on success. |
| `render` | `[--check]` | Writes `rendered/` and deletes files that are no longer produced. `--check` only lists stale files. |
| `review` | `--author ID [--base REF] [--approve ID]… [--reject ID]… [--label NAME]… [--markdown PATH]` | Compares the working tree with `REF` (default `main`) and prints each change, its tier and reason, and the gate's decision for the simulated votes and labels. |
| `gate` | `--pr N --base REF [--target-url URL] [--summary PATH]` | Reads pull request `N` from Azure DevOps, decides, and posts `berth/review`. Needs `SYSTEM_ACCESSTOKEN`, `SYSTEM_COLLECTIONURI`, `SYSTEM_TEAMPROJECT` and `BUILD_REPOSITORY_ID`. |
| `release` | `APP --env ENV --version TAG@DIGEST` | Sets `version` in `apps/APP/ENV.yaml`, keeping comments. The file must exist. |
| `promote` | `APP --to ENV` | Copies `version` from the environment `ENV` promotes from. |
| `new` | `NAME --description TEXT --owner ID --owner ID --source URL --version TAG@DIGEST [--env ENV]` | Creates `apps/NAME/app.yaml` with one service and `apps/NAME/ENV.yaml` (default `dev`). Fails if the directory exists. |
| `schema` | `[--check]` | Writes `app.schema.json`, `env.schema.json`, `platform.schema.json` and `software.schema.json` in `platform/schema/`. |
| `explain` | `APP --env ENV` | Prints the app's owners, namespace and image in `ENV`; how to look at it and its company URLs; every `links` entry; each merged setting with the file that set it (or `default`); the Kubernetes objects rendered; and the scheduler for each workflow. Exits 1 if the app does not run in `ENV`. |
| `images` | `[--base REF]` | Prints `registry/repository@digest<TAB>tag` for every image the specs deploy, or only those not deployed at `REF`. |
| `deploy` | `--cluster CLUSTER [--app APP [--env ENV]] [--changed-from REF] [--prune] [--revert] [--no-wait] [--include-break-glass] [--no-software] [--context CTX] [--kubeconfig PATH]` | Applies `rendered/k8s/CLUSTER/`; see [Deploying and drift](#deploying-and-drift). Refuses to run unless `berth check` passes. With `--app`, applies only that app (and not the platform); with `--changed-from`, only app namespaces whose manifests changed, plus the platform if `_platform/` or `platform/software/` changed. Exits 1 if any namespace failed. |
| `drift` | `--cluster CLUSTER … [--slack] [--details PATH] [--context CTX] [--kubeconfig PATH]` | Reports each cluster's drift. `--slack` posts the summary to `SLACK_WEBHOOK_URL`; `--details` writes the full diffs. |
| `breakglass` | `APP --env ENV (--reason TEXT [--hours N] \| --end) [--context CTX] [--kubeconfig PATH]` | Marks the app's namespace with who (from `kubectl auth whoami`), why and until when (default 8 hours), or removes the mark. |

kubectl's context defaults to the cluster's name. `BERTH_KUBECTL` and `BERTH_HELM` choose the binaries (default: from `PATH`).

Every command exits 0 on success and 1 on a problem. `review` and `gate` exit 0 whatever the decision; the decision is in their output and, for `gate`, in the posted status. `drift` exits 0 whatever it finds.
