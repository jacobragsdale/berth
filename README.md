# berth

berth is a proof of concept for deploying a few hundred services and their batch jobs to AKS from one repository, without one person reviewing every change. Each app has a short spec that its owners maintain. berth renders it to plain Kubernetes manifests for Flux, DAG files for Airflow and job definitions for Control-M. A merge gate decides, from what a change touches, whether it needs nobody else, another owner of the app, or the platform team.

It is a proof of concept with invented apps and `contoso` placeholders. Locally verified:

- the unit and end-to-end tests
- rendered manifests against Kubernetes 1.33 and CRD schemas (kubeconform)
- every rendered pod, including those Airflow and Control-M start, against the admission policies (Kyverno CLI)
- the generated DAGs, parsed by Airflow 3.2.2 with KubernetesPodOperator building their pods

Nothing has run against Azure DevOps, AKS, Key Vault or Control-M; see [what is unverified](docs/design.md#what-is-unverified).

## Try it

You need [uv](https://docs.astral.sh/uv/) and git. In a clone of this repository:

```sh
uv sync --locked
uv run berth check
```

```text
ok: 6 apps, 17 app environments
```

Change a production setting for one app and a public hostname for another, render, and ask who must approve:

```sh
printf 'config:\n  LOG_LEVEL: warn\n' >> apps/payments-api/prod.yaml
sed -i 's/hostname: portal$/hostname: my-portal/' apps/customer-portal/app.yaml
uv run berth render
uv run berth review --base origin/main --author alice.ng@contoso.com
```

```text
customer-portal: platform review
  dev   services.web.hostname  portal -> my-portal  platform  a public hostname
  qa    services.web.hostname  portal -> my-portal  platform  a public hostname
  prod  services.web.hostname  portal -> my-portal  platform  a public hostname
payments-api: owner review
  prod  config.LOG_LEVEL  info -> warn  owner  application settings
gate: pending
  customer-portal: needs approval from an owner other than the author (erin.walsh@contoso.com, farid.haddad@contoso.com) and the platform team (infra.lead@contoso.com, platform.oncall@contoso.com)
  payments-api: needs approval from an owner other than the author (bob.osei@contoso.com)
```

Add `--approve bob.osei@contoso.com` and the payments-api line becomes `owner review satisfied`. Run `git diff rendered/` to see what Kubernetes would receive. In the working copy where the repository was created, use `--base main` instead of `origin/main`.

## How the pieces fit

People edit `apps/<app>/app.yaml` and one file per environment, `apps/<app>/<env>.yaml`. `berth render` writes `rendered/`, and CI fails if the two disagree. Flux in each cluster applies `rendered/k8s/<cluster>/`, Airflow reads `rendered/airflow/<env>/` as a Git DAG bundle, and the main-branch pipeline runs `ctm deploy` on `rendered/controlm/<env>/`. On a pull request, `pipelines/pr.yml` runs the checks, and `pipelines/gate.yml` posts the `berth/review` status that the branch policy requires.

| Path | Owned by | What it holds |
|---|---|---|
| `apps/<app>/` | the app's owners | `app.yaml`, and `dev.yaml`, `qa.yaml`, `prod.yaml` for each environment the app runs in |
| `platform/platform.yaml` | platform team | environments, clusters, vaults, the size menu, platform approvers, freezes |
| `platform/review.yaml` | platform team | which changes need which review |
| `platform/admission/` | platform team | Kyverno policies, the same rules enforced at the API server |
| `platform/flux/` | platform team | one-time Flux bootstrap per cluster |
| `platform/schema/` | generated | JSON Schemas for editor completion (`berth schema`) |
| `rendered/` | generated | what is deployed; never edited by hand |
| `pipelines/` | platform team | Azure Pipelines for pull requests, the gate, main, and app releases |
| `src/berth/` | platform team | the renderer, review logic and CLI |

## Documentation

- [Design](docs/design.md) explains why berth works this way, starting from what the problem needs, and what it chose not to do.
- [Research](docs/research.md) summarises how Google, Meta, Uber, Netflix, Stripe and others solve the same problem, with sources.
- [How-to guides](docs/how-to.md) cover the everyday tasks: adding an app, releasing, promoting, secrets, the escape hatch, emergencies, and setting up the gate in Azure DevOps.
- [Reference](docs/reference.md) lists every spec field, the review tiers and escalations, the rendered objects, and the CLI.

## Developing berth

```sh
uv run pre-commit run --all-files
uv run pytest
scripts/validate-rendered.sh
```

`scripts/validate-rendered.sh` downloads checksum-verified kubeconform v0.8.0 and Kyverno CLI v1.19.2 into `.tools/`, then validates everything under `rendered/` and runs the policy tests in `platform/admission/tests/`. `uv run pytest -m integration` runs the same script from the test suite.
