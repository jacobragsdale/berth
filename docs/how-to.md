# How-to guides

These guides are for app owners and the platform team working in the deploy repository. Each one assumes a clone with `uv sync --locked` done. Guides that change the repository end the same way: commit the spec and open a pull request into `main`. Nothing rendered is committed; the deploy pipeline renders `main` itself.

A few terms recur. The gate is the `berth/review` status on every pull request, and `main`'s branch policy requires it to pass. It decides from what the change touches how much review it needs, called its tier:

- `auto` needs no approval when an owner of the app opened the pull request.
- `owner` needs another owner of the app.
- `platform` needs another owner and one of the platform approvers listed in `platform/platform.yaml`.

The full rules are in [the review rules reference](reference.md#review-rules). Owners, approvers and `YOU` in the commands below are all the same kind of identity: your lower-case Azure DevOps sign-in name, such as `alice.ng@contoso.com`.

To see which tier a change needs and who can approve it, and what it does to the rendered Kubernetes objects and DAGs, before pushing:

```sh
uv run berth review --base origin/main --author YOU
uv run berth diff --base origin/main
```

`review` lists each change with its tier and reason, then names the people whose approval is still missing. Add them as reviewers on the pull request; the gate does not add reviewers itself. `diff` prints the rendered output's diff; the pull request build attaches the same diff to its run.

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
4. Check it:

   ```sh
   uv run berth check
   ```

   `check` validates and renders everything, and prints `ok: …`, or lists each problem with its file and field.

5. See what the spec turned into, with every setting and the file it came from:

   ```sh
   uv run berth explain fx-pricer --env dev
   ```

A new app needs approval from the co-owner. After the merge, the deploy pipeline applies the app's namespace `fx-pricer-dev` on the qa cluster and waits for the rollout; its log shows `fx-pricer-dev: applied 7 objects`. To deploy the app to qa or prod later, add `apps/fx-pricer/qa.yaml` or `prod.yaml` containing at least `version:`. A first deploy to qa needs the co-owner, and a first deploy to prod needs a platform approver as well.

## How to release a version to dev

Apps whose repository uses the release template release to dev automatically. Pushing a git tag builds the image tagged with it, and the pipeline opens a pull request that sets `version` in `dev.yaml`. It sets auto-complete on that pull request, so Azure DevOps completes it as soon as the gate passes. The gate passes without a human because the release bot changed only a dev version.

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
```

`release` changes only the `version:` line and keeps the file's comments and layout. A version is always `tag@sha256:digest`, because the digest is what runs and the tag is for people.

## How to promote a version to qa or prod

`berth promote` copies the version from the environment the target promotes from in your working tree: dev for qa, and qa for prod.

```sh
uv run berth promote ledger --to prod
```

```text
apps/ledger/prod.yaml: version 3.1.0@sha256:d8fdf1a9…
```

A qa promotion needs no approval when you own the app. A prod promotion needs another owner. In both cases the version must already appear in the source environment's file somewhere in `main`'s history: `dev.yaml` for qa, `qa.yaml` for prod. If it does not, the gate also requires a platform approver and says so (`3.1.0 has not run in qa`). berth reads git history, not the cluster, so "has run" means "was merged to main", not that the deploy succeeded or passed tests.

You can promote to qa and prod in one pull request by running `promote` twice, qa first. The prod change then needs a platform approver, because the version is not yet in `qa.yaml` on `main`. Promoting in two pull requests avoids that.

## How to see your app running

Owners can read everything in their app's namespaces except Secrets, with their own sign-in. You need the Azure CLI (signed in with `az login`), kubectl and kubelogin. Get credentials for each cluster once; dev and qa are on `aks-qa`, prod is on `aks-prod` (resource group `rg-aks-prod`):

```sh
az aks get-credentials --resource-group rg-aks-qa --name aks-qa --context aks-qa
kubelogin convert-kubeconfig -l azurecli
```

Then use kubectl, or the Headlamp desktop app, which reads the same credentials and needs nothing installed in the cluster:

```sh
kubectl --context aks-qa -n payments-api-dev get pods
kubectl --context aks-qa -n payments-api-dev logs deployment/payments-api-api
kubectl --context aks-qa -n payments-api-dev get events --sort-by=.lastTimestamp
```

- **Is my change deployed?** The namespace records the commit last deployed to it in full: `kubectl --context aks-qa get namespace payments-api-dev -o jsonpath='{.metadata.annotations.berth\.contoso\.io/deployed-commit}'`. Your change is in if your commit is that one or an ancestor of it: `git merge-base --is-ancestor YOUR_COMMIT DEPLOYED_COMMIT && echo deployed`. The `berth-deploy` pipeline run for the merge says, per app environment, `applied` and whether each rollout finished; the `rendered` branch's history shows what each cluster runs, one commit per deploy.
- **What if it failed?** The run names the app environment and why: an object the API server refused, `not applied` (a conflict with a change made by hand; see [How to resolve what the drift report shows](#how-to-resolve-what-the-drift-report-shows)), or `did not become ready` (the rollout did not finish in ten minutes). The stage then shows "succeeded with issues", and every other app deploys as usual. Your app's next environment is `held`, on this cluster and the next, so a change that broke dev or qa goes no further. The next run tries again, whatever it was triggered by. Nothing rolls back automatically, but a stuck rollout never takes capacity away: the old pods keep serving until new ones are ready. Fix forward, or revert the commit.
- **Is it as git says?** The daily drift report in Slack lists any app whose cluster state differs from git.
- **Where did a value come from?** `uv run berth explain payments-api --env dev` shows every setting with the file that set it, and links to the app's logs and traces in Datadog.
- **Traces, metrics and older logs:** Datadog, filtered by the `service` and `env` tags that berth sets on every pod. A service's Datadog name is `<app>-<service>`, for example `payments-api-api`.

## How to expose a service to company callers

"Company" means the company network, not the internet: other company systems call the service by URL. A service is either `internal` (the default: reachable only by other apps in the cluster, through `talks_to`) or `company`. Only HTTP and HTTPS are routed this way. Give the service a hostname and `expose: company`:

```yaml
services:
  api:
    port: 8080
    expose: company
    hostname: quotes
```

Open the pull request. The service answers at `https://quotes.<domain>` in each environment, where `platform.yaml` sets the domain: `quotes.dev.apps.contoso.com` in dev, `quotes.qa.apps.contoso.com` in qa and `quotes.apps.contoso.com` in prod. To expose it in one environment only, run the app in that environment only: the hostname applies wherever the app runs. No DNS or certificate request is needed, because each environment has a wildcard record and certificate. `berth check` refuses a hostname that another service already uses in the same environment.

A new hostname needs no approval in dev and qa, and another owner in prod. Changing an existing prod hostname breaks every caller that uses the old one, so tell them first.

## How to run third-party software

Software that only runs a container, such as Redis or a vendor's web app, is an ordinary app. Software with CRDs, controllers or webhooks is platform software instead: the platform team adds it as a file in `platform/software/`, like the two there; see [How to upgrade platform software](#how-to-upgrade-platform-software).

You need permission to import into the registry (the `AcrPush` role on `contosoacr`), and Helm if the vendor ships a chart.

1. Copy the image into ACR, and note its digest:

   ```sh
   az acr import --name contosoacr --source docker.io/library/redis:8.2.1 --image mirror/redis:8.2.1
   az acr repository show --name contosoacr --image mirror/redis:8.2.1 --query digest -o tsv
   ```

2. Read what the vendor's chart produces, if there is one: `helm template` it and note the image's command, arguments, port, environment variables, config files, volumes and health checks.
3. Write the app with `berth new`, passing `--version 8.2.1@sha256:DIGEST` with the digest from step 1. Set `image: mirror/redis`, and replace the generated `api` service with what you noted. Leave out the chart's ServiceAccount, RBAC and disruption budget; berth makes its own. Without a `health` path, berth checks health by connecting to the port, which suits non-HTTP services. Paths the image writes to need `writable`, because the root filesystem is read-only. Those directories start empty on every restart; use `volumes` for data that must survive one:

   ```yaml
   image: mirror/redis
   services:
     redis:
       port: 6379
       size: s
       args: [--save, ""]
       writable: [/data]
   ```

4. Add the image to `platform/mirrors.yaml`, so the mirror bot keeps it current, unless another app already mirrors the same image: then reuse its `target`. `berth check` refuses an image under `mirror/` without an entry. The entry's key is only a name. Like everything under `platform/`, the change needs a platform approver:

   ```yaml
   mirrors:
     redis:
       source: docker.io/library/redis
       target: mirror/redis
       track: "8.2"  # the newest 8.2.x
   ```

5. Run `uv run berth check`. Anything the spec cannot express becomes an override, which needs a platform approver.

`apps/rates-cache/` is a worked example: Redis configured with a file, with no health path and a writable working directory.

Databases holding data that matters are not run this way. Ask Azure or the DBA team for a managed one, and connect to it with a host in `config` and the password in `secrets`.

## How to keep a third-party image current

The mirror bot (`pipelines/mirror.yml`) runs every morning. For each entry in `platform/mirrors.yaml` it finds the newest upstream tag on the entry's `track`, copies it into ACR, and opens a dev release pull request for every app whose dev version is an older tag. "Older" compares the `X.Y.Z` numbers. A rebuild that an upstream pushes under the same tag is not noticed. The pull request comes from the release bot, which may change versions only, so in dev it merges once the checks pass, without a person. Promote it to qa and prod as you would your own releases.

- **Follow a newer line:** change `track` from `"8.2"` to `"8.3"` in a pull request, which needs a platform approver. Every app on that mirror then gets a dev release of the new line. There is no per-app opt-out: an app that must stay behind needs its own mirror entry with its own target.
- **Follow a variant:** add `suffix: "-alpine"` to follow tags such as `8.2.11-alpine`. Release candidates and other suffixes are never taken.
- **See what it would do:** this reads the public registries and changes nothing in the repository:

  ```sh
  uv run berth mirror --plan-out plan.json
  ```

  It prints one line per pending update, and writes the same as JSON to `plan.json`:

  ```text
  docker.io/library/redis:8.2.11 -> mirror/redis:8.2.11, release to dev: rates-cache
  ```

Upstream images reach dev without a person reviewing them. The track limits how far they move, and only dev is automatic. If your registry scans images, as Microsoft Defender for Containers does, check its findings before promoting.

## How to respond to a rightsizing pull request

Every Monday the rightsizing bot (`pipelines/rightsize.yml`) compares each service's and task's size with what it used in Datadog over the last 14 days. Sizes are the menu in `platform/platform.yaml` (`xs` to `xxl`); each is a CPU and memory pair. The bot proposes the smallest size that holds the 95th percentile of CPU plus 25%, and the peak memory plus 30%. Where that differs from the current size, it opens one pull request per app per environment. The description gives each change, the reason, the observed CPU and peak memory, and an estimated monthly saving.

- **In dev and qa** the pull request merges once the checks pass: the bot may change sizes only, and a size change there needs no approval.
- **In prod** an owner approves it. If nobody does, it stays open, and the next week's run updates the same pull request rather than opening another. Close it to keep the current size; the bot proposes again if the numbers still say so. There is no way yet to stop proposals for one workload.
- **Merging restarts the pods** with the new size, as a rolling update.
- **A workload killed for memory** is never shrunk. The bot proposes one size up instead.
- **A service that autoscales on CPU** (it has `autoscale:` in its spec) is only offered sizes with at least its current CPU, because the autoscaler's target is a percentage of the CPU request.
- **No proposal for a workload with less than seven days of data,** such as a new app, or with no data at all, for example when a metric name does not match your Datadog account.

The window is 14 days by default. A batch job that peaks once a month needs a longer one: set `rightsizing.days` in `platform.yaml` (up to 60) to cover your longest cycle.

To see what it would propose without opening anything, run it with Datadog keys in the environment. The platform team holds an API key and an application key with permission to read metrics; `DD_SITE` is your Datadog site, such as `datadoghq.com` or `datadoghq.eu`:

```sh
DD_API_KEY=... DD_APP_KEY=... DD_SITE=datadoghq.com uv run berth rightsize --env prod --app payments-api --plan-out plan.json
```

It prints one line per workload, such as `payments-api services.api: m -> s: smaller size fits the observed use with margin; p95 CPU 120m, peak memory 380Mi, over 14 days`, and writes the plan to `plan.json`.

The bot writes sizes into environment files, such as `services.api.size` in `prod.yaml`, or `workflows.<name>.tasks.<task>.size` for a task. A size in an environment file wins over the one in `app.yaml` for that environment, so after the bot has set one, change it in the environment file rather than `app.yaml`. You can set those by hand the same way.

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

Quote values that YAML reads as numbers or booleans, such as `"5"` or `"true"`. A setting must be a string, and berth rejects anything else. A changed setting renames the ConfigMap, so the app's pods restart with it. Deploys delete the old ConfigMap a week after it was replaced, once nothing uses it, so a workflow run that started on the old settings can still finish.

The tier depends on which environments the change reaches, not on which file you edit. A setting changed in `dev.yaml` needs no approval from an owner. The same setting changed in `app.yaml` also changes prod, so it needs another owner.

## How to add a secret

Secret values never enter git. The spec names the secret, and External Secrets Operator copies it from the environment's Key Vault into a Kubernetes Secret in the app's namespace.

To add a secret:

1. Add its name to `secrets` in `app.yaml`, for example `secrets: [db-password]`.
2. Set the value in each environment's vault (`kv-contoso-dev`, `kv-contoso-qa`, `kv-contoso-prod`) under the name `APP--NAME`, here `payments-api--db-password`. Developers can write to every environment's vault:

   ```sh
   az keyvault secret set --vault-name kv-contoso-dev --name payments-api--db-password --value "$VALUE"
   ```

   Key Vault keeps every version, so a mistaken value can be restored, and its audit log records who wrote it.
3. Open the pull request. After the merge, the app's containers get the value as an environment variable named after the secret in upper case with `-` replaced by `_`, here `DB_PASSWORD`.

If the vault has no such secret, the ExternalSecret reports a sync error and the Kubernetes Secret is not created. Pods that need it then fail to start with `CreateContainerConfigError`, and start once the value exists. A changed value reaches running pods only when they restart: External Secrets Operator refreshes the Secret hourly, and environment variables are read at container start.

## How to call another app's service

Add the service to `talks_to` as `APP/SERVICE`:

```yaml
talks_to: [ledger/api]
```

Your containers get `LEDGER_API_HOST=api.ledger-<env>.svc.cluster.local`, `LEDGER_API_PORT` (the service's own `port`) and `LEDGER_API_URL=http://api.ledger-<env>.svc.cluster.local:<port>`, and ledger's network policy admits traffic from your namespace. The target must run in every environment your app runs in and must have a `port`, or `berth check` names the environment that is missing it.

Because ledger's network policy changes, the gate also counts this as a change to ledger, shown as `callers.YOUR-APP`, and ledger's owners decide. In dev and qa one of them approves, unless you own ledger too; in prod one of them other than you. Add them as reviewers. Dropping an entry from `talks_to` narrows access, and needs nobody from ledger.

## How to move a job between Control-M and Airflow

Change the workflow's `orchestrator` in `app.yaml`, for example from `controlm` to `airflow` under `workflows.var` in `apps/risk-batch/app.yaml`. `uv run berth diff --base origin/main` shows that it removes `controlm/<env>/risk-batch.json` and adds `airflow/<env>/risk-batch__var.py`; DAG IDs are `<app>__<workflow>`. It also swaps the scheduler's RoleBinding in the app's namespaces. The pods are identical either way.

The change needs no approval in dev and qa when you own the app, and another owner in prod. Moving to Control-M has two limits:

- The workflow's `timezone` must be the Control-M server's (`America/New_York`).
- The `schedule` must run at one fixed time on listed weekdays or month days, such as `30 18 * * 1-5`.

`berth check` names the problem when either is not met. Moving a workflow off Control-M leaves its folder, named `<app>-<workflow>`, on the Control-M server, because `ctm deploy` only adds and updates folders. Ask the Control-M team to delete it after the merge.

## How to change something the spec cannot express

Use an override, which is an RFC 7386 JSON merge patch applied to one rendered object. Give the object's `kind` and `name`. Deployments are named `<app>-<service>`, Services `<service>` and CronJobs `<app>-<workflow>`; `uv run berth explain APP --env ENV` lists every object, and `uv run berth render` writes them to `rendered/k8s/<cluster>/<app>-<env>/manifests.yaml` to read. Say why the override is needed:

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

## How to fix something live in an emergency

Each environment in `platform/platform.yaml` names its `break_glass_groups`. Their members can change that environment's app namespaces with kubectl, using the credentials from [How to see your app running](#how-to-see-your-app-running). In dev and qa that is every developer. Production's group is managed by Entra PIM, so nobody is in it until they ask:

1. In the Entra portal, under **Privileged Identity Management** > **My roles** > **Groups**, activate your membership of the prod break-glass group, with a reason. It lasts a few hours and then ends by itself.
2. Sign in again, so that kubectl sees the new group: `kubelogin remove-tokens`, then run any kubectl command.

Mark the change first, so that deploys leave it alone and the drift report says who made it and why:

```sh
uv run berth breakglass payments-api --env prod --reason "pool exhausted, scaling up" --hours 4
kubectl --context aks-prod -n payments-api-prod scale deployment/payments-api-api --replicas=8
```

`berth breakglass` warns if you are not in the environment's group yet. Break-glass can scale, restart and change Deployments, change ConfigMaps, Services, autoscalers, disruption budgets, network policies and routes, run and delete Jobs and CronJobs, and delete pods. It cannot read Secrets, open a shell in a container, or port-forward, because each would reveal the app's secrets. Ask the platform team if you need one of those.

The mark is a ConfigMap named `berth-break-glass` in the app's namespace, holding your sign-in name, your reason and when it expires. Until then, deploys skip that namespace and log that they did. After it expires, deploys apply the app again when git changes it, and the drift report flags the mark as `EXPIRED`. A deploy that meets your change then stops with a conflict until someone deals with it. Before then, do one of two things:

- **Keep the change.**
  1. Open a pull request that makes the spec say what you did, labelled `break-glass` if it cannot wait for the review tiers or a freeze.
  2. After it merges, end the mark: `uv run berth breakglass payments-api --env prod --end`. Ending the mark deploys nothing by itself.
  3. The merge's deploy applies the app once the mark has ended; if you ended it afterwards, re-run the `berth-deploy` pipeline. Git now sets the same values you did, so it applies without a conflict, and your change stops counting as drift.
- **Undo it.** End the mark, then run the `berth-deploy` pipeline with **Put git back over changes made by hand** ticked. From a clean checkout of current `main`, `uv run berth deploy --cluster aks-prod --app payments-api --env prod --revert` does the same for one app.

If you change something without marking it, nothing undoes it either. The next deploy that changes that app stops with a conflict naming the field, and the next drift report lists the change as "not marked".

## How to resolve what the drift report shows

The report arrives in Slack every morning, one message per cluster, with one line per app namespace that needs attention. The platform team acts on it. The pipeline run each message comes from holds the full diffs. Run any deploy below from the `berth-deploy` pipeline, or from a clean checkout of current `main`, never from a branch.

- **"changed by hand: Kind/name: manager: path"**: someone changed that field outside berth. The manager is the tool Kubernetes recorded for the change: `kubectl-edit`, `kubectl-set`, or `kubectl` for a scale. Adopt it (change the spec so git says the same, merge, and deploy the app), or put git back with `uv run berth deploy --cluster CLUSTER --app APP --env ENV --revert`.
- **"made by hand: Kind/name"**: an object nobody rendered. Delete it with kubectl (a break-glass group member or the platform team), or add what it does to the spec.
- **"not deployed"**: git renders something the last full deploy of that namespace did not apply, because a deploy failed, was held, or has not run yet. The deploy pipeline's last run says why; the next run tries again.
- **"differs from git"** or **"missing"**: the cluster does not match git, for example after someone deleted an object. Deploy the app again with `--all`, or with `--revert` if a conflict blocks it.
- **"(removed from git)"**: git no longer renders that object. Run the `berth-deploy` pipeline with **Delete objects git no longer renders** ticked, or from a clean checkout of `main`, `uv run berth deploy --cluster CLUSTER --app APP --env ENV --all --prune`. Prune only touches objects berth labelled, in the namespaces it deploys, and never namespaces or volume claims. Old settings ConfigMaps never appear here: deploys retire those themselves.
- **"on the cluster but no longer in git"**: the app or environment was removed. A platform operator deletes the namespace once its data is no longer needed.
- **"break-glass … EXPIRED"**: the mark outlived its expiry. Ask whoever made it to keep or undo the change, as in [How to fix something live in an emergency](#how-to-fix-something-live-in-an-emergency).

## How to ship during an incident or a freeze

Production changes wait during the freezes listed in `platform/platform.yaml`, and the gate's status says until when. The gate does not re-run when a freeze ends: re-run the `berth-gate` pipeline for the pull request, or push a commit. Pushing resets approvals.

To ship anyway:

1. Add the label `break-glass` to the pull request. Anyone who can edit the pull request can add it.
2. Get one approval, from someone other than yourself, for each part the pull request changes: for each app, one of its owners or a platform approver; for files outside `apps/`, a platform approver. A pull request that changes one app needs one person.
3. If the status does not change within a few minutes, re-run the `berth-gate` pipeline. Whether label changes trigger the gate's service hook has not been verified.

With the label, the gate succeeds whatever the tiers and freezes say, provided the specs validate. One other person per part is enough by design, so that an incident is never blocked on a particular reviewer. It is per part so that two owners of one app cannot also change another app, or the platform, under the label. Afterwards, ask a platform approver to review what shipped. The gate does not raise an alert or schedule that review.

## How to stop running an app in an environment

Delete the environment's file:

```sh
git rm apps/doc-indexer/qa.yaml
```

Stopping an app in dev needs no approval when you own it, in qa another owner, and in prod a platform approver as well. If another app lists this one in `talks_to` in that environment, `berth check` fails until that app changes too.

After the merge, the deploy reports the namespace as "on the cluster but no longer in git", and the daily drift report lists it until it is gone. Nothing is deleted automatically. A platform operator deletes the namespace once its data, including any persistent volume claims, is no longer needed. The app's Key Vault secrets and any Control-M folders also stay until someone deletes them. Retiring the whole app means deleting its directory, which needs a platform approver.

## How to see what a platform change does to every app

Changes to `platform/`, `src/`, `pipelines/` or `scripts/` always need a platform approver, judged by the policy on `main`. To see their effect on the fleet:

1. Make the change, then run `uv run berth review --base origin/main --author YOU`. Apps whose output changed without a spec change appear under "rendered output changes without a spec change". The same list is in the gate's pipeline summary, which is the only place their owners hear about it. Their approval is not required.
2. Run `uv run berth diff --base origin/main --out rendered.diff`, and read the diff for the apps you expected to change, and for any you did not.

For a change to berth's own code, such as the renderer, the gate runs `main`'s berth, so its list of affected apps leaves the change out, and its report says so. The pull request build runs your berth, and attaches `rendered.diff` from step 2 to its run: that is the diff to review. Main's berth also reads the specs, so a pull request cannot both add a spec field to berth and use it. Merge the code first.

A change to `platform/review.yaml` does not affect the pull request that makes it. That pull request is judged by the table on `main`, so a new rule applies only after it merges.

## How to upgrade platform software

Platform software is the cluster software in `platform/software/<name>.yaml`: External Secrets Operator and Envoy Gateway. Each file has one install per cluster, under `installs`. Every change to it needs a platform approver.

1. Upgrade the qa cluster first. Set `version` on the qa install only, which overrides the shared version:

   ```yaml
   installs:
     envoy-gateway:
       cluster: aks-qa
       namespace: envoy-gateway-system
       version: v1.10.0
   ```

2. Render the charts and validate the output. The script downloads the charts, and its own Helm and kubeconform, so it needs network access:

   ```sh
   scripts/validate-software.sh
   ```

   The last line is kubeconform's summary, and it should report `Invalid: 0, Errors: 0`. CustomResourceDefinitions are counted as skipped.
3. Read the chart's release notes for the versions you skip. The pull request pipeline attaches a diff of the rendered charts between `main` and your branch. A removed CustomResourceDefinition deletes every object of that kind, so stop and ask if the diff shows one.
4. After the merge and a quiet day on qa, set the shared `version` to the new one and remove the qa install's override, in a second pull request.

The deploy pipeline renders each chart with `helm template` and applies it, before the platform objects and the apps. It renders a chart again only when its version, values or the cluster's Kubernetes version change.

## How to set up a cluster

This is a one-time step per cluster for the platform team, after the Azure setup in [What needs cloud engineering](design.md#what-needs-cloud-engineering). It needs cluster-admin on the cluster.

1. Get credentials, with the context named after the cluster (`rg-aks-prod` and `aks-prod` for prod), and the pinned tools:

   ```sh
   az aks get-credentials --resource-group rg-aks-qa --name aks-qa --context aks-qa
   kubelogin convert-kubeconfig -l azurecli
   scripts/fetch-tools.sh
   export BERTH_KUBECTL=.tools/bin/kubectl BERTH_HELM=.tools/bin/helm
   ```

2. Deploy everything, without waiting for rollouts the first time, because secrets cannot sync until External Secrets Operator is running:

   ```sh
   uv run berth deploy --cluster aks-qa --no-wait
   ```

   It applies the platform software's namespaces, the charts, the platform objects (secret stores, the Gateway, the scheduler roles) and the admission policies, then every app.
3. Once the controllers are running, check the cluster:

   ```sh
   uv run berth drift --cluster aks-qa
   ```

   The first line should end `N in sync, 0 need attention`. Anything else is listed below it; see [How to resolve what the drift report shows](#how-to-resolve-what-the-drift-report-shows).

From then on, the deploy pipeline does this after every merge.

## How to set up break-glass access

This is a one-time step for whoever administers Entra ID, with the platform team. Without it, the `break_glass_groups` in `platform/platform.yaml` grant nothing.

1. For dev and qa, create an Entra security group, or reuse one that holds every developer, and put its object ID in both environments' `break_glass_groups`.
2. For prod, create a separate security group with no members, and put its object ID in prod's `break_glass_groups`.
3. Onboard the prod group to **Privileged Identity Management** for groups. Make every developer **eligible** for membership, not active. In the membership settings, require a justification on activation, set the maximum activation to a few hours, and decide whether activation needs approval. An approval step is slower in an incident; without one, the justification and the PIM audit log are the record.
4. Check that AKS sees the group: activate your eligibility, sign in again (`kubelogin remove-tokens`), and run `kubectl auth whoami -o yaml` against the prod cluster. The group's object ID should be among your groups. `kubectl auth can-i patch deployments -n ledger-prod` should then say `yes`, and `kubectl auth can-i get secrets -n ledger-prod` should say `no`.

## How to set up the deploy and drift pipelines

These have not been run against a real organisation. Treat each step as a checklist item to verify.

1. Create service connections `berth-aks-qa` and `berth-aks-prod` with workload identity federation, each for an identity that is cluster-admin on its cluster. Deploys apply CRDs, ClusterRoles and admission policies, which need it. The drift pipeline uses the same connections, because its diffs are server-side dry runs and Kubernetes cannot grant "dry run only"; `berth drift` never writes.
2. Give the project's Build Service identity **Contribute** and **Create branch** on the repository, for the `rendered` branch, which `berth publish` creates on the first deploy and adds a commit to after each one. Add a branch policy, or branch permissions, so that nobody else can push to `rendered`.
3. Create the pipeline `berth-deploy` from `pipelines/main.yml`, and Azure Pipelines environments `berth-aks-qa`, `berth-aks-prod`, `berth-controlm-dev`, `berth-controlm-qa` and `berth-controlm-prod`. On `berth-aks-qa` and `berth-aks-prod`, add the **Exclusive lock** check: with the pipeline's `lockBehavior: runLatest`, a newer run then replaces a queued older one, and two deploys never run at once. Add business-hours checks on the prod environments if you want them.
4. Create variable groups `berth-controlm-dev`, `berth-controlm-qa` and `berth-controlm-prod`, each holding `CTM_ENDPOINT` and a `CTM_TOKEN` issued by the Control-M team. Airflow needs nothing here: point each environment's Git DAG bundle at the `rendered` branch, subdirectory `airflow/<env>/`, with Airflow's own git connection.
5. If you add a cluster or an environment, add it to `pipelines/templates/targets.yml` too. `berth check` fails until the two agree.
6. Create a variable group `berth-drift`, linked to Key Vault, holding `SLACK_WEBHOOK_URL`: an incoming webhook for the channel that should get the report.
7. Create the pipeline `berth-drift` from `pipelines/drift.yml`. It runs at 13:00 UTC every day.
8. For the rightsizing bot:
   1. Check the Datadog metric names in `platform.yaml`'s `rightsizing` against your account first: run `berth rightsize` for one app, as in [How to respond to a rightsizing pull request](#how-to-respond-to-a-rightsizing-pull-request), and compare its numbers with Datadog's own graphs. With wrong names it proposes nothing; with names that measure something else it proposes wrong sizes, which merge by themselves in dev and qa.
   2. Create a variable group `berth-datadog`, linked to Key Vault, holding `DD_API_KEY`, `DD_APP_KEY` and `DD_SITE`.
   3. Create an Azure Resource Manager service connection `berth-rightsizing`, with workload identity federation, for the bot's own identity. Give that identity **Contribute**, **Force push**, **Create branch** and **Contribute to pull requests** on the repository, and set `rightsizing_bot` in `platform.yaml` to its unique name, as it appears in a pull request's `createdBy.uniqueName`. The pipeline signs in as it and passes its token to `berth pr`, so its pull requests are the bot's, never the build service's.
   4. Create the pipeline `berth-rightsize` from `pipelines/rightsize.yml`. It runs every Monday at 12:00 UTC.
9. For the mirror bot, use the release bot's connection `berth-release`, which the app release template also uses. Create it as in step 8.3 for the release bot's identity, `release_bot` in `platform.yaml`, and also give that identity `AcrPush` on the registry. Then create the pipeline `berth-mirror` from `pipelines/mirror.yml`, which runs every day at 06:00 UTC. Public upstream registries need no credentials.
10. Grant each variable group pipeline permission to the pipeline that uses it only.

## How to set up the gate in Azure DevOps

This setup has not been run against a real organisation. Treat each step as a checklist item to verify.

Prerequisites: Project Administrator on the project, and a dedicated identity for the gate (a service principal, or a user with a personal access token) with Code (read) and Code (status) access to the repository. The gate identity only reads pull requests and posts statuses; it never merges.

To set up the gate:

1. Create the pipeline `berth-pr` from `pipelines/pr.yml`, and add it to `main`'s branch policy as required build validation. It runs the tests, `berth check`, schema checks, the integration tests against a local API server, the rendered diff, and a review preview, all from the pull request's own copy of the repository.
2. Create an Incoming WebHook service connection named `berth-pr-events`.
3. Add service hooks on the repository for **Pull request created** and **Pull request updated** that post to the webhook URL Azure DevOps shows for that connection. Pull request updated includes reviewer votes.
4. Create a variable group `berth-gate`, linked to Key Vault, with a secret `BERTH_GATE_TOKEN` holding the gate identity's token. Grant pipeline permission to the gate pipeline only.
5. Create the pipeline `berth-gate` from `pipelines/gate.yml`. It runs `main`'s berth against each pull request and posts `berth/review`.
6. In `main`'s branch policy, require the status `berth/review`, with the authorized identity set to the gate identity.
7. In the same policy, turn on **Reset all approval votes** on new pushes, so approvals apply to the final change. Do not require a minimum number of reviewers, because the gate decides that per change.
8. Set `release_bot` in `platform/platform.yaml` to the release identity's unique name, as it appears in a pull request's `createdBy.uniqueName`.

To confirm it works, open a pull request that changes a prod setting. `berth/review` should show pending and name the other owners. Once one of them approves, it should show succeeded.
