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
| `config` | map of string to string | `{}` | Environment variables for every container. Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings, so quote numbers and booleans. A key may not be the name of a variable berth sets itself (a secret, a `talks_to` variable, the proxy, telemetry, `BERTH_*`): that one would win. |
| `files` | map of file name to string | `{}` | Files mounted read-only at `/config/<name>`. |
| `secrets` | list of names | `[]` | Key Vault secrets read from `<app>--<name>` and exposed as the environment variable `NAME` (upper case, `-` becomes `_`). |
| `talks_to` | list of `app/service` | `[]` | Services this app calls. Each one injects `<APP>_<SERVICE>_HOST`, `_PORT` and `_URL`, and admits this app through the target's network policy, so a new entry also needs the target's owners (see [Review rules](#review-rules)). The target must run in every environment this app runs in, and must have a `port`. An app may not list itself. |
| `calls` | list of `host` or `host:port` | `[]` | Systems outside the cluster the app depends on, such as a database. Recorded on the namespace as `berth.contoso.io/app-calls`; no effect on traffic. |
| `azure_identity` | bool | `false` | Gives pods a workload identity. Needs an entry for `<app>/<env>` in `platform.yaml` `identities` for each environment. |
| `services` | map of name to [service](#services) | `{}` | Long-running processes. Names are 1–20 characters. |
| `workflows` | map of name to [workflow](#workflows) | `{}` | Scheduled or triggered jobs. Names must not repeat a service name. `<app>-<workflow>-<task>` is a Kubernetes label, so it may have at most 63 characters, and `<app>-<workflow>` at most 52 for a `kubernetes` workflow, whose CronJob it names. |
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
| `target` | `{kind, name}` | A rendered object in the app's namespace, named as `berth render` or `berth explain` shows it. Rendering fails if no object matches. |
| `patch` | mapping | An RFC 7386 JSON merge patch: mappings merge, `null` deletes, lists and scalars replace. |
| `reason` | string | At least 10 characters. Shown to the platform reviewer. |

## Environment files

`apps/<app>/<env>.yaml` deploys the app to `env`, which must be a key of `platform.yaml` `environments`. Without the file, the app does not run in that environment.

| Field | Type | Description |
|---|---|---|
| `version` | `tag@sha256:<64 hex>` | Required. The image to run. |
| `config` | map | Merged over `app.yaml` `config`, key by key. |
| `services.<name>` | `{size, replicas, autoscale}` | Replaces those fields for one service. Setting `replicas` clears `autoscale`, and the reverse. |
| `workflows.<name>` | `{schedule, enabled, tasks}` | Replaces the schedule; `enabled: false` leaves the workflow out of this environment; `tasks.<task>.size` replaces a task's size. |
| `overrides` | list | Applied after the overrides in `app.yaml`. |

## platform.yaml

`platform/platform.yaml` holds the platform team's settings. Every change to it is platform-tier.

| Field | Description |
|---|---|
| `registry` | Registry host for every image. |
| `tenant_id` | Entra tenant of the Key Vaults. |
| `repo_url` | This repository's URL, for links to commits. |
| `release_bot` | Identity whose pull requests may change versions only. The mirror bot opens its pull requests as this identity too. |
| `rightsizing_bot` | Identity whose pull requests may change sizes only (`services.*.size`, `workflows.*.tasks.*.size`). Optional. |
| `rightsizing` | How the rightsizing bot reads usage; see [Rightsizing](#rightsizing). Every field has a default. |
| `platform_approvers` | Individuals, any one of whom satisfies a platform-tier review. |
| `max_replicas_without_review` | `replicas` or `autoscale.max` above this needs platform review. |
| `storage_class` | Storage class for `volumes`. |
| `clusters.<cluster>` | `key_vault` (the platform vault, for the Gateway's certificates), `gateway_ip` (the reserved private address of the cluster's Gateway, where each environment's wildcard DNS record points) and `networks` (pod and service ranges, never sent to the proxy). Every environment's `cluster` must be listed. |
| `gateway` | `namespace`, where the Gateway lives and its proxies run, and `controller`, the GatewayClass controller name. |
| `links.<name>` | An `https://` URL template shown by `berth explain`. Placeholders: `{app}`, `{env}`, `{namespace}`, `{service}` (`<app>-<component>`) and `{cluster}`. |
| `environments.<env>` | `cluster`, `key_vault`, `production` (default `false`), `promotes_from`, `default_replicas`, `domain` (company hostnames are `<hostname>.<domain>`), `proxy` (`url`, and `no_proxy` entries added to berth's own), `otlp_endpoint` (optional; may use `$(HOST_IP)`), `airflow` (`namespace`, `service_account`), `controlm` (`server`, `connection_profile`, `timezone`, `agent_namespace`, `agent_service_account`) and `break_glass_groups` (Entra group object IDs whose members get the `berth-break-glass` role in this environment's app namespaces; see [Access](#access)). Environments must not promote from each other in a cycle. |
| `sizes.<name>` | `cpu`, `memory`, and `platform_review` (default `false`). The memory limit equals `memory`; there is no CPU limit. |
| `identities` | `<app>/<env>` to workload identity client ID. |
| `freezes` | `start`, `end` (time zone required) and `reason`. Production changes wait while a freeze is active. |
| `deploys` | Brakes on what one deploy may do without a person; see [Deploy brakes](#deploy-brakes). Every field has a default. |

## Deploy brakes

`platform.yaml`'s `deploys` block. A limit is `{share, floor}`, and is exceeded by more than `floor` items when that is also more than `share` (0–1) of the whole.

| Field | Default | Description |
|---|---|---|
| `prune_limit` | `{share: 0.05, floor: 20}` | A deploy that would delete more than this of the objects berth made in the cluster's app namespaces deletes nothing and applies nothing, and fails as the platform would (exit 1), listing the kinds by count. `--prune-all` goes past it; `--no-prune` deploys without deleting. |

## Rightsizing

`platform.yaml`'s `rightsizing` block:

| Field | Default | Description |
|---|---|---|
| `days` | `14` | How much history to read. 7–60. |
| `min_days` | `7` | With less history than this, the bot leaves the size alone. |
| `cpu_percentile` | `0.95` | The share of CPU samples the new size must cover (nearest rank). |
| `cpu_margin` | `1.25` | Headroom over that percentile. |
| `memory_margin` | `1.3` | Headroom over the peak working set. |
| `cpu_query` | `max:kubernetes.cpu.usage.total{service:$service,env:$env} by {pod_name}` | Datadog query returning CPU in nanocores, per pod. |
| `memory_query` | `max:kubernetes.memory.working_set{service:$service,env:$env} by {pod_name}` | Datadog query returning memory in bytes, per pod. |
| `oom_query` | `sum:kubernetes_state.container.status_report.count.terminated{service:$service,env:$env,reason:oomkilled}` | Datadog query counting OOM kills. |
| `price_per_vcpu_month`, `price_per_gib_month` | `25.00`, `3.50` | For the savings estimate only. |

Queries must contain `$service` and `$env`; berth appends `.rollup(max, N)` (`sum` for OOM kills), with `N` chosen so a series stays under 1,400 points. The rules, in order:

1. Less than `min_days` of data: no change.
2. Any OOM kill: the next size up with more memory.
3. Otherwise: the first size in menu order whose CPU is at least the percentile times the margin, and whose memory is at least the peak times the margin. A service that autoscales keeps its CPU request.

## mirrors.yaml

`platform/mirrors.yaml` lists upstream images to keep in the registry. Every app whose `image` is under `mirror/` needs an entry, and no two entries may fill one target.

| Field | Description |
|---|---|
| `mirrors.<name>.source` | Upstream image, `host/repository`, for example `docker.io/library/redis`. |
| `mirrors.<name>.target` | Repository in the registry, under `mirror/`. Apps refer to it as `image`. |
| `mirrors.<name>.track` | `X` or `X.Y`: follow the newest `X.*.*` or `X.Y.*` tag. Tags must be `X.Y.Z` or `vX.Y.Z`. |
| `mirrors.<name>.suffix` | Optional variant, such as `-alpine`, which tags must end with. |

New tags are released to the environment nothing promotes from (`dev`), for apps whose version there is older.

## Platform software

`platform/software/<name>.yaml` installs one Helm chart: cluster software with CRDs, controllers or webhooks. Every change is platform-tier. `berth deploy` renders each chart with `helm template` and applies it with server-side apply; Helm never installs anything, and chart output is not committed. A chart is rendered again only when its repository, chart, version, values or the cluster's Kubernetes version change: each install's namespace records a digest of them.

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

`platform/review.yaml` lists rules. Each rule has a `reason`, `paths`, and a `tier`, which is either one tier or a map with a tier for every environment. A change's path is a path into the app's merged spec for one environment, one key per segment, for example `services.api.size` or `config.LOG_LEVEL`. A key that contains a dot is still one segment: `files.*` matches the file `indexer.toml`, and reports show the path as `files[indexer.toml]`. `*` matches one segment and `**` any number. The first matching rule decides. Synthetic paths cover changes that have no field:

| Path | Meaning |
|---|---|
| `app` | the app was added or removed |
| `environment` | an environment file was added or removed |
| `callers.<app>` | another app added this one to its `talks_to`, so this app's network policy will admit it. The change belongs to the app being called, so its owners approve. Removing a caller narrows access and needs nobody. |
| `repo_file.<path>` | another file in the app's directory changed |
| `platform_file.<path>` | a file outside `apps/` changed |

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
- The release bot may change only `version`, and the rightsizing bot only sizes; anything else from either fails the gate. Each satisfies an `auto` tier on its own.
- An app's tier is the highest tier among its changes. Owners are read from the target branch, or from the change itself for a new app.

### Gate decision

The gate's state is the `berth/review` status.

| State | When |
|---|---|
| `failed` | The pull request's specs fail validation or rendering, judged by the target branch's berth; or an owner of an affected app, or a platform approver, voted reject (−10); or a bot changed something it may not. |
| `pending` | A requirement is unmet, or a production change falls in a freeze. With the `break-glass` label: until each app it changes has one approval, from that app's owners or a platform approver, and its platform files (if any) one from a platform approver, none of them the author. |
| `succeeded` | Every requirement is met. With `break-glass`: the approvals above, whatever the tiers and freezes. |

Votes of 10 (approved) and 5 (approved with suggestions) count as approval. Group votes are ignored. Policy, approvers, owners and the bots are all read from the target branch.

The gate does not compare anything rendered, because nothing rendered is committed. A change to berth's own code (`src/`, `scripts/`, `pyproject.toml`, `uv.lock`) is a platform-file change like any other. Its report notes that main's berth rendered the list of affected apps; the pull request build attaches what the new code renders. Because main's berth reads the specs, a pull request cannot both add a spec field to berth and use it: merge the code first.

## Rendered output

Nothing rendered is committed to main. `berth deploy` and `berth drift` render main in memory. `berth render` writes the same tree to `rendered/` (ignored by git), or to `--out`, for people to read. `berth diff` shows how a change alters it. After each cluster's deploy, `berth publish` commits that cluster's part to the `rendered` branch, which Airflow reads as its DAG bundle and the Control-M stage deploys from.

| Path in the rendered tree | Contents |
|---|---|
| `k8s/<cluster>/<app>-<env>/manifests.yaml` | Namespace, ServiceAccount, the `berth-owners` and `berth-break-glass` RoleBindings, ConfigMaps, ExternalSecret, and per service a Deployment, Service, HPA, PodDisruptionBudget, HTTPRoute and PersistentVolumeClaims as applicable; CronJobs; scheduler RoleBindings; NetworkPolicy. |
| `k8s/<cluster>/_platform/namespaces.yaml` | Namespaces for platform software. Not labelled as berth's, so app policies and app drift checks leave them alone. |
| `k8s/<cluster>/_platform/platform.yaml` | The `berth-break-glass` ClusterRole, ClusterRoles for Airflow and Control-M, a ClusterSecretStore per environment and one for the platform vault, the GatewayClass, the cluster's Gateway with a listener per environment, the HTTP-to-HTTPS redirect, and ExternalSecrets for platform software. |
| `k8s/<cluster>/_platform/admission.yaml` | The ValidatingAdmissionPolicies and their bindings; see [Admission policies](#admission-policies). |
| `airflow/<env>/<app>__<workflow>.py` | One DAG per Airflow workflow; DAG ID `<app>__<workflow>`. |
| `controlm/<env>/<app>.json` | Automation API folders `<app>-<workflow>`, one `Job:Kubernetes` per task. |

On the `rendered` branch, an app environment's files change only when its namespace deployed in full, or was already up to date. One that failed, was held or was skipped keeps its previous files, so its schedulers go on starting pods its namespace can run. Each commit's message names the cluster, the main commit and the outcomes, for example `aks-qa: rendered from 1a2b3c4d5e6f (deployed, unchanged)`.

What every app environment gets, whatever its spec says:

- A namespace `<app>-<env>`, with the `restricted` pod security standard, the owners, source and `calls` as annotations, and `berth.contoso.io/keep: "true"`, so prune never deletes it. Persistent volume claims carry the same annotation.
- Pods that run as UID 10001, non-root, with a read-only root filesystem, no privilege escalation, all capabilities dropped, the `RuntimeDefault` seccomp profile, and an emptyDir at `/tmp`.
- Images pinned by digest.
- `BERTH_APP`, `BERTH_ENV` and `BERTH_VERSION` in every container.
- When the environment sets `proxy`: `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, and their lower-case forms. `NO_PROXY` always holds `localhost`, `127.0.0.1`, `.svc`, `.cluster.local`, the cluster's `networks` and `.<domain>`, then the environment's own `no_proxy` entries.
- Telemetry that agrees with the spec. Every container gets `OTEL_SERVICE_NAME` (`<app>-<component>`) and `OTEL_RESOURCE_ATTRIBUTES` (`deployment.environment.name`, `service.version`, `service.namespace`). When the environment sets `otlp_endpoint`, it also gets `OTEL_EXPORTER_OTLP_ENDPOINT`, plus `HOST_IP` from the node when the endpoint uses `$(HOST_IP)`. Pods carry `tags.datadoghq.com/env`, `service` and `version`; Deployments and CronJobs carry `env` and `service`. The component is the service name, or `<workflow>-<task>` for a task.
- ConfigMaps for `config` and `files` named by a hash of their content, so a change restarts pods, and labelled `berth.contoso.io/versioned: "true"`, so deploys retire the old ones (see [Deploying and drift](#deploying-and-drift)).
- Rolling updates with `maxUnavailable: 0`.
- A NetworkPolicy that admits traffic only from the same namespace, from apps that list it in `talks_to`, and from the gateway namespace for services exposed to the company.

In production, services also spread across zones, and get a disruption budget when they have two or more pods.

## Access

Rendered from `owners` and each environment's `break_glass_groups` into each app namespace.

| Who | Kubernetes |
|---|---|
| An app's owners | `view` on the app's namespaces (`berth-owners`): everything except Secrets, including pod logs. |
| Members of an environment's `break_glass_groups` | `berth-break-glass` on that environment's app namespaces. They may read workloads, pods, logs, events, Services, ConfigMaps and volume claims; scale, restart and change Deployments; change ConfigMaps, Services, HPAs, disruption budgets, network policies and HTTPRoutes; create, change and delete Jobs and CronJobs; delete pods; and annotate ExternalSecrets to refresh them. They may not read Secrets, `exec`, `attach` or port-forward (each would reveal the app's secrets), change RBAC, or touch the Namespace itself or anything outside app namespaces. |
| Airflow's worker service account | Create pods in namespaces of apps with Airflow workflows (`berth-airflow`). |
| The Control-M agent's service account | Create jobs in namespaces of apps with Control-M workflows (`berth-controlm`). |

For production, make the break-glass groups Entra PIM groups: membership is then requested, approved and time-limited, and nobody has it by default. `berth breakglass` warns when the signed-in user is not in one of the environment's groups.

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

berth talks to the API server through `kubectl proxy`, on a Unix socket in a directory only the current user can open. kubectl reads the kubeconfig and signs in (kubelogin on AKS); berth calls the REST API, so conflicts and errors come back as structured answers. It needs Kubernetes 1.30 or later, for aggregated discovery.

`berth deploy` applies with server-side apply under the field manager `berth`. Unless `--app` is given, it first applies `_platform/namespaces.yaml`, platform software by `order` (charts whose digest changed), `_platform/platform.yaml` and `_platform/admission.yaml`. Then it takes each app namespace on the cluster, environment by environment in promotion order, several at a time (`--jobs`, default 8), and decides one of:

| Outcome | When |
|---|---|
| `unchanged` | The namespace's `berth.contoso.io/deployed-digest` equals the digest of what git renders for it, and neither `--all` nor `--revert` was given. Nothing is applied. |
| `held` | The environment it promotes from failed, was held, or was skipped for break-glass, for the same app in this run, on this cluster or in a `--hold-from` results file. Or that environment is on this cluster, is not part of this run (as with `--app APP --env prod`), and its namespace's `deployed-digest` is not what git renders for it. Nothing is applied; the detail says which. |
| `skipped` | A `berth-break-glass` ConfigMap in the namespace has an expiry in the future (unless `--include-break-glass`), or the namespace's `berth.contoso.io/deployed-commit` descends from the commit being deployed. |

Before applying anything, a deploy works out what it would delete: the objects git dropped from each namespace it may deploy, and what berth made in each retired environment. If that exceeds `deploys.prune_limit`, it stops there; see [Deploy brakes](#deploy-brakes).
| `deployed` | Every object applied and, unless `--no-wait`, every Deployment finished rolling out within 600 seconds. |
| `failed` | An object did not apply (a conflict with a change made by hand names the field), or a rollout did not finish. The namespace is not stamped, so the next deploy tries again. |

A Deployment's replica count changes hands when its service starts or stops autoscaling, and a deploy moves it without moving a pod. When a service gets `autoscale`, git stops setting the count; berth first holds the live count as field manager `berth-handover`, because a field its only applier lets go of returns to its default of one pod. The autoscaler takes the count from there. When a service goes back to `replicas`, berth applies that Deployment over the autoscaler's count, which is no change made by hand, and deletes the HorizontalPodAutoscaler git dropped before applying, even with `--no-prune`: left until after, it would go on overriding the count git now sets. If a person also changed the Deployment, the apply stops with the usual conflict instead.

After applying a namespace it:

- reports fields set by another field manager, other than `berth`, `berth-deploy`, `berth-break-glass`, `berth-handover` and `kube-controller-manager`, ignoring status, `kubectl.kubernetes.io/restartedAt`, and fields berth also sets (a change adopted into git); with `--revert`, removes them with a strategic merge patch (built-in kinds only);
- deletes objects labelled as berth's that git no longer renders, except Namespaces, PersistentVolumeClaims, versioned ConfigMaps, objects with owner references and anything annotated `berth.contoso.io/keep: "true"`; with `--no-prune`, lists them instead;
- once deployed, stamps the namespace with `berth.contoso.io/deployed-digest`, `deployed-commit` and `deployed-at`, as field manager `berth-deploy`.

Unless `--app` is given, every berth namespace on the cluster with no environment file in git is a retired environment. Without a break-glass mark, the deploy deletes what berth made there under the same exemptions, so its workloads stop, removes the namespace's deploy stamps, so the environment deploys in full if git brings it back, and reports it as on the cluster but no longer in git. The Namespace, its volume claims and anything kept stay for a person to delete. With `--no-prune`, it only reports it.

Then, in every namespace that runs what git renders and has no break-glass mark, it retires versioned ConfigMaps that are neither rendered nor named by a live Deployment or CronJob. It annotates each with `berth.contoso.io/superseded-at` and deletes it once that is more than seven days old. A ConfigMap that is rendered again loses the annotation.

`--results-out` writes what happened as JSON: the cluster, the commit, the platform's outcome, and each namespace's outcome. The deploy pipeline passes it to the next cluster's deploy as `--hold-from`, and to `berth publish`.

`berth drift` reads each cluster with one list per kind: berth's namespaces, the break-glass marks, berth's objects, and every other object in app namespaces. Namespaces that look different get a server-side dry run of each rendered object, diffed against the live object, without what the API server keeps for itself (status, versions, managed fields). So does every namespace with `--full`. It reports, per app namespace:

| Line | Meaning |
|---|---|
| `not deployed: …` | the namespace's deployed digest is not what git renders: a deploy has not finished since the last change |
| `differs from git: Kind/name` | applying git's version would change the object |
| `missing: Kind/name` | git renders an object the cluster does not have |
| `changed by hand: Kind/name: manager: path` | a field set by a field manager other than berth or Kubernetes' controllers |
| `made by hand: Kind/name` | an object without berth's label, not owned by a controller, and not one Kubernetes or berth creates (`default` ServiceAccount, `kube-root-ca.crt`, `berth-break-glass`) |
| `Kind/name (removed from git)` | an object berth made that git no longer renders (versioned ConfigMaps excepted: deploys retire those) |
| `on the cluster but no longer in git` | a berth namespace with no environment file |
| `break-glass by … until …` / `EXPIRED` | the namespace's break-glass mark |
| `could not check: …` | the API server refused or failed for that namespace |

## CLI

```text
berth [--root ROOT] COMMAND
```

`--root` is the repository root; the default is the current directory.

| Command | Arguments | Behaviour |
|---|---|---|
| `check` | | Validates every spec and cross-reference, and renders everything; fails if `platform/schema/` is stale or `pipelines/templates/targets.yml` disagrees with `platform.yaml`. Prints `ok: N apps, M app environments` on success. |
| `render` | `[--out DIR]` | Writes the rendered tree to `DIR` (default `rendered/`, which git ignores) and deletes files no longer produced. Refuses a non-empty directory that is not a rendered tree. |
| `diff` | `[--base REF] [--out PATH]` | Renders `REF` (default `main`) and the working tree with the berth that runs it, and prints a unified diff of the rendered trees. With `--out`, writes the diff there and prints one line per changed file. |
| `review` | `--author ID [--base REF] [--approve ID]… [--reject ID]… [--label NAME]… [--markdown PATH]` | Compares the working tree with `REF` (default `main`) and prints each change, its tier and reason, and the gate's decision for the simulated votes and labels. |
| `gate` | `--pr N --base REF [--target-url URL] [--summary PATH]` | Reads pull request `N` from Azure DevOps, decides, and posts `berth/review`. Needs `SYSTEM_ACCESSTOKEN`, `SYSTEM_COLLECTIONURI`, `SYSTEM_TEAMPROJECT` and `BUILD_REPOSITORY_ID`. |
| `release` | `APP --env ENV --version TAG@DIGEST` | Sets `version` in `apps/APP/ENV.yaml`, keeping comments. The file must exist. |
| `promote` | `APP --to ENV` | Copies `version` from the environment `ENV` promotes from. |
| `new` | `NAME --description TEXT --owner ID --owner ID --source URL --version TAG@DIGEST [--env ENV]` | Creates `apps/NAME/app.yaml` with one service and `apps/NAME/ENV.yaml` (default `dev`). Fails if the directory exists. |
| `schema` | `[--check]` | Writes the JSON Schemas in `platform/schema/`. |
| `explain` | `APP --env ENV` | Prints the app's owners, namespace and image in `ENV`; how to look at it and its company URLs; every `links` entry; each merged setting with the file that set it (or `default`); the Kubernetes objects rendered; and the scheduler for each workflow. Exits 1 if the app does not run in `ENV`. |
| `images` | `[--base REF]` | Prints `registry/repository@digest<TAB>tag` for every image the specs deploy, or only those not deployed at `REF`. |
| `deploy` | `--cluster CLUSTER [--app APP [--env ENV]] [--all] [--no-prune \| --prune-all] [--revert] [--no-wait] [--include-break-glass] [--no-software] [--jobs N] [--results-out PATH] [--hold-from PATH]… [--context CTX] [--kubeconfig PATH]` | See [Deploying and drift](#deploying-and-drift). Refuses to run unless the specs validate and render. With `--app`, deploys only that app (and not the platform). `--all` applies namespaces that are already up to date; `--revert` implies it. `--no-prune` deletes nothing; `--prune-all` deletes past the prune brake. Exits 1 if the platform failed or the prune brake stopped the run (no app was deployed), 2 if any app environment failed or was held, 0 otherwise. |
| `drift` | `--cluster CLUSTER … [--slack] [--details PATH] [--full] [--jobs N] [--context CTX] [--kubeconfig PATH]` | Reports each cluster's drift. `--slack` posts the summary to `SLACK_WEBHOOK_URL`; `--details` writes the full diffs; `--full` diffs every namespace. |
| `breakglass` | `APP --env ENV (--reason TEXT [--hours N] \| --end) [--context CTX] [--kubeconfig PATH]` | Writes the `berth-break-glass` ConfigMap in the app's namespace, with who (as the API server knows them), why and until when (default 8 hours), or deletes it. Warns if the user is not in the environment's `break_glass_groups`. |
| `publish` | `--cluster CLUSTER [--results PATH] [--remote NAME] [--branch NAME] [--no-push]` | Commits the cluster's part of the rendered tree onto the branch (default `rendered`, on `origin`) and pushes it. With `--results`, app environments that did not deploy keep their previous files. Uses git's plumbing, so the checkout and index are untouched. The push is not forced. |
| `rightsize` | `--env ENV [--app APP] --plan-out PATH` | Reads Datadog (`DD_API_KEY`, `DD_APP_KEY`, `DD_SITE`) for every service and task in `ENV`, prints a recommendation for each, and writes the plan as JSON. Changes no files. |
| `rightsize` | `--apply PLAN --app APP [--markdown PATH]` | Writes `APP`'s proposed sizes from a plan into its environment file, and the pull request description to `PATH`. |
| `rightsize` | `--apps PLAN` | Prints the apps a plan changes, one per line. |
| `mirror` | `--plan-out PATH` | Lists each mirror's upstream tags, and writes the releases to make as JSON: the tag to copy and the apps to release it to. Changes no files. |
| `mirror` | `--rows PLAN` | Prints a plan's releases, one per line: source, target, tag, environment and app, tab-separated. |
| `pr` | `--branch NAME --title TEXT [--description TEXT \| --description-file PATH] [--auto-complete] [--path PATH]…` | Commits what changed under the paths (default `apps`), force-pushes it to the branch, and opens a pull request into main, or updates the one already open from that branch. Needs `BERTH_ADO_TOKEN` (or `SYSTEM_ACCESSTOKEN`), `SYSTEM_COLLECTIONURI` and `SYSTEM_TEAMPROJECT`; `BERTH_ADO_PROJECT` and `BERTH_ADO_REPOSITORY` name the deploy repository when the job runs elsewhere. |

kubectl's context defaults to the cluster's name. `BERTH_KUBECTL` and `BERTH_HELM` choose the binaries (default: from `PATH`).

Every command exits 0 on success and 1 on a problem, except `deploy`, which exits 2 when only app environments failed. `review` and `gate` exit 0 whatever the decision; the decision is in their output and, for `gate`, in the posted status. `drift` exits 0 whatever it finds.
