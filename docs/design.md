# Design: one spec per app, review by risk, deploys you can see

berth replaces "one person reviews every deployment change" with three parts. Each app has a small spec that only says what its owners know. A renderer turns that spec into everything the platform knows. Rules decide how much review each change needs. The platform team keeps central control by owning the spec format, the renderer, the rules and the admission policies, rather than by reading 400 apps' diffs.

Delivery is deliberately plain. After each merge a pipeline renders `main` and applies it, one app namespace at a time, and a daily report says where the clusters differ from git. There is no controller in the cluster, and no tool to learn beyond kubectl and Python.

## The problem is review attention

The setup this replaces keeps every Deployment, ConfigMap, git-crypt secret, resource request and mount for about 400 deployments in one repository. That repository is a mix of Helm charts, plain YAML and bash scripts, and the infra lead approves every change. Changes are applied with kubectl from someone's laptop. No pipeline runs, so nothing proves that what runs matches the repository, and nothing keeps image tags in step across environments.

Other teams own some of the surroundings:

- Clusters and node pools belong to a separate cloud engineering team that works in Terraform. Node pools already autoscale.
- Apps send OpenTelemetry to Datadog.
- Airflow is run by the same team as the repository; Control-M is run by another team.
- Every batch job, on either scheduler, runs in a container declared in the repository.
- Other systems on the company network call some services by URL, under a subdomain scheme the company already has.
- Outbound traffic goes through a corporate proxy.

Most of the review load is changes that a platform reviewer cannot judge anyway. An image tag bump, a timeout value or a log level is the owning developer's call, and the infra lead can only check that it parses. The changes that need infra judgment are rare: a new mount, an identity that reaches Azure resources, a 4 GiB pod, an override of the pod spec, an app's first deploy to production. A single queue spends the same attention on both kinds, so it is slow for the common case and not especially careful for the rare one. Meta's configuration paper calls this arrangement the "conservative extreme": "a central committee for approval" ([Tang et al., SOSP 2015](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf)).

## What the system has to do

Working back from that problem, any replacement has to keep these properties:

1. Everything running in a cluster traces back to a reviewed commit, and anything that does not is found within a day.
2. Owners state intent and the platform decides mechanism. The smaller the interface, the less there is to review and the less there is to misuse.
3. Review effort follows risk, decided by written rules rather than by one person reading everything.
4. Guardrails sit in more than one layer: what the spec cannot express, what CI rejects, and what the API server refuses.
5. Ownership attaches to the thing that deploys, and developers are trusted with as much as that allows.
6. Promotion moves the same artifact forward, and production only receives what already ran in qa.
7. One app's mistake can neither block nor break another app.
8. A fleet-wide change is cheap to make and its blast radius is visible before it merges.
9. Nothing that holds data is deleted because a file went missing, and a deploy never deletes much at once without a person.
10. An emergency has a fast path that is still recorded and reconciled afterwards.
11. Day-to-day changes, including a new app, never wait on cloud engineering. Whatever does need them is a one-time setup.
12. A new developer can learn what an app is, and what a change to it will do, from the repository alone.
13. As few third-party moving parts as possible. Where the team can own something in Python, it does.

## The spec is the interface

In berth, an app is one container image and everything that runs from it: its services, its scheduled workflows, and the configuration and secrets they share. An app has one version per environment, so promotion is a single value. A repository that builds two images becomes two apps. Third-party software that only runs a container, such as Redis or a vendor's web app, is an app like any other. Its image is copied into ACR, and someone ports its Helm chart into a spec once. That rule keeps the unit of deployment, the unit of promotion and the unit of ownership the same thing.

The spec holds only what an owner knows:

- the version, and a size from the platform's menu;
- settings and secret names;
- schedules;
- the other services the app calls, and the outside systems it calls;
- a hostname, if company callers reach it.

Everything else is a platform decision applied by the renderer: security contexts, probes, labels, network policy, disruption budgets, zone spread, the proxy settings, telemetry tags, RBAC for the schedulers and for people, namespaces, and how secrets get from Key Vault into a pod.

Large companies converged on this split. Meta's scheduler team owns a `create_job` module and a validator, and other teams call it with a few arguments ([Tang et al.](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf)). Google's production intent is expanded by "integrators" that offer "opinionated service models" ([Prodspec and Annealing](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). Netflix's Keel injects central teams' opinions through "resolvers" at render time ([Keel](https://github.com/spinnaker/spinnaker/tree/main/keel)).

The spec is plain YAML with a JSON Schema, not a configuration language. Google's own retrospective on Borg says configuration languages drift towards Turing-completeness, and advises keeping data as data and computation in a real language ([Borg, Omega, and Kubernetes](https://queue.acm.org/detail.cfm?id=2898444)). Here the computation is about 7,000 lines of typed Python, with the CLI, the gate and the deploy tooling included. The data is a file an owner can read in a minute. Unknown fields are errors, so a typo cannot be silently ignored, and the schema gives editors completion and inline checks.

Some needs will always fall outside the spec. For those, `overrides` lets an app patch a rendered object directly. Every override needs platform review and a written reason. Meta found that per-task customisation in Twine was "abused" and is removing it ([Twine, OSDI 2020](https://www.usenix.org/conference/osdi20/presentation/tang)). An escape hatch that is expensive to use and easy to count turns those needs into a backlog. When several apps override the same field, it belongs in the spec. Porting third-party charts will find these gaps early. `writable` (scratch directories for images that expect a writable root filesystem) is the first one.

## Ownership is per app, not per team

Each `app.yaml` lists at least two owners by identity. They approve the app's changes, and their names go on the app's namespace so that whoever is on call can find them. There is no team object and no team directory, so people can move between teams without anything in the repository changing. Google's OWNERS files work the same way: they sit in each directory, list individuals, and need no central registration ([Software Engineering at Google, ch. 9](https://abseil.io/resources/swe-book/html/ch09.html)).

The research did not find this model elsewhere: Backstage, Monzo, Spotify and Zalando all assign ownership to teams. The risk is the bus factor. When someone leaves, the apps they own lose an approver. Requiring two owners covers the common case. A weekly check that every owner is still an active account, posted to Slack, would cover the rest; it is not built yet.

Ownership is data in the repository, so changing it is an ordinary owner-tier change. It is judged by the owners on the target branch, so nobody can add themselves as an owner and then approve their own change. Ownership is also access. Each app namespace gets a `berth-owners` RoleBinding to Kubernetes' built-in `view` role, so owners can read their pods, logs and events with their own kubectl or Headlamp login, but not Secrets.

Each app gets its own namespace in each environment, named `<app>-<env>`. Airbnb uses the same `svc-env` pattern ([InfoQ talk](https://www.infoq.com/presentations/airbnb-kubernetes-services/)). In Kubernetes the namespace is where RBAC, quotas, network policy and pod security apply. With 400 deployments in three shared namespaces there is no way to give an app's owners read access to only their own pods, or to stop one app's traffic reaching another. The cost is about 1,100 namespaces across the two clusters, and DNS names change for calls between apps. `talks_to` hides the DNS change. An app that declares it calls `ledger/api` gets `LEDGER_API_HOST`, `LEDGER_API_PORT` and `LEDGER_API_URL`, and ledger's network policy admits it.

## Review effort follows risk

`platform/review.yaml` maps each kind of change to one of three tiers:

- An `auto` change merges once checks pass, provided an owner of the app opened it.
- An `owner` change needs approval from another owner.
- A `platform` change needs approval from both an owner and a platform approver.

The table leans towards developers. A version, a setting, a size, a schedule or a company hostname is `auto` in dev and qa and `owner` in prod. A new app is `owner`, and its first deploy to prod is `platform`. Persistent storage, Azure access and overrides are `platform` everywhere.

Calling another app is a change to that app as well. Adding `ledger/api` to `talks_to` makes ledger's network policy admit the caller, so the gate records a second change, `callers.<caller>`, on ledger, and ledger's owners approve it. Who may reach an app is that app's owners' call. Without this, two owners of one app could open a path into any other app's production namespace. Removing a caller only narrows access, so it needs nobody.

Three checks look at the new value as well as the field, and raise a change to `platform`:

- a size the platform menu flags;
- more than ten replicas;
- a version that never ran in the environment this one promotes from (dev for qa, qa for prod).

Production never drops below `owner`, whatever the table says. A path no rule covers needs the platform team, so an unforeseen field fails safe.

Writing rules down, instead of asking a person to judge each change, is what the larger companies do. Google's release engineering chapter says to "fit the deployment process to the risk profile of a given service" ([SRE book, ch. 8](https://sre.google/sre-book/release-engineering/)). Prodspec keeps manual approval for "particularly critical changes" so that approvers do not become desensitised ([Prodspec](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). berth uses a short table rather than a model, for three reasons. The table is auditable, people can argue about a line of it in a pull request, and a 30-person organisation does not have the incident history a model would need. The production floor exists for separation of duties: someone other than the author approves every production change.

The gate compares meaning, not text. It merges each environment file over `app.yaml` on both sides of the change and diffs the results path by path. Editing a base setting in `app.yaml` therefore shows as a change in every environment it reaches, including prod. When the platform team changes the renderer or a default, the affected apps' output changes without any change to their specs. The gate lists those apps so that their owners can watch, but does not ask them to approve. That is the same idea as Meta's AutoCanary, where a shared change runs the checks of every service that consumes it ([AutoCanary](https://atscaleconference.com/?p=76100)).

Freezes delay production changes rather than reject them. A pull request labelled `break-glass` skips the freeze and the tiers, and needs one approval from someone other than its author for each part it changes: for each app, one of that app's owners or a platform approver; for platform files, a platform approver. One person is enough for the usual single-app fix. Counting per part stops two owners of one app from also changing another app, or the review rules, under the label. Azure Repos has no CODEOWNERS file, and its path-filtered reviewers cannot tell a replica change from a hostname change inside the same file. So the gate is code that posts one required status, `berth/review`.

## Who judges a pull request

A merge gate is worthless if the change it judges can rewrite it. Azure Repos runs a build-validation pipeline from the pull request's own merge commit. That means a pull request can edit `pipelines/pr.yml`, or `src/berth/review.py`, and post itself a passing status. berth therefore separates the checks from the gate.

`pipelines/pr.yml` is the pull request's own build, and it decides nothing on its own. `pipelines/gate.yml` is triggered by a webhook, and webhook-triggered pipelines always run the default branch's YAML. It checks out main's berth and runs it against the pull request's tree, using main's `review.yaml`, main's approvers and main's owners. It also checks with main's berth that the specs validate and render. The gate posts as a dedicated identity, and the branch policy accepts `berth/review` only from that identity.

The gate never compares rendered files, because none are committed (see [Rendered output is published, not committed](#rendered-output-is-published-not-committed)). That matters for changes to berth itself. When the gate compared committed output with what main's renderer produced, a pull request that changed the renderer could never pass: its output came from the new code, and the gate rendered with the old. Now a renderer change is judged as what it is, code, by a platform approver. The gate notes that its list of affected apps comes from main's renderer, and the pull request's own build attaches what the new renderer produces. Main's berth also reads the specs, so a change that adds a spec field must merge before any app uses it.

## Deploying: kubectl, with git as the record

The main-branch pipeline runs `berth deploy` for the qa cluster, then the prod cluster, then Control-M. berth talks to each API server through `kubectl proxy`, on a private Unix socket. kubectl handles the kubeconfig and the Entra sign-in; berth calls the REST API itself, so the answers are structured. A conflict comes back as the field and the manager that owns it, not as text to search for, and berth can make many requests at once.

Every apply is server-side apply with the field manager `berth`. The API server then records which fields berth set, and that record is what makes the rest of the design work:

- **A change made by hand is visible.** Kubernetes notes the manager of every field. A field set by `kubectl-edit`, `kubectl-set` or `kubectl-scale` belongs to someone other than berth or Kubernetes' own controllers. `berth drift` reports each one with the exact path, such as `Deployment/ledger-api: kubectl-set: .spec.template.spec.containers[name=main].env[name=LOG_LEVEL]`.
- **A deploy never silently undoes one.** If git sets a field that someone changed by hand, the apply stops with a conflict naming the field, and only that app environment's deploy fails. Fields someone added are kept and reported.
- **Putting git back is explicit.** `berth deploy --revert` takes the conflicting fields back, and removes added fields with a patch built from the same record.
- **What git dropped is deleted, except what holds data.** Every object berth makes is labelled. A deploy deletes the objects git no longer renders in the namespaces it deploys, so a removed CronJob stops firing and a service made internal loses its HTTPRoute in the same run. An app environment whose file is gone has its workloads stopped the same way. Namespaces, volume claims and anything marked `berth.contoso.io/keep` are never deleted: a person deletes those once nobody needs the data. `--no-prune`, a parameter of the deploy pipeline, holds every deletion for a run.

  Google's Prodspec paper names partial output as the most common configuration failure, and warns against deleting things because they are absent ([Prodspec](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). That warning is why data-bearing kinds are never pruned. It is also why pruning the rest is safe here: berth renders all or nothing. A spec that fails to load or render fails `berth check` and stops the deploy before anything is applied, so a missing file cannot quietly empty a namespace. What remains is a renderer change that drops a kind for every app. A brake catches that: a run that would delete more than `deploys.prune_limit` of the cluster's app objects (by default 20, or 5% if that is more) deletes nothing, applies nothing, and fails as the platform would, naming the kinds it would have deleted. If git really dropped them, `--prune-all` goes past the brake.

  Settings are retired on a schedule instead: each change renders a new ConfigMap named by its content hash, and a deploy deletes the old one a week after it stopped being rendered, once no workload names it, so a job that started on the old settings still finds them. An autoscaler git dropped is deleted before the Deployment is applied rather than after, so that between the two it cannot override the count git now sets.

Each app namespace converges on its own, so one app cannot hold up another:

- **Only what differs is applied.** After a namespace deploys in full, rollouts included, berth stamps it with a digest of its manifests, and with the commit and time. The next deploy applies only namespaces whose digest differs from what `main` renders. A namespace that failed keeps its old stamp, so the next run tries it again, whatever triggered that run. There is no "changed since the last deploy" bookkeeping to fall behind.
- **A failure stops at its own app.** Environments go in promotion order. If an app fails in dev, its qa environment is held; if it fails or is held in qa, the prod cluster's deploy holds its prod environment, from the qa stage's results. "Ran in qa" means qa runs it: an environment skipped for break-glass holds the next one too, because it may be running something else, and an environment deployed on its own is held unless the one it promotes from is stamped with what git renders for it. A skip because a newer commit is already deployed holds nothing. Every other app carries on, and the stage shows "succeeded with issues" instead of stopping the run. Only a failure in the platform itself (cluster software, shared objects, admission policies) stops the run, because every app depends on it.
- **Namespaces deploy in parallel**, eight at a time by default, rollout waits included. With about 1,100 namespaces, a change that touches every app would otherwise take hours.
- **Deploys never overlap, and never go backwards.** The pipeline batches merges and holds an exclusive lock on each cluster, with a newer run replacing one still queued. berth also refuses to apply over a namespace stamped with a newer commit, in case a deploy runs from an older checkout.

This is a push model, not GitOps in the strict sense. The [OpenGitOps principles](https://opengitops.dev/) also ask that an agent pull and continuously reconcile, and berth does neither. That was a choice against Argo CD and Flux. Both would correct drift continuously, but each is another system in each cluster to run, secure and learn. Argo CD would also bring a second permission model. Server-side apply with a field manager gives the same visibility. The daily report replaces continuous correction with a person who decides. Break-glass becomes natural, because nothing undoes an emergency fix behind someone's back. The cost is a window, up to a day, in which an unmarked manual change goes unnoticed. If that window ever matters, Argo CD can be added with sync permission turned off, as a viewer and drift detector, without changing how deploys work.

## Drift is reported every morning

`pipelines/drift.yml` runs `berth drift` against both clusters every day at 13:00 UTC and posts to Slack. It posts even when everything is in sync, so a missing message means the job broke. It reads each cluster with one list per kind of object, whatever the number of namespaces. The managed-fields record already says what people changed, so only namespaces that look different get a server-side diff of every object. For every app namespace it reports:

- changes git renders that no deploy has applied yet, for example because one failed;
- objects that differ from git, or that git renders and the cluster lacks;
- fields set by hand, with who set them;
- objects nobody rendered, such as a ConfigMap made during an incident;
- objects git no longer has;
- namespaces on the cluster whose app has been removed;
- break-glass marks, with their reason and whether they have expired.

The platform team acts on the report. That means adopting a change into the spec through a pull request, reverting it with `berth deploy --revert`, or asking whoever made it.

## Break-glass

Anyone may break glass, but nobody holds the access by default in production. Each environment names the Entra groups whose members get the `berth-break-glass` role in its app namespaces. In dev and qa that can be every developer. In production it is a group managed by Entra Privileged Identity Management: developers are eligible, not members, and joining means asking, with a reason, for a few hours. The request, and any approval, is logged.

The role covers what an emergency needs and no more: scale, restart and change workloads, change settings and routes, run jobs, delete a stuck pod. It cannot read Secrets, open a shell in a container, or port-forward, because each of those reveals the app's secrets. It cannot change RBAC, the Namespace itself, or anything outside app namespaces, so an emergency edit can never touch cluster software or Kubernetes itself. Before this, the break-glass groups had Kubernetes' `edit` role permanently, which includes reading every Secret: every developer could read every production database password at any time.

`berth breakglass <app> --env <env> --reason "..."` marks the namespace with who, why and until when. The mark is a ConfigMap in the namespace, which the role may write, rather than an annotation on the Namespace, which it may not. Deploys then skip that namespace, so a merge of something unrelated cannot undo the fix. The daily report shows the mark and flags it once it expires. The fix then goes into the spec through a pull request (labelled `break-glass` if it must skip the freeze and tiers), or is reverted. Ending the mark returns the namespace to normal deploys.

## Guardrails at the API server

Every rule the renderer follows is also enforced by the API server, for whatever reaches the cluster without the renderer: a break-glass edit, or a pod that Airflow or Control-M starts. berth uses Kubernetes' built-in ValidatingAdmissionPolicy, stable since 1.30. Nothing is installed to enforce it, and an outage of some webhook cannot block deploys. The policies are rendered from `platform.yaml`:

- images come from the company registry, pinned by digest;
- every container has CPU and memory requests and a memory limit;
- an app reads secrets only from its own environment's store, only under its own `<app>--` prefix, and never with `dataFrom`;
- a route's hostnames are under its own environment's domain, and attach only to the cluster's Gateway;
- every app namespace names at least two owners.

Each namespace also enforces Kubernetes' `restricted` pod security standard. The integration tests submit violating objects to a real API server and check that each policy refuses them by name.

## Company callers reach services through one Gateway per cluster

A service with `expose: company` and a `hostname` gets an `HTTPRoute` at `<hostname>.<domain>`, where the environment sets the domain, for example `quotes.dev.apps.contoso.com`. Each cluster has one Gateway behind a private Azure load balancer on a reserved IP. Each environment's wildcard DNS record points at that IP, and each has a wildcard certificate. Once those exist, a new hostname needs no DNS or certificate request. The Gateway has one HTTPS listener per environment, and each listener admits routes only from that environment's namespaces, so a dev app cannot claim a qa hostname. Gateway API is the successor to Ingress. Microsoft retires the managed NGINX in AKS's app routing add-on on 30 November 2026 ([retirement notice](https://mc.merill.net/message/AZ555839)). Envoy Gateway implements it here, installed by the platform team without changing the cluster's Azure configuration.

Callers already use URLs under the company's subdomain scheme. So the move can go one hostname at a time: an explicit DNS record for a moved name overrides the wildcard, and no caller changes anything.

## Seeing what runs

There is no UI to install. Developers look in three places:

- **kubectl and Headlamp,** with their own Entra sign-in: owners have `view` on their app's namespaces, so pods, logs, events, mounts and network policies are all readable. Headlamp's desktop app gives the same view by clicking, with nothing in the cluster. The Kubernetes Dashboard was archived in January 2026, and Kubernetes SIG UI points to Headlamp in its place ([Kubernetes blog](https://kubernetes.io/blog/2026/06/01/dashboard-to-headlamp/)).
- **Datadog** keeps the history. berth sets Datadog's unified service tags (`tags.datadoghq.com/env`, `service` and `version`) on every pod. It also sets `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` on every container, which Datadog maps to the same tags ([semantic mapping](https://docs.datadoghq.com/opentelemetry/mapping/semantic_mapping/)). The version comes from the environment file, so Datadog shows exactly which release each pod runs.
- **`berth explain <app> --env <env>`** prints every merged setting with the file it came from, the objects berth renders, the app's URLs, and links to Datadog.

A status page built on the team's React library, reading the drift report's output, is the natural next step if people want deploy status in a browser.

## Outbound traffic

Every container gets `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`, in upper and lower case, from the environment's `proxy` in `platform.yaml`. The common bug is a `NO_PROXY` that misses the cluster's own addresses, so that calls between apps go out through the proxy. berth always adds the cluster's names (`.svc`, `.cluster.local`), its pod and service ranges, and the environment's own domain. `calls` lists the outside systems an app depends on, such as a database host. That records the dependency where it can be read and searched; it has no effect on traffic yet.

Databases are not run in the clusters. A database that holds data people need is managed by Azure or by the DBA team, so backups, restores and upgrades are theirs. An app reaches it with a host in `config` and a password in Key Vault.

## Secrets leave git

git-crypt shares one key across everyone who can decrypt. It gives no per-app access, every offboarding means rotating every secret, and an encrypted diff cannot be reviewed. It also cannot coexist with committed rendered output, because the manifests would have to carry decrypted values.

In berth a spec lists secret names only. Values live in one Key Vault per environment under the key `<app>--<name>`. External Secrets Operator keeps a Kubernetes Secret in sync from each app's `ExternalSecret`, and each environment's store admits only namespaces of that environment. Developers and the platform team write values, in every environment including prod. Key Vault keeps every version of a secret, and its audit log shows who wrote what. A daily check that flags prod writes by someone who is not an owner of that app would fit in the drift report; it is not built yet.

External Secrets Operator is the one third-party controller berth depends on for apps. The alternatives were worse here. The AKS Key Vault CSI driver creates a Kubernetes Secret only while a pod mounts it ([CSI options](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options)), which does not work for jobs that read a secret as an environment variable before they start. One workload identity per app would put cloud engineering back in the path of every app. A sync job written in Python is possible, but a bug in it could hand one app another's database password.

## Platform software

Cluster software, meaning anything with CRDs, controllers or webhooks, is declared in `platform/software/<name>.yaml` as a pinned Helm chart. There are two: External Secrets Operator and Envoy Gateway. Helm never installs anything. `berth deploy` renders each chart with `helm template` and applies it with kubectl like everything else, before the platform objects and the apps. Chart output is not committed: the CRDs alone are megabytes nobody reviews. Instead, `scripts/validate-software.sh` renders and schema-checks every chart, and the pull request pipeline attaches the rendered diff of a chart change. Values can use placeholders such as `${registry}` and `${tenant_id}`, so a value is written once, in `platform.yaml`.

## What needs cloud engineering

Nothing per app. A new app, a new environment file, a new secret name, a company hostname and a new workflow are all changes in this repository. What does need cloud engineering is a one-time list of Azure objects that Terraform should own:

- workload identity and the OIDC issuer enabled on both clusters;
- one managed identity per cluster, with a federated credential, for External Secrets Operator;
- the Key Vaults (one per environment, one platform vault per cluster), and write access to them for developers;
- a reserved private IP per cluster for the Gateway, and the wildcard DNS records pointing at it;
- the wildcard certificates, and a proxy exception for the company domain;
- AKS-managed Entra ID with Kubernetes RBAC, so that berth's RoleBindings decide who can read and write. With Azure RBAC, each grant would be an Azure role assignment.

Node pools and per-app Azure identities (`azure_identity`) stay with cloud engineering by design.

## Jobs are workloads too

A workflow in the spec is a set of tasks with dependencies, a schedule, a time zone, and an `orchestrator`: `airflow`, `controlm`, or `kubernetes` for a single-task CronJob. Every task pod comes from the same builder as a service pod. A job therefore gets the same image, security context, size, settings, secrets, proxy and telemetry tags whether a Deployment, Airflow or Control-M starts it.

For Airflow, berth writes one plain DAG file per workflow. Each task is a KubernetesPodOperator that carries its pod template inline, and Airflow reads `airflow/<env>/` from the `rendered` branch as a Git DAG bundle. The pods run in the app's own namespace, under a RoleBinding that lets Airflow's worker start pods there and nowhere else.

For Control-M, berth writes Automation API JSON: one folder per workflow, one `Job:Kubernetes` per task carrying a Kubernetes Job manifest, and flows for the dependencies. Kubernetes owns retries and the deadline, so Control-M only starts and watches the job. berth translates the cron expressions Control-M can express and rejects the rest with a message. How the definitions reach Control-M depends on its team: the deploy pipeline can run `ctm deploy` with a token they issue, or publish the files for them to import.

Because the orchestrator is one field, moving a job between Control-M and Airflow is a one-line change. Stripe used the same move when it migrated cron jobs to Kubernetes: one YAML source generated both the old and the new scheduler's configuration ([Stripe](https://stripe.com/blog/operating-kubernetes)).

## Rendered output is published, not committed

Pull requests change specs only. A reviewer still sees exactly what will reach Kubernetes and the schedulers: `berth diff` renders both sides, and the pull request build attaches the diff. For a change to berth itself, that build runs the new code, so the diff shows its blast radius across every affected app.

The rendered output used to be committed beside the specs. That had three costs, which grew with the fleet. Every pull request had to re-render, and any platform change made every open pull request stale. The gate, which must run main's code, could not pass a change to the renderer. And the committed tree said what main renders, which is not always what runs: after a failed deploy, the schedulers would read files that referred to objects the cluster did not have.

Instead, after each cluster's deploy, `berth publish` commits that cluster's part of the rendered tree to the `rendered` branch. Only the pipeline can push to it. An app environment's files change there only once it runs what they describe; one that failed or was held keeps its previous files. Airflow reads its DAGs from that branch, and the Control-M stage deploys from it, so a scheduler never starts a pod whose namespace lacks its settings. The branch's history is also the record of what each cluster ran, commit by commit. Two runs can publish at once, one for each cluster; the push is never forced, so the one refused reads the branch again and puts its cluster's part on top. Environments are directories, not branches, so promotion is still copying one version line from `qa.yaml` to `prod.yaml`.

## Every change is checked the same way

The aim is that a reviewer never has to check shape, only intent:

| Check | Catches | Tool |
|---|---|---|
| Spec schema | unknown fields, wrong types, bad names, an unparseable cron, a version without a digest | Pydantic models, also exported as JSON Schema for editors |
| Cross-file rules | sizes not on the menu, `talks_to` a service that does not exist, two services with one hostname, a schedule Control-M cannot express, software in an app namespace, a Gateway without its certificate, an override of an object that is not rendered, a name too long for Kubernetes once berth joins it to the app's, a setting named like a variable berth sets, pipelines that list other clusters than `platform.yaml` | `berth check` |
| Rendered effect | what the change does to every object and DAG, including a change to berth itself | `berth diff`, attached to the pull request build |
| Kubernetes schemas | invalid objects, for the clusters' version and the next one | kubeconform |
| Admission and deploys | what the API server refuses, including scheduler pods; drift detection; revert; prune | a real kube-apiserver, in `pytest -m integration` |
| Platform software | chart values a chart rejects, objects that fail schemas | `helm template` and kubeconform |
| Images | a digest that is not in ACR, or a tag that does not name that digest | `berth images` and `az acr` |
| Code | lint, format, types, tests | Ruff, ty, pytest, pre-commit |

Then the gate decides who must approve, and the API server enforces the policies again.

## Onboarding

The tutorial ([Add your first app](tutorial.md)) runs entirely on a laptop. It creates an app, shows what berth would deploy for it, and shows the review tier change as the change gets riskier. After that:

- **`berth explain`** answers "what is this app and where did this value come from".
- **Every spec file starts with a `yaml-language-server` schema line,** so editors complete field names and flag mistakes as you type.
- **Error messages say what to do,** such as "unknown size 'xxxl' (sizes: xs, s, m, l, xl, xxl)".

## Failure is contained

A bad version stalls instead of breaking. Deployments roll with `maxUnavailable: 0` behind readiness probes, so old pods keep serving until new ones are ready, and the deploy waits for each rollout and fails that app environment if it does not finish. The app's next environment is then held, so the change goes no further, while every other app deploys as usual. In production, services spread across zones, and those with two or more pods get a disruption budget. berth does not roll back automatically: with a few pods per service a statistical canary is mostly noise. Rolling back is reverting the commit.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the central repository, split review with CODEOWNERS per team directory | It rests on teams; Azure Repos has no CODEOWNERS; path rules cannot see which field changed. |
| Manifests in each app's own repository | Removes the bottleneck, but also the consistency and the fleet-wide changes that made central control worth having. |
| A shared Helm chart with a values file per app | The closest standard alternative. Helm templates are text templating, so they validate nothing until rendered. berth keeps the values-file idea and replaces the templates with typed code. |
| Argo CD or Flux | See [Deploying](#deploying-kubectl-with-git-as-the-record). |
| Kyverno | Its extras (mutating and generating objects) duplicate what the renderer already does, and as a webhook it would sit in the path of every deploy. |
| Vertical Pod Autoscaler | The rightsizing bot needs nothing in the cluster, and keeps git as the record of what each app requests. |
| First-class dependency kinds (a `postgres:` block) | Third-party software is an ordinary app instead, so there is one kind of thing to learn and review. |

## Moving from the current repository

Today's starting point is kubectl from laptops, so the first goal is knowing what is actually running. Each step below is useful on its own:

1. **CI on the current repository.** Schema checks and a pipeline, before anything else changes.
2. **Secrets move to Key Vault.** It is useful on its own, and required before rendered output can be committed.
3. **New apps start on berth.** New deployments are most of today's queue, so this pays off before anything is migrated. `berth drift` immediately shows whether those apps stay as deployed.
4. **Existing apps move one at a time.** Porting a deployment to a spec finds the spec's gaps. Each one becomes an override first, and counting overrides measures them. The namespace move is the expensive part. A renderer option that places migrated apps in the old shared namespaces would let the spec, review and secrets changes land first; it is not built. The bash scripts need reading one by one: most do what a spec field now does.
5. **Laptops lose write access.** Once an app is on berth, people's access to its namespaces drops to `view`. Break-glass goes through each environment's break-glass groups, and in production through Entra PIM.

Before requiring the gate, run it in report-only mode: post the status without making it required, and compare its decisions with the infra lead's for a few weeks.

## Bots open pull requests; people decide

Two bots keep the repository current without anyone remembering to. Each one opens pull requests through the same gate as everyone else, as an identity that may change one kind of field.

- **Rightsizing.** Every Monday, the rightsizing bot reads each workload's CPU and memory from Datadog over two weeks, by the `service` and `env` tags berth puts on every pod. It turns them into a size from the menu: the 95th percentile of CPU and the peak of memory, each with a margin. That is the same idea as the Vertical Pod Autoscaler's recommender, but nothing runs in the cluster, and git stays the record of what each app requests. It never shrinks a workload that was killed for memory; it proposes one size up instead. It leaves CPU alone for services that autoscale on CPU. It opens one pull request per app and environment, with the evidence and an estimated saving. In dev and qa a size change merges once the checks pass; in prod an owner approves it. Node pools already autoscale, so smaller requests become fewer nodes. Google measured the same effect on its own fleet: jobs sized automatically left 23% slack, those sized by hand 46% ([Autopilot](https://research.google/pubs/autopilot-workload-autoscaling-at-google-scale/)).
- **Mirror.** Every day, the mirror bot checks each image in `platform/mirrors.yaml` for a newer upstream tag on its track, such as the newest Redis 8.2.x. It copies the tag into ACR, and opens a dev release for every app still on an older one. It is the release bot's job for software you do not build. Moving to a new minor version is a reviewed change to the track.

Both bots write a JSON plan first, and the pipeline opens the pull requests from it. The network calls (Datadog, the registries) and the repository edits never happen in the same step. Every bot, and the app release template, opens its pull request with `berth pr`, as its own identity. A bot that runs again updates its open pull request instead of opening another.

## Next to build

- **`berth adopt`,** which turns a live difference into the spec change that would make git match.
- **More in the drift report:** prod secret writes by non-owners, owners who are no longer active accounts, and certificates that expire within 30 days.

## What is unverified

Verified locally:

- rendering, review, the CLI and `berth explain`;
- schema validation of every rendered object, for Kubernetes 1.33 and 1.34;
- `berth deploy`, `drift`, `breakglass` and `--revert` against a real kube-apiserver and etcd (envtest 1.34), through `kubectl proxy`, including holds, parallel deploys, pruning and its brake, stopping a retired environment, retiring old ConfigMaps, and what the break-glass role allows, by impersonating a member of its group;
- `berth publish` against a local git remote;
- every admission policy refusing what it should, and admitting every pod Airflow and Control-M start in dev and qa;
- both platform software charts, rendered with their values and schema-checked;
- the mirror bot's tag listing against Docker Hub, GitHub, Microsoft and Quay registries, including Docker Hub's token flow and pagination;
- the generated DAGs in Airflow 3.2.2.

Not run anywhere real, and marked so in each file that talks to it:

- **Azure DevOps:**
  - the gate's webhook trigger and payload path, the pull request and status REST calls, restricting the status to one identity, and the release template;
  - the deploy and drift pipelines' service connections, `kubelogin` behind `kubectl proxy`, the exclusive lock with `lockBehavior: runLatest`, artifacts passed between stages, and pushing the `rendered` branch;
  - compile-time use of the variables in `pipelines/templates/targets.yml` (`${{ split(variables.clusters, ',') }}`);
  - `berth pr`'s REST calls, and a bot's token from `az account get-access-token`;
  - `az acr manifest show-metadata`.
- **AKS:**
  - rollouts actually completing, since the test API server has no nodes;
  - External Secrets Operator and Envoy Gateway running;
  - the Gateway's internal load balancer annotations, through Gateway API's `infrastructure` field;
  - owners as `User` subjects in RoleBindings, which assumes AKS-managed Entra ID uses the user principal name;
  - Entra PIM group membership reaching the cluster's tokens after `kubelogin remove-tokens`;
  - about 1,100 namespaces per cluster.
- **Datadog:**
  - the OTLP endpoint (berth assumes the Datadog Agent's receiver on each node at port 4318), and the link URL formats;
  - the rightsizing bot's queries: the v1 query API call, and the default metric names, which differ between Datadog's Kubernetes integrations.
- **ACR:** `az acr import` in the mirror pipeline.
- **Control-M:**
  - the `Job:Kubernetes` field names and flow syntax, from BMC documentation that could only be read in search excerpts;
  - the `ctm` CLI commands;
  - how Control-M starts containers today, and whether its team accepts definitions from this repository.
- **Slack:** the incoming webhook.
- **The proxy:** whether every tool in your images honours CIDR ranges in `NO_PROXY`.
