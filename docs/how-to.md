# How-to guides

These guides are for app owners and the platform team working in the deploy repository. Each one assumes a clone with `uv sync --locked` done. Guides that change the repository end the same way: commit the spec and the rendered output together, and open a pull request into `main`.

A few terms recur. The gate is the `berth/review` status on every pull request, and `main`'s branch policy requires it to pass. It decides from what the change touches how much review it needs, called its tier:

- `auto` needs no approval when an owner of the app opened the pull request.
- `owner` needs another owner of the app.
- `platform` needs another owner and one of the platform approvers listed in `platform/platform.yaml`.

The full rules are in [the review rules reference](reference.md#review-rules). Owners, approvers and `YOU` in the commands below are all the same kind of identity: your lower-case Azure DevOps sign-in name, such as `alice.ng@contoso.com`.

To see which tier a change needs and who can approve it, before pushing:

```sh
uv run berth render
uv run berth review --base origin/main --author YOU
```

The output lists each change with its tier and reason, then names the people whose approval is still missing. Add them as reviewers on the pull request; the gate does not add reviewers itself.

## How to add an app

Add an app when you have a new image to run. An app is one image and everything that runs from it, so a repository that builds two images needs two apps.

Prerequisites: a second person who agrees to co-own the app, and an image pushed to `contosoacr.azurecr.io`.

To add an app:

1. Look up the image's digest:

   ```sh
   az acr repository show --name contosoacr --image fx-pricer:0.1.0 --query digest -o tsv
   ```

2. Scaffold the app, replacing `DIGEST` with the 64 hexadecimal characters after `sha256:` in that output:

   ```sh
   uv run berth new fx-pricer --description "Streams FX prices to the trading desk" \
     --owner kai.muller@contoso.com --owner lena.rossi@contoso.com \
     --source https://dev.azure.com/contoso/trading/_git/fx-pricer \
     --version 0.1.0@sha256:DIGEST
   ```

   This writes `apps/fx-pricer/app.yaml`, with one service named `api` on port 8080, and `apps/fx-pricer/dev.yaml`, which holds only the version.

3. Edit `app.yaml` to describe what the app runs: services, workflows, settings, secret names, and the services it calls. With a YAML language server, such as the Red Hat YAML extension for VS Code, your editor completes and checks fields from the schema named on the file's first line. For every field, see the [app.yaml reference](reference.md#appyaml).
4. Render and check:

   ```sh
   uv run berth render
   uv run berth check
   ```

   `check` prints `ok: …`, or lists each problem with its file and field.

A new app needs approval from the co-owner and a platform approver. After the merge, Flux applies `rendered/k8s/aks-qa/fx-pricer-dev/` to the qa cluster. To deploy the app to qa or prod later, add `apps/fx-pricer/qa.yaml` or `prod.yaml` containing at least `version:`. A first deploy to qa needs the co-owner, and a first deploy to prod needs a platform approver as well.

## How to release a version to dev

Apps whose repository uses the release template release to dev automatically. Pushing a git tag builds the image tagged with it, and the pipeline opens a pull request that sets `version` in `dev.yaml`. It sets auto-complete on that pull request, so Azure DevOps completes it as soon as the gate passes. The gate passes without a human because the release bot changed only a dev version and the rendered output that follows from it.

To use the template, add this to the app repository's pipeline:

```yaml
trigger:
  tags:
    include: ['*']
resources:
  repositories:
    - repository: berth
      type: git
      name: platform/berth
steps:
  - template: pipelines/templates/release.yml@berth
    parameters:
      app: payments-api
```

To release to dev by hand, which needs no approval when you own the app:

```sh
uv run berth release payments-api --env dev --version 1.6.0@sha256:DIGEST
uv run berth render
```

`release` changes only the `version:` line and keeps the file's comments and layout. A version is always `tag@sha256:digest`, because the digest is what runs and the tag is for people.

## How to promote a version to qa or prod

`berth promote` copies the version from the environment the target promotes from in your working tree: dev for qa, and qa for prod.

```sh
uv run berth promote ledger --to prod
uv run berth render
```

```text
apps/ledger/prod.yaml: version 3.1.0@sha256:d8fdf1a9…; run `berth render`
```

A qa promotion needs no approval when you own the app. A prod promotion needs another owner. In both cases the version must already appear in the source environment's file somewhere in `main`'s history: `dev.yaml` for qa, `qa.yaml` for prod. If it does not, the gate also requires a platform approver and says so (`3.1.0 has not run in qa`). berth reads git history, not the cluster, so "has run" means "was merged to main", not that the deploy succeeded or passed tests.

You can promote to qa and prod in one pull request by running `promote` twice, qa first. The prod change then needs a platform approver, because the version is not yet in `qa.yaml` on `main`. Promoting in two pull requests avoids that.

## How to change a setting

Settings are `config` (environment variables) and `files` (files mounted read-only at `/config/<name>`). Settings in `app.yaml` apply in every environment. An environment file can set `config` for that environment only, overriding `app.yaml` key by key. `files` and `secrets` live only in `app.yaml`.

```yaml
config:
  LOG_LEVEL: info
  RETRY_LIMIT: "5"
files:
  indexer.toml: |
    [sources]
    sharepoint = true
```

Quote values that YAML reads as numbers or booleans, such as `"5"` or `"true"`. A setting must be a string, and berth rejects anything else. A changed setting renames the ConfigMap, so the app's pods restart with it.

The tier depends on which environments the change reaches, not on which file you edit. A setting changed in `dev.yaml` needs no approval from an owner. The same setting changed in `app.yaml` also changes prod, so it needs another owner.

## How to add a secret

Secret values never enter git. The spec names the secret, and External Secrets Operator copies it from the environment's Key Vault into a Kubernetes Secret in the app's namespace.

To add a secret:

1. Add its name to `secrets` in `app.yaml`, for example `secrets: [db-password]`.
2. Have the value set in each environment's vault (`kv-contoso-dev`, `kv-contoso-qa`, `kv-contoso-prod`) under the name `APP--NAME`, here `payments-api--db-password`. berth does not set values, and this repository has no self-service path for it. A platform approver, or whoever your organisation gives Key Vault write access, sets the value.
3. Render and open the pull request. After the merge, the app's containers get the value as an environment variable named after the secret in upper case with `-` replaced by `_`, here `DB_PASSWORD`.

If the vault has no such secret, the ExternalSecret reports a sync error and the Kubernetes Secret is not created. Pods that need it then fail to start with `CreateContainerConfigError`, and start once the value exists. A changed value reaches running pods only when they restart: External Secrets Operator refreshes the Secret hourly, and environment variables are read at container start.

## How to call another app's service

Add the service to `talks_to` as `APP/SERVICE`:

```yaml
talks_to: [ledger/api]
```

After rendering, your containers get `LEDGER_API_URL=http://api.ledger-<env>.svc.cluster.local`, on port 80, which forwards to the service's `port`. Ledger's network policy admits traffic from your namespace. The target must run in every environment your app runs in and must have a `port`, or `berth check` names the environment that is missing it. Ledger's owners are not asked to approve.

## How to move a job between Control-M and Airflow

Change the workflow's `orchestrator` in `app.yaml`, for example from `controlm` to `airflow` under `workflows.var` in `apps/risk-batch/app.yaml`, then render. The rendered diff removes `rendered/controlm/<env>/risk-batch.json` and adds `rendered/airflow/<env>/risk-batch__var.py`; DAG IDs are `<app>__<workflow>`. It also swaps the scheduler's RoleBinding in the app's namespaces. The pods are identical either way.

The change needs no approval in dev and qa when you own the app, and another owner in prod. Moving to Control-M has two limits:

- The workflow's `timezone` must be the Control-M server's (`America/New_York`).
- The `schedule` must run at one fixed time on listed weekdays or month days, such as `30 18 * * 1-5`.

`berth check` names the problem when either is not met. Moving a workflow off Control-M leaves its folder, named `<app>-<workflow>`, on the Control-M server, because the main-branch pipeline only adds and updates folders. Ask your Control-M administrators to delete it after the merge.

## How to change something the spec cannot express

Use an override, which is an RFC 7386 JSON merge patch applied to one rendered object. Give the object's `kind` and `name`. Deployments are named `<app>-<service>`, Services `<service>` and CronJobs `<app>-<workflow>`; `rendered/k8s/<cluster>/<app>-<env>/manifests.yaml` shows every object. Say why the override is needed:

```yaml
overrides:
  - target: {kind: Deployment, name: fx-gateway-fix}
    reason: FIX sessions need the static-egress node pool and two minutes to log out cleanly
    patch:
      spec:
        template:
          spec:
            terminationGracePeriodSeconds: 120
            nodeSelector: {agentpool: fxegress}
```

In a merge patch, a mapping merges, `null` deletes a key, and a list replaces the whole list. Put an override in `app.yaml` to apply it in every environment, or in one environment's file for that environment only. Every override needs a platform approver. The admission policies still check the result: images must come from the registry pinned by digest, and containers must keep memory limits. See [Who judges a pull request](design.md#who-judges-a-pull-request).

## How to ship during an incident or a freeze

Production changes wait during the freezes listed in `platform/platform.yaml`, and the gate's status says until when. The gate does not re-run when a freeze ends: re-run the `berth-gate` pipeline for the pull request, or push a commit. Pushing resets approvals.

To ship anyway:

1. Add the label `break-glass` to the pull request. Anyone who can edit the pull request can add it.
2. Get approval from one owner of the app, or one platform approver, other than yourself.
3. If the status does not change within a few minutes, re-run the `berth-gate` pipeline. Whether label changes trigger the gate's service hook has not been verified.

With the label, the gate succeeds whatever the tiers and freezes say, provided `berth check` passes. One other person is enough by design, so that an incident is never blocked on a particular reviewer. Afterwards, ask a platform approver to review what shipped. The gate does not raise an alert or schedule that review.

## How to stop running an app in an environment

Delete the environment's file and render:

```sh
git rm apps/doc-indexer/qa.yaml
uv run berth render
```

Stopping an app in dev needs no approval when you own it, in qa another owner, and in prod a platform approver as well. If another app lists this one in `talks_to` in that environment, `berth check` fails until that app changes too.

After the merge, Flux removes the app's workloads. It keeps the namespace and any persistent volume claims, which berth marks `kustomize.toolkit.fluxcd.io/prune: disabled`. A platform operator deletes the namespace once the data is no longer needed. The app's Key Vault secrets and any Control-M folders also stay until someone deletes them. Retiring the whole app means deleting its directory, which needs a platform approver.

## How to see what a platform change does to every app

Changes to `platform/`, `src/`, `pipelines/` or `scripts/` always need a platform approver, judged by the policy on `main`. To see their effect on the fleet:

1. Make the change, then run `uv run berth render`.
2. Run `uv run berth review --base origin/main --author YOU`. Apps whose output changed without a spec change appear under "rendered output changes without a spec change". The same list is in the gate's pipeline summary, which is the only place their owners hear about it. Their approval is not required.
3. Read `git diff rendered/` for the apps you expected to change, and for any you did not.

A change to `platform/review.yaml` does not affect the pull request that makes it. That pull request is judged by the table on `main`, so a new rule applies only after it merges.

## How to set up the gate in Azure DevOps

This setup has not been run against a real organisation. Treat each step as a checklist item to verify.

Prerequisites: Project Administrator on the project, and a dedicated identity for the gate (a service principal, or a user with a personal access token) with Code (read) and Code (status) access to the repository. The gate identity only reads pull requests and posts statuses; it never merges.

To set up the gate:

1. Create the pipeline `berth-pr` from `pipelines/pr.yml`, and add it to `main`'s branch policy as required build validation. It runs the tests, `berth check`, schema and policy validation, and a review preview, all from the pull request's own copy of the repository.
2. Create an Incoming WebHook service connection named `berth-pr-events`.
3. Add service hooks on the repository for **Pull request created** and **Pull request updated** that post to the webhook URL Azure DevOps shows for that connection. Pull request updated includes reviewer votes.
4. Create a variable group `berth-gate`, linked to Key Vault, with a secret `BERTH_GATE_TOKEN` holding the gate identity's token. Grant pipeline permission to the gate pipeline only.
5. Create the pipeline `berth-gate` from `pipelines/gate.yml`. It runs `main`'s berth against each pull request and posts `berth/review`.
6. In `main`'s branch policy, require the status `berth/review`, with the authorized identity set to the gate identity.
7. In the same policy, turn on **Reset all approval votes** on new pushes, so approvals apply to the final change. Do not require a minimum number of reviewers, because the gate decides that per change.
8. Set `release_bot` in `platform/platform.yaml` to the release identity's unique name, as it appears in a pull request's `createdBy.uniqueName`.

To confirm it works, open a pull request that changes a prod setting. `berth/review` should show pending and name the other owners. Once one of them approves, it should show succeeded.
