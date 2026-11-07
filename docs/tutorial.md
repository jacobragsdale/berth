# Tutorial: add your first app

In this tutorial, we will add an app called `quote-board` to the deploy repository, see exactly what berth would deploy for it, and find out who would have to approve three different changes to it. Everything happens on your machine. Nothing is pushed, and no cluster is touched.

You act as Nina Berg (`nina.berg@contoso.com`), a developer who is starting a new service with her colleague Kai Müller (`kai.muller@contoso.com`). It takes about fifteen minutes.

Before you start, you need:

- [uv](https://docs.astral.sh/uv/) and git, with your name and email set for git (`git config --global user.name` and `user.email`), because one step commits;
- a fresh clone of this repository, with no local changes.

## Get ready

In the clone, install berth and check that the repository is valid:

```sh
uv sync --locked
uv run berth check
```

You should see:

```text
ok: 7 apps, 20 app environments
```

The numbers count the apps already in the repository and the environments they run in. If your clone has more apps, the numbers in this tutorial's output will be larger. Everything else should match.

Work on a branch, so that you can throw everything away at the end:

```sh
git switch -c tutorial
```

## Create the app

An app is one container image and everything that runs from it. Create `quote-board` with two owners and the version of its image that should run in dev:

```sh
uv run berth new quote-board --description "Shows live FX quotes on the office screens" \
  --owner nina.berg@contoso.com --owner kai.muller@contoso.com \
  --source https://dev.azure.com/contoso/trading/_git/quote-board \
  --version 0.1.0@sha256:bbfadb5f057fab4e27d7d70df4144472a347c3ff250fb24683abe858d6be820e
```

You should see:

```text
created apps/quote-board/app.yaml
created apps/quote-board/dev.yaml
```

The version is a tag for people and a digest for the machine. In real use the digest comes from the image you pushed to ACR. Here it is a placeholder, because nothing will be pulled.

Look at the spec berth wrote:

```sh
cat apps/quote-board/app.yaml
```

```yaml
# yaml-language-server: $schema=../../platform/schema/app.schema.json
name: quote-board
description: Shows live FX quotes on the office screens
owners:
  - nina.berg@contoso.com
  - kai.muller@contoso.com
source: https://dev.azure.com/contoso/trading/_git/quote-board
services:
  api:
    port: 8080
    health:
      path: /healthz
    size: s
```

Notice how short it is. It says what only the owners know: who owns the app, what it serves on, how to check it is healthy, and a size from the platform's menu. `dev.yaml` holds just the version. The first line points editors at the schema, so if you open the file in VS Code with the YAML extension, it completes and checks every field.

## Render it

Turn the spec into what the cluster will receive:

```sh
uv run berth render
git status --short
```

You should see:

```text
rendered 36 files (1 changed)
?? apps/quote-board/
?? rendered/k8s/aks-qa/quote-board-dev/
```

berth wrote the app's Kubernetes manifests in `rendered/k8s/aks-qa/quote-board-dev/`. That file is exactly what `berth deploy` will apply to the qa cluster after the merge. Now check the whole repository again:

```sh
uv run berth check
```

```text
ok: 8 apps, 21 app environments
```

## See what you will get

Ask berth to explain the app in dev:

```sh
uv run berth explain quote-board --env dev
```

The output starts with where to find the app once it is deployed:

```text
quote-board in dev: Shows live FX quotes on the office screens
  owners     nina.berg@contoso.com, kai.muller@contoso.com
  namespace  quote-board-dev on aks-qa
  image      contosoacr.azurecr.io/quote-board:0.1.0@sha256:bbfadb5f057fab4e27d7d70df4144472a347c3ff250fb24683abe858d6be820e

Where to look
  pods and logs   kubectl --context aks-qa -n quote-board-dev get pods  (or Headlamp, filtered to the namespace)
  in step?        berth drift --cluster aks-qa
  datadog-logs (quote-board-api)  https://app.datadoghq.com/logs?query=service%3Aquote-board-api%20env%3Adev
  datadog-traces (quote-board-api)  https://app.datadoghq.com/apm/traces?query=service%3Aquote-board-api%20env%3Adev
```

Then every setting, with the file that set it:

```text
Settings (merged; the last column is the file that set each one)
  azure_identity            False                                                         default
  overrides                 -                                                             default
  secrets                   -                                                             default
  services.api.expose       internal                                                      default
  services.api.health.path  /healthz                                                      apps/quote-board/app.yaml
  services.api.port         8080                                                          apps/quote-board/app.yaml
  services.api.replicas     1                                                             platform default for dev
  services.api.size         s                                                             apps/quote-board/app.yaml
  services.api.writable     -                                                             default
  talks_to                  -                                                             default
  version                   0.1.0@sha256:bbfadb5f057fab4e27d7d70df4144472a347c3ff250f...  apps/quote-board/dev.yaml
```

And the objects berth renders:

```text
Kubernetes objects (rendered/k8s/aks-qa/quote-board-dev/manifests.yaml)
  Namespace quote-board-dev
  ServiceAccount quote-board
  RoleBinding berth-owners
  RoleBinding berth-writers
  Deployment quote-board-api
  Service api
  NetworkPolicy berth-ingress
```

The `Where to look` lines are for later, once the app is deployed; they need access to the cluster.

Notice that you never wrote `replicas`: the platform's default for dev filled it in. Notice also the `berth-owners` RoleBinding. It gives you and Kai read access to your own namespace, pods and logs included, with your own kubectl login. Nobody had to set that up by hand. `berth-writers` lets the break-glass groups (`kubectl_writers` in `platform/platform.yaml`) fix things in an emergency. The NetworkPolicy, the security settings, the probes, the proxy settings and the Datadog tags are in the manifest file if you want to look.

## Ask who must approve

The gate decides how much review a change needs. Ask it about this one, as Nina:

```sh
uv run berth review --base origin/main --author nina.berg@contoso.com
```

You should see:

```text
quote-board: owner review
  *    app          - -> added  owner  a new or retired app
  dev  environment  - -> added  auto   starting or stopping an app in an environment
gate: pending
  quote-board: needs approval from an owner other than the author (kai.muller@contoso.com)
```

A new app needs its other owner, but not the platform team, because it only starts in dev. Simulate Kai's approval:

```sh
uv run berth review --base origin/main --author nina.berg@contoso.com --approve kai.muller@contoso.com
```

The last two lines should read:

```text
gate: succeeded
  quote-board: owner review satisfied
```

## Make an everyday change

Pretend the app has merged, by committing it to your branch:

```sh
git add -A
git commit -qm "Add quote-board"
```

Now change a setting in dev only, render, and ask again. This time compare with your commit, `HEAD`, because that is where the app now exists:

```sh
printf 'config:\n  REFRESH_SECONDS: "5"\n' >> apps/quote-board/dev.yaml
uv run berth render
uv run berth review --base HEAD --author nina.berg@contoso.com
```

You should see:

```text
rendered 36 files (1 changed)
quote-board: auto review
  dev  config.REFRESH_SECONDS  - -> 5  auto  application settings
gate: succeeded
  quote-board: auto review satisfied
```

Nobody else has to approve. An owner changing a setting in dev is the owner's call, so the gate passes as soon as the checks do. Most changes to most apps look like this one.

## Make a risky change

Undo that change, then ask for a much bigger pod:

```sh
git restore apps rendered
sed -i 's/    size: s$/    size: xl/' apps/quote-board/app.yaml
uv run berth render
uv run berth review --base HEAD --author nina.berg@contoso.com
```

On macOS, use `sed -i ''` instead of `sed -i`. You should see:

```text
rendered 36 files (1 changed)
quote-board: platform review
  dev  services.api.size  s -> xl  platform  size xl needs platform review
gate: pending
  quote-board: needs approval from an owner other than the author (kai.muller@contoso.com) and the platform team (infra.lead@contoso.com, platform.oncall@contoso.com)
```

It is the same field as `size: s` was, but the value decides the tier. A size the platform marks as expensive brings the platform team back in.

## Ship to production

Undo the size change, and deploy the app to prod by giving it a prod version:

```sh
git restore apps rendered
printf 'version: 0.1.0@sha256:bbfadb5f057fab4e27d7d70df4144472a347c3ff250fb24683abe858d6be820e\n' > apps/quote-board/prod.yaml
uv run berth render
uv run berth review --base HEAD --author nina.berg@contoso.com
```

You should see:

```text
rendered 37 files (1 changed)
quote-board: platform review
  prod  environment  - -> added  platform  starting or stopping an app in an environment
gate: pending
  quote-board: needs approval from an owner other than the author (kai.muller@contoso.com) and the platform team (infra.lead@contoso.com, platform.oncall@contoso.com)
```

A first deploy to production needs the platform team. Most later prod changes, such as a new version that has already run in qa, need only Kai.

## Clean up

Throw the tutorial away and return to `main`:

```sh
git clean -fdq apps rendered
git restore apps rendered
git switch main
git branch -D tutorial
```

`uv run berth check` should print `ok: 7 apps, 20 app environments` again.

## Next steps

You have added an app, seen everything berth would deploy for it, and watched the review tier move from no one, to the other owner, to the platform team, as the change got riskier. To add a real app, with a digest from ACR and a pull request, follow [How to add an app](how-to.md#how-to-add-an-app). Once it is deployed, [How to see your app running](how-to.md#how-to-see-your-app-running) shows where to look. To understand why the tiers are drawn where they are, read [Review effort follows risk](design.md#review-effort-follows-risk).
