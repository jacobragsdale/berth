# berth

berth is a proof of concept for deploying a few hundred services and their batch jobs to AKS from one repository, without one person reviewing every change. Each app has a short spec that its owners maintain. berth renders it to plain Kubernetes manifests, DAG files for Airflow and job definitions for Control-M. A merge gate decides, from what a change touches, whether it needs nobody else, another owner of the app, or the platform team.

After a merge, a pipeline renders `main` and applies it with server-side apply, one app namespace at a time: only what changed, several at once, and a failing app holds only itself. It publishes what each cluster now runs to a `rendered` branch, which Airflow and Control-M read. Every morning a report in Slack says where the clusters differ from git: who changed which field by hand, which objects nobody rendered, and which emergency fixes have outlived their break-glass mark. There is no controller to run in the cluster; the moving parts are Python and kubectl.

It is a proof of concept with invented apps and `contoso` placeholders. Locally verified:

- the unit tests, and end-to-end runs of the CLI in throwaway git repositories
- `berth deploy`, `drift`, `breakglass` and `--revert` against a real Kubernetes API server and etcd (envtest 1.34): holds, parallel deploys, deleting what git dropped and the brake on deleting too much, retiring old settings, and what the break-glass role may and may not do
- the built-in admission policies refusing what they should, and admitting every pod Airflow and Control-M start
- publishing to the `rendered` branch, against a local git remote
- rendered manifests against Kubernetes 1.33 and 1.34 and CRD schemas (kubeconform)
- both platform software charts, rendered with their values and schema-checked
- the generated DAGs, parsed by Airflow 3.2.2 with KubernetesPodOperator building their pods

Nothing has run against Azure DevOps, AKS, Key Vault, Datadog, Slack or Control-M; see [what is unverified](docs/design.md#what-is-unverified). New to it? Start with the [tutorial](docs/tutorial.md).

## Try it

You need [uv](https://docs.astral.sh/uv/) and git. In a clone of this repository:

```sh
uv sync --locked
uv run berth check
```

```text
ok: 7 apps, 20 app environments
```

Change a production setting for one app and ask for a much bigger pod for another, then ask who must approve:

```sh
printf 'config:\n  LOG_LEVEL: warn\n' >> apps/payments-api/prod.yaml
sed -i 's/^    size: m$/    size: xl/' apps/ledger/app.yaml
uv run berth review --base origin/main --author alice.ng@contoso.com
```

On macOS, use `sed -i ''` instead of `sed -i`.

```text
ledger: platform review
  dev   services.api.size  m -> xl  platform  size xl needs platform review
  qa    services.api.size  m -> xl  platform  size xl needs platform review
  prod  services.api.size  m -> xl  platform  size xl needs platform review
payments-api: owner review
  prod  config.LOG_LEVEL  info -> warn  owner  application settings
gate: pending
  ledger: needs approval from an owner other than the author (carol.diaz@contoso.com, dev.patel@contoso.com) and the platform team (infra.lead@contoso.com, platform.oncall@contoso.com)
  payments-api: needs approval from an owner other than the author (bob.osei@contoso.com)
```

Add `--approve bob.osei@contoso.com` and the payments-api line becomes `owner review satisfied`. Run `uv run berth diff --base origin/main` to see what Kubernetes would receive, and `uv run berth explain payments-api --env prod` to see every merged setting with the file it came from. In the working copy where the repository was created, use `--base main` instead of `origin/main`.

## How the pieces fit

People edit `apps/<app>/app.yaml` and one file per environment, `apps/<app>/<env>.yaml`, and nothing else: nothing rendered is committed. `berth render` writes the rendered tree to `rendered/` (ignored by git) for reading, and `berth diff` shows what a change does to it. After a merge, `pipelines/main.yml` runs `berth deploy` on the qa cluster, then prod, publishing each cluster's part to the `rendered` branch, then deploys Control-M from that branch. Before prod, `berth deploy --plan` decides what the deploy would do, and the run waits for a platform approver if it would change the platform or many production apps at once. Airflow reads `airflow/<env>/` from the same branch as a Git DAG bundle. On a pull request, `pipelines/pr.yml` runs the checks, and `pipelines/gate.yml` posts the `berth/review` status that the branch policy requires. `pipelines/drift.yml` posts the drift report to Slack every morning. Two bots open pull requests through the same gate, with `berth pr`: `pipelines/rightsize.yml` proposes sizes from a fortnight of Datadog usage every Monday, and `pipelines/mirror.yml` copies new upstream image versions into ACR every day and opens their dev releases.

| Path | Owned by | What it holds |
|---|---|---|
| `apps/<app>/` | the app's owners | `app.yaml`, and `dev.yaml`, `qa.yaml`, `prod.yaml` for each environment the app runs in |
| `platform/platform.yaml` | platform team | environments (with their break-glass groups), clusters, vaults, the proxy, the Gateway, the size menu, approvers, freezes |
| `platform/review.yaml` | platform team | which changes need which review |
| `platform/software/` | platform team | cluster software as pinned Helm charts: External Secrets Operator and Envoy Gateway |
| `platform/mirrors.yaml` | platform team | upstream images copied into ACR for third-party apps, and kept current by the mirror bot |
| `platform/schema/` | generated | JSON Schemas for editor completion (`berth schema`) |
| `rendered/` (local) and the `rendered` branch | generated | what `berth render` writes for reading, never committed to main; and what each cluster runs, published by the deploy pipeline |
| `pipelines/` | platform team | Azure Pipelines for pull requests, the gate, deploys, the drift report, app releases, and the rightsizing and mirror bots; `templates/targets.yml` lists the clusters and environments they loop over |
| `src/berth/` | platform team | the renderer, review logic, deploy and drift tooling, and CLI |

## Documentation

- [Tutorial](docs/tutorial.md) walks through adding an app on your own machine, and what each review tier feels like.
- [Design](docs/design.md) explains why berth works this way, starting from what the problem needs, and what it chose not to do.
- [Research](docs/research.md) summarises how Google, Meta, Uber, Netflix, Stripe and others solve the same problem, with sources.
- [How-to guides](docs/how-to.md) cover the everyday tasks: adding an app, seeing it run, exposing it to company callers, third-party software, releasing, promoting, secrets, emergency fixes, the drift report, and setting up clusters and pipelines.
- [Reference](docs/reference.md) lists every spec field, the review tiers, the rendered objects, the admission policies, what deploy and drift do, and the CLI.

## Developing berth

```sh
uv run pre-commit run --all-files
uv run pytest
uv run berth check
scripts/validate-rendered.sh
uv run pytest -m integration
```

`.github/workflows/ci.yml` runs the same five checks on every push and pull request to `main` on GitHub. `uv run pytest -m scale` times `check`, `render`, `deploy` and `drift` on 300 synthetic apps; it takes about four minutes and is not part of either run.

`scripts/fetch-tools.sh` downloads checksum-verified kubeconform v0.8.0, Helm v4.3.1, and envtest v1.34.1 (kube-apiserver, etcd and kubectl) into `.tools/`; the other scripts call it. `validate-rendered.sh` renders the specs and schema-checks the result, offline after the first run. `pytest -m integration` starts a local API server for the deploy, drift and admission tests, and also runs `validate-rendered.sh` and `validate-software.sh`, which downloads and renders the platform software charts.
