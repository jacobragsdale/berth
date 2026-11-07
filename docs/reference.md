# Reference

This page covers the spec files, the platform files, the review rules, what berth renders, and the CLI. It is written from `src/berth/model.py`, `src/berth/review.py` and `berth --help`. The JSON Schemas in `platform/schema/` are generated from the same models, and are what editors use. Unknown fields are errors in every file.

## app.yaml

`apps/<app>/app.yaml` describes the app in every environment.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Must equal the directory name. Lower-case letters, digits and `-`, at most 40 characters, starting with a letter. |
| `description` | string | required | 1–200 characters. Shown on Control-M folders. |
| `owners` | list of identities | required | At least two, no duplicates. Lower-case Azure DevOps or Entra sign-in names (`name@domain`). Groups are not accepted. |
| `source` | URL | required | `https://` URL of the repository that builds the image. Shown on the namespace. |
| `image` | string | `name` | Repository in the platform registry. The full image is `<registry>/<image>:<version>`. |
| `config` | map of string to string | `{}` | Environment variables for every container. Keys match `^[A-Z_][A-Z0-9_]*$`. Values must be strings, so quote numbers and booleans. |
| `files` | map of file name to string | `{}` | Files mounted read-only at `/config/<name>`. |
| `secrets` | list of names | `[]` | Key Vault secrets read from `<app>--<name>` and exposed as the environment variable `NAME` (upper case, `-` becomes `_`). |
| `talks_to` | list of `app/service` | `[]` | Services this app calls. Each one injects `<APP>_<SERVICE>_URL` and admits this app through the target's network policy. The target must run in every environment this app runs in, and must have a `port`. An app may not list itself. |
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
| `expose` | `internal` or `public` | `internal` | `public` adds an Ingress and needs `port` and `hostname`. |
| `hostname` | DNS label | none | Public only. The environment's `public_domain` is appended. |
| `volumes` | map of name to `{path, size_gi}` | `{}` | Persistent disks (ReadWriteOnce, 1–1024 GiB). A service with volumes runs one replica, rolls out with `Recreate`, and defaults to 1 replica. |

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
| `release_bot` | Identity whose pull requests may change versions only. |
| `platform_approvers` | Individuals, any one of whom satisfies a platform-tier review. |
| `max_replicas_without_review` | `replicas` or `autoscale.max` above this needs platform review. |
| `storage_class` | Storage class for `volumes`. |
| `environments.<env>` | `cluster`, `key_vault`, `production` (default `false`), `promotes_from`, `default_replicas`, `ingress_class`, `ingress_namespace`, `public_domain`, `airflow` (`namespace`, `service_account`) and `controlm` (`server`, `connection_profile`, `timezone`, `agent_namespace`, `agent_service_account`). |
| `sizes.<name>` | `cpu`, `memory`, and `platform_review` (default `false`). The memory limit equals `memory`; there is no CPU limit. |
| `identities` | `<app>/<env>` to workload identity client ID. |
| `freezes` | `start`, `end` (time zone required) and `reason`. Production changes wait while a freeze is active. |

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
- These raise a change to `platform`: a new `size` with `platform_review: true`; `replicas` or `autoscale.max` above `max_replicas_without_review`; `expose` set to `public`; a `version` not found in the history of the `promotes_from` environment's file on the target branch.
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
| `rendered/k8s/<cluster>/<app>-<env>/manifests.yaml` | Namespace, ServiceAccount, ConfigMaps, ExternalSecret, and per service a Deployment, Service, HPA, PodDisruptionBudget, Ingress and PersistentVolumeClaims as applicable; CronJobs; scheduler RoleBindings; NetworkPolicy. |
| `rendered/k8s/<cluster>/_flux/<app>-<env>.yaml` | The Flux Kustomization that applies the directory above. |
| `rendered/k8s/<cluster>/_platform/platform.yaml` | ClusterRoles for Airflow and Control-M, and a ClusterSecretStore per environment on the cluster. |
| `rendered/airflow/<env>/<app>__<workflow>.py` | One DAG per Airflow workflow; DAG ID `<app>__<workflow>`. |
| `rendered/controlm/<env>/<app>.json` | Automation API folders `<app>-<workflow>`, one `Job:Kubernetes` per task. |

What every app environment gets, whatever its spec says:

- A namespace `<app>-<env>`, with the `restricted` pod security standard, the owners and source as annotations, and pruning disabled.
- Pods that run as UID 10001, non-root, with a read-only root filesystem, no privilege escalation, all capabilities dropped, the `RuntimeDefault` seccomp profile, and an emptyDir at `/tmp`.
- Images pinned by digest.
- `BERTH_APP`, `BERTH_ENV` and `BERTH_VERSION` in every container.
- Config maps named by content hash, so a change restarts pods.
- Rolling updates with `maxUnavailable: 0`.
- A NetworkPolicy that admits traffic only from the same namespace, from apps that list it in `talks_to`, and from the ingress namespace for public services.

In production, services also spread across zones, and get a disruption budget when they have two or more pods.

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
| `schema` | `[--check]` | Writes `platform/schema/app.schema.json` and `env.schema.json`. |

Every command exits 0 on success and 1 on a problem. `review` and `gate` exit 0 whatever the decision; the decision is in their output and, for `gate`, in the posted status.
