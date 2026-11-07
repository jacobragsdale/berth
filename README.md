# berth

berth is a proof of concept for deploying a few hundred services and their batch jobs to AKS from one repository, without one person reviewing every change. Each app has a short spec that its owners maintain. berth renders it to plain Kubernetes manifests, DAG files for Airflow and job definitions for Control-M. A merge gate decides, from what a change touches, whether it needs nobody else, another owner of the app, or the platform team.

A pipeline applies the result with `kubectl` server-side apply. Every morning a report in Slack says where the clusters differ from git: who changed which field by hand, which objects nobody rendered, and which emergency fixes have outlived their break-glass mark. There is no controller to run in the cluster; the moving parts are Python and kubectl.

It is a proof of concept with invented apps and `contoso` placeholders. Locally verified:

- the unit tests, and end-to-end runs of the CLI in throwaway git repositories
- `berth deploy`, `drift`, `breakglass`, `--revert` and `--prune` against a real Kubernetes API server and etcd (envtest 1.34)
- the built-in admission policies refusing what they should, and admitting every pod Airflow and Control-M start
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
ok: 6 apps, 17 app environments
```

Change a production setting for one app and ask for a much bigger pod for another, render, and ask who must approve:

```sh
printf 'config:\n  LOG_LEVEL: warn\n' >> apps/payments-api/prod.yaml
sed -i 's/^    size: m$/    size: xl/' apps/ledger/app.yaml
uv run berth render
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

Add `--approve bob.osei@contoso.com` and the payments-api line becomes `owner review satisfied`. Run `git diff rendered/` to see what Kubernetes would receive, and `uv run berth explain payments-api --env prod` to see every merged setting with the file it came from. In the working copy where the repository was created, use `--base main` instead of `origin/main`.

## How the pieces fit

People edit `apps/<app>/app.yaml` and one file per environment, `apps/<app>/<env>.yaml`. `berth render` writes `rendered/`, and CI fails if the two disagree. After a merge, `pipelines/main.yml` runs `berth deploy` on the qa cluster, then prod, then Control-M. Airflow reads `rendered/airflow/<env>/` as a Git DAG bundle. On a pull request, `pipelines/pr.yml` runs the checks, and `pipelines/gate.yml` posts the `berth/review` status that the branch policy requires. `pipelines/drift.yml` posts the drift report to Slack every morning.

| Path | Owned by | What it holds |
|---|---|---|
| `apps/<app>/` | the app's owners | `app.yaml`, and `dev.yaml`, `qa.yaml`, `prod.yaml` for each environment the app runs in |
| `platform/platform.yaml` | platform team | environments, clusters, vaults, the proxy, the Gateway, the size menu, approvers, break-glass groups, freezes |
| `platform/review.yaml` | platform team | which changes need which review |
| `platform/software/` | platform team | cluster software as pinned Helm charts: External Secrets Operator and Envoy Gateway |
| `platform/schema/` | generated | JSON Schemas for editor completion (`berth schema`) |
| `rendered/` | generated | what is deployed; never edited by hand |
| `pipelines/` | platform team | Azure Pipelines for pull requests, the gate, deploys, the drift report, and app releases |
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
scripts/validate-rendered.sh
uv run pytest -m integration
```

`scripts/fetch-tools.sh` downloads checksum-verified kubeconform v0.8.0, Helm v4.3.1, and envtest v1.34.1 (kube-apiserver, etcd and kubectl) into `.tools/`; the other scripts call it. `validate-rendered.sh` schema-checks everything under `rendered/`, offline after the first run. `pytest -m integration` starts a local API server for the deploy, drift and admission tests, and also runs `validate-rendered.sh` and `validate-software.sh`, which downloads and renders the platform software charts.
