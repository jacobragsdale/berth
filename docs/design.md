# Design: one spec per app, review by risk

berth replaces "one person reviews every deployment change" with three parts. Each app has a small spec that only says what its owners know. A renderer turns that spec into everything the platform knows. Rules decide how much review each change needs. The platform team keeps central control by owning the spec format, the renderer, the rules and the admission policies, rather than by reading 400 apps' diffs. Delivery is GitOps: Flux pulls the rendered output from git and keeps the clusters matching it.

## The problem is review attention

The setup this replaces keeps every Deployment, ConfigMap, git-crypt secret, resource request and mount for about 400 deployments in one repository, and the infra lead approves every change. That repository already is declarative and versioned, so it has most of what GitOps asks for. What does not scale is the reviewer. Meta's configuration paper calls this arrangement the "conservative extreme": "a central committee for approval" ([Tang et al., SOSP 2015](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf)).

Most of the review load is changes that a platform reviewer cannot judge anyway. An image tag bump, a timeout value or a log level is the owning developer's call, and the infra lead can only check that it parses. The changes that need infra judgment are rare: a new mount, an identity that reaches Azure resources, a public hostname, a 4 GiB pod, an override of the pod spec. A single queue spends the same attention on both kinds, so it is slow for the common case and not especially careful for the rare one.

## What the system has to do

Working back from that problem, any replacement has to keep these properties:

1. Everything running in a cluster traces back to a reviewed commit, so audit, rollback and rebuilding a cluster all start from git.
2. Owners state intent and the platform decides mechanism. The smaller the interface, the less there is to review and the less there is to misuse.
3. Review effort follows risk, decided by written rules rather than by one person reading everything.
4. Guardrails sit in more than one layer: what the spec cannot express, what CI rejects, and what the API server refuses.
5. Ownership attaches to the thing that deploys.
6. Promotion moves the same artifact forward, and production only receives what already ran in qa.
7. One app's mistake can neither block nor break another app.
8. A fleet-wide change is cheap to make and its blast radius is visible before it merges.
9. Nothing that holds data is deleted because a file went missing.
10. An emergency has a fast path that is still audited.

The rest of this page is how berth meets each of these, and where it does not yet.

## Is this GitOps?

GitOps is a practice, and the question of whether berth uses it is fair. The [OpenGitOps v1.0 principles](https://opengitops.dev/) say a system's desired state is declarative, versioned and immutable, pulled automatically by software agents, and continuously reconciled. Those principles describe how desired state reaches the cluster. On who may change that state they say only that the store "should provide access control and auditing on the changes" ([glossary](https://github.com/open-gitops/documents/blob/main/GLOSSARY.md)).

So GitOps is the right delivery model, and berth uses it: Flux pulls `rendered/` from git and reconciles each cluster to it. But adopting Flux or Argo CD would not fix the bottleneck by itself, because the bottleneck is in the half GitOps leaves open: what people write, and who must approve it. berth adds that half, which is the spec, the renderer and the gate.

## The spec is the interface

In berth, an app is one container image and everything that runs from it: its services, its scheduled workflows, and the configuration and secrets they share. An app has one version per environment, so promotion is a single value. A repository that builds two images becomes two apps. That rule keeps the unit of deployment, the unit of promotion and the unit of ownership the same thing.

The spec holds only what an owner knows. That is the version, a size from the platform's menu, settings, secret names, schedules, and which other services the app calls. Everything else is a platform decision applied by the renderer: security contexts, probes, labels, network policy, disruption budgets, zone spread, RBAC for the schedulers, namespaces, and how secrets get from Key Vault into a pod. Large companies converged on this split. Meta's scheduler team owns a `create_job` module and a validator, and other teams call it with a few arguments ([Tang et al.](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf)). Google's production intent is expanded by "integrators" that offer "opinionated service models" ([Prodspec and Annealing](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). Netflix's Keel injects central teams' opinions through "resolvers" at render time ([Keel](https://github.com/spinnaker/spinnaker/tree/main/keel)).

The spec is plain YAML with a JSON Schema, not a configuration language. Google's own retrospective on Borg says configuration languages drift towards Turing-completeness, and advises keeping data as data and computation in a real language ([Borg, Omega, and Kubernetes](https://queue.acm.org/detail.cfm?id=2898444)). Here the computation is about 2,200 lines of typed Python, CLI and gate included, and the data is a file an owner can read in a minute. Unknown fields are errors, so a typo cannot be silently ignored, and the schema gives editors completion and inline checks.

Some needs will always fall outside the spec. For those, `overrides` lets an app patch a rendered object directly. Every override needs platform review and a written reason. Meta found that per-task customisation in Twine was "abused" and is removing it ([Twine, OSDI 2020](https://www.usenix.org/conference/osdi20/presentation/tang)). An escape hatch that is expensive to use and easy to count turns those needs into a backlog: when several apps override the same field, it belongs in the spec.

Every company the research found with a small service spec built it in-house: Airbnb's kube-gen, Stripe's cron YAML, Shopify's Services DB, Uber's Up and Netflix's Keel. The open-source options are either young (Score, Radius) or a whole control plane to run (KubeVela, Crossplane, Kratix); see [application-spec tools](research.md#application-spec-tools). The spec's field names follow Score's shape, so adopting Score later would not change much for owners.

## Ownership is per app, not per team

Each `app.yaml` lists at least two owners by identity. They approve the app's changes, and their names go on the app's namespace so that whoever is on call can find them. There is no team object and no team directory, so people can move between teams without anything in the repository changing. Google's OWNERS files work the same way: they sit in each directory, list individuals, and need no central registration ([Software Engineering at Google, ch. 9](https://abseil.io/resources/swe-book/html/ch09.html)).

The research did not find this model elsewhere: Backstage, Monzo, Spotify and Zalando all assign ownership to teams. The risk is the bus factor. When someone leaves, the apps they own lose an approver. Requiring two owners covers the common case. A check that every owner is still an active Entra user, and a bulk command to replace one owner with another, would cover the rest; neither is built. A list entry could also be an Entra group, which is still a list of people rather than a team structure. berth accepts individuals only, so that the gate never needs to look up group membership.

Ownership is data in the repository, so changing it is an ordinary owner-tier change. It is judged by the owners on the target branch, so nobody can add themselves as an owner and then approve their own change.

Each app gets its own namespace in each environment, named `<app>-<env>`. Airbnb uses the same `svc-env` pattern ([InfoQ talk](https://www.infoq.com/presentations/airbnb-kubernetes-services/)). In Kubernetes the namespace is where RBAC, quotas, network policy and pod security apply. With 400 deployments in three shared namespaces there is no built-in way to give an app's owners read access to their own pods, or to stop one app's traffic reaching another. The cost is about 1,100 namespaces across the two clusters, and DNS names change for calls between apps. `talks_to` hides the DNS change: an app that declares it calls `ledger/api` gets `LEDGER_API_URL`, and ledger's network policy admits it.

## Review effort follows risk

`platform/review.yaml` maps each kind of change to one of three tiers. An `auto` change merges once checks pass, provided an owner of the app opened it. An `owner` change needs approval from another owner. A `platform` change needs approval from both an owner and a platform approver. A version, a setting, a size or a schedule is `auto` in dev and qa and `owner` in prod. A public hostname, persistent storage, Azure access, an override, a new app, or a first deploy to prod is `platform` everywhere. Four checks look at the new value as well as the field, and raise a change to `platform`: a size the platform menu flags, more than ten replicas, exposing a service publicly, and a version that never ran in the environment this one promotes from (dev for qa, qa for prod). Production never drops below `owner`, whatever the table says. A path no rule covers needs the platform team, so an unforeseen field fails safe.

Writing rules down, instead of asking a person to judge each change, is what the larger companies do. Google's release engineering chapter says to "fit the deployment process to the risk profile of a given service" ([SRE book, ch. 8](https://sre.google/sre-book/release-engineering/)). Prodspec keeps manual approval for "particularly critical changes" so that approvers do not become desensitised ([Prodspec](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). Meta now tiers human review of diffs by risk ([RADAR](https://arxiv.org/abs/2605.30208), a preprint). berth uses a short table rather than a model because the table is auditable, people can argue about a line of it in a pull request, and a 30-person organisation does not have the incident history a model would need.

The production floor exists for separation of duties. Change management in regulated firms usually asks that someone other than the author approve a production change, not that a particular team does. Owner approval meets that. Check this against your own change policy before relying on it.

The gate compares meaning, not text. It merges each environment file over `app.yaml` on both sides of the change and diffs the results path by path, so editing a base setting in `app.yaml` shows as a change in every environment it reaches, including prod. When the platform team changes the renderer or a default, the affected apps' output changes without any change to their specs. The gate then lists those apps so that their owners can watch, but does not ask them to approve. That is the same idea as Meta's AutoCanary, where a shared change runs the checks of every service that consumes it ([AutoCanary](https://atscaleconference.com/?p=76100)).

Freezes delay production changes rather than reject them, as Annealing's checks do. A pull request labelled `break-glass` skips the freeze and the tiers, and needs one approval from any owner or platform approver other than its author. Google's guidance is that a breakglass bypass should exist, alert, and be reviewed afterwards ([Building Secure and Reliable Systems, ch. 14](https://google.github.io/building-secure-and-reliable-systems/raw/ch14.html)). berth implements the bypass; the alert and the follow-up review are not built.

Azure Repos has no CODEOWNERS file. It offers path-filtered required reviewers and required status checks ([branch policies](https://learn.microsoft.com/en-us/azure/devops/repos/git/branch-policies)). Path filters cannot tell a replica change from a hostname change inside the same file, and requiring several individual reviewers means all of them must approve, rather than any one owner other than the author. So the gate is code that posts one required status, `berth/review`.

## Who judges a pull request

A merge gate is worthless if the change it judges can rewrite it. Azure Repos runs a build-validation pipeline from the pull request's own merge commit. That means a pull request can edit `pipelines/pr.yml`, or `src/berth/review.py`, and post itself a passing status. berth therefore separates the checks from the gate.

`pipelines/pr.yml` is the pull request's own build: tests, `berth check`, schema and policy validation, and a preview of the review. It proves the change is well formed, but a pull request can weaken it, so it decides nothing on its own. `pipelines/gate.yml` is triggered by a webhook when a pull request is created, pushed to or voted on. Webhook-triggered pipelines always run the default branch's YAML, so the gate is main's copy. It checks out main's berth and runs it against the pull request's tree, using main's `review.yaml`, main's approvers and main's owners. It also re-runs `berth check`, because `rendered/` is what Flux deploys and a hand-edited manifest must not slip through as "already rendered". The gate posts as a dedicated identity, and the branch policy accepts `berth/review` only from that identity. Editing anything under `pipelines/`, `platform/` or `src/` is a platform-tier change, judged by the old rules.

The Kyverno policies in `platform/admission/` are the last layer. They enforce at the API server the same rules the renderer follows: images from the company registry pinned by digest, memory limits, an app reading only its own secrets from its own environment's vault, and two owners on every app namespace. Each namespace also enforces the Kubernetes `restricted` pod security standard.

Two gaps remain. The release bot opens pull requests as an identity that may only change versions, and the gate enforces that. If that identity is the shared build service, though, any pipeline in the project can act as the bot, so it should be a dedicated identity. And Flux applies whatever is on main, so the branch policy on main must not be bypassable by ordinary members, and its bypass permission should be audited.

## Secrets leave git

git-crypt shares one key across everyone who can decrypt. It gives no per-app access, every offboarding means rotating every secret, and an encrypted diff cannot be reviewed. It also cannot coexist with committed rendered output, because the manifests would have to carry decrypted values ([Akuity, the rendered manifests pattern](https://akuity.io/blog/the-rendered-manifests-pattern/)).

In berth a spec lists secret names only. Values live in one Key Vault per environment under the key `<app>--<name>`. The renderer creates one External Secrets Operator `ExternalSecret` per app, and the operator keeps a Kubernetes Secret in sync. Each environment's `ClusterSecretStore` admits only namespaces labelled with that environment. The admission policy rejects an `ExternalSecret` that reads another app's prefix or uses `dataFrom`, since a `dataFrom` find query could read the whole vault.

berth uses External Secrets Operator rather than the AKS Key Vault CSI driver because of batch jobs. The CSI driver creates a Kubernetes Secret only while a pod mounts the volume, and deletes it when the pods go ([CSI options](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options)). A job's pod that reads a secret as an environment variable needs the Secret to exist before it starts. The trade-off is isolation. With one operator identity per vault, an app is kept to its own secrets by Kubernetes admission rather than by Azure RBAC. The stronger alternative is one workload identity per app with role assignments scoped to individual secrets. Key Vault supports that scope, though Microsoft calls it "not recommended", and a subscription holds 5,000 role assignments ([RBAC guide](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide), [limits](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshoot-limits)). How owners set secret values is not built either. Open self-service in the dev and qa vaults, and a small pipeline or function that checks ownership for prod, would fit this design.

## Jobs are workloads too

A workflow in the spec is a set of tasks with dependencies, a schedule, a time zone, and an `orchestrator`: `airflow`, `controlm`, or `kubernetes` for a single-task CronJob. Every task pod comes from the same builder as a service pod, so a job gets the same image, security context, size, settings and secrets whether a Deployment, Airflow or Control-M starts it.

For Airflow, berth writes one plain DAG file per workflow, with each task a KubernetesPodOperator that carries its pod template inline. The pods run in the app's own namespace. A RoleBinding lets Airflow's worker service account create pods only in namespaces whose apps have Airflow workflows, rather than across the cluster. Generating plain Python has three advantages over generating YAML that a DAG factory reads at parse time. The rendered diff shows exactly what Airflow will run. A broken spec breaks one file, not every DAG the factory builds. And Airflow needs no berth code installed.

For Control-M, berth writes Automation API JSON: one folder per workflow, one `Job:Kubernetes` per task carrying a Kubernetes Job manifest, and flows for the dependencies. Kubernetes owns retries and the deadline, so Control-M only starts and watches the job. Control-M schedules at fixed times on chosen days, so berth translates the cron expressions it can express and rejects the rest with a message. A workflow on Control-M must also use the Control-M server's time zone. Control-M has no pull-based sync, so `pipelines/main.yml` runs `ctm deploy` after a merge. Deleting folders that disappear and detecting drift are not built.

Because the orchestrator is one field, moving a job between Control-M and Airflow is a one-line change that renders both sides. Stripe used the same move when it migrated cron jobs to Kubernetes: one YAML source generated both the old and the new scheduler's configuration, and rollback was a three-line change ([Stripe](https://stripe.com/blog/operating-kubernetes)).

## Rendered output is committed

The rendered manifests are committed in the same pull request as the spec change. `berth render` writes them and CI fails if they are stale, so a reviewer sees exactly what will reach Kubernetes rather than only the parameters. A renderer change shows its blast radius as a diff across every affected app. Meta likewise reviews both the configuration program and its generated JSON ([Tang et al.](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf)), and the same argument is behind the rendered manifests pattern.

Environments are directories on main, not branches. Branches per environment need cherry-picks, depend on merge order, and drift after hotfixes ([Kapelonis](https://dev.to/codefreshio/stop-using-branches-for-deploying-to-different-gitops-environments-5bmj)). Promotion in berth is copying one version line from `qa.yaml` to `prod.yaml`.

Each app environment gets its own Flux Kustomization. Flux applies a Kustomization as a unit, so with one per cluster a single invalid manifest would stop every app's deploys. With one per app environment the failure stays with that app, and Flux reports deploy status per app back to the commit in Azure DevOps.

berth uses Flux rather than Argo CD for three reasons. Flux is AKS's long-standing managed GitOps extension. It reaches Azure Repos with workload identity (`provider: azure`). And it posts commit statuses to Azure DevOps. Argo CD's main advantage is its UI, which would let developers see their own apps' sync state without asking anyone. Microsoft's September 2026 tutorial describes the AKS Argo CD extension as generally available, while the extensions release page still says preview. Because the output is plain YAML in a directory per app environment, switching means changing the one small file per app that the renderer emits, not the specs.

## Failure is contained

Google's Prodspec paper names partial output from a configuration system as the most common failure, and warns against deleting things because they are absent ([Prodspec](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production)). In GitOps, absence plus pruning is deletion. berth marks namespaces and persistent volume claims `kustomize.toolkit.fluxcd.io/prune: disabled`, so a missing file can remove an app's workloads but never its data. Removing an app from prod is a platform-tier change.

A bad version stalls instead of breaking. Deployments roll with `maxUnavailable: 0` behind readiness probes, so old pods keep serving until new ones are ready. In production, services spread across zones, and those with two or more pods get a disruption budget. berth does not roll back automatically. Flux has no progressive delivery, and with a few pods per service a statistical canary is mostly noise. Flagger could add canaries later for the services with enough traffic.

## Alternatives considered

Keeping the central repository and splitting review with CODEOWNERS per team directory is the smallest change. It was rejected because it rests on teams, because Azure Repos has no CODEOWNERS, and because path rules cannot see which field changed. Moving manifests into each app's repository, as Zalando and Shopify did, removes the bottleneck. But it also removes the consistency and fleet-wide changes that made central control worth having.

A shared Helm chart with a values file per app comes closest to berth. The values file is the spec. But Helm templates are text templating, so they validate nothing until they are rendered, and reviewers see values rather than output unless the output is rendered and committed anyway. berth keeps the values-file idea and replaces the templates with typed code and a checked schema.

Adopting a platform product (Score, KubeVela, Radius, Kratix, Humanitec, Crossplane) was weighed against a thin renderer; see [application-spec tools](research.md#application-spec-tools). Kargo could automate promotion with verification gates on top of Argo CD, and is the natural next step if manual promotion pull requests become the new bottleneck. Pushing from Azure Pipelines with environment approvals was rejected. Approval would attach to a pipeline run rather than to a reviewed change, drift would never be corrected, and the pipelines would hold cluster credentials.

## Moving from the current repository

Secrets move first. Moving them to Key Vault is useful on its own, and it is required before rendered output can be committed. An importer can then turn each existing Deployment, Service and ConfigMap into a spec. Whatever does not fit becomes an override, and counting the overrides measures the spec's gaps before anyone depends on it. The namespace move is the expensive part. A renderer option that places migrated apps in the old shared namespaces would let the spec, review and secrets changes land first, with apps moving to their own namespaces one at a time behind `ExternalName` services for the old DNS names. Neither the importer nor that option is built.

Before requiring the gate, run it in report-only mode: post the status without making it required, and compare its decisions with the infra lead's for a few weeks. That comparison also produces the measurement worth having first, namely what share of today's pull requests are version bumps, settings, resources, mounts and new apps.

## What is unverified

The parts that run locally have been verified: rendering, review, the CLI, schema validation of every rendered object, the Kyverno policies (via the CLI), and the generated DAGs in Airflow 3.2.2. Everything that needs a real service has not, and the code and pipelines that talk to one say so in a header comment:

- Azure DevOps: the gate pipeline's webhook trigger and payload path, the pull request and status REST calls, restricting the status to one identity, the release template, and `az repos pr create` with auto-complete.
- AKS and Flux: the bootstrap files, `provider: azure`, the commit-status notifications, Kyverno and External Secrets Operator in a real cluster, workload identity for ledger, the app routing ingress class and namespace, and running about 1,100 namespaces. The AKS documentation gives no namespace limit.
- Control-M: the `Job:Kubernetes` field names and flow syntax, which come from BMC documentation that returned 403 to fetching and was read only in search excerpts; `OrderMethod: Manual`; `generateName` in a Job spec; and the `ctm` CLI commands.
- Airflow: the GitDagBundle configuration that reads `rendered/airflow/<env>/`, and KubernetesPodOperator running pods in another namespace under the RoleBinding.

The decisions still open are Flux or Argo CD, External Secrets Operator with admission isolation or per-app identities, how owners set secret values, whether owners may include Entra groups, how to sequence the namespace move, and whether owner self-merge in dev and qa satisfies your change-management policy.
