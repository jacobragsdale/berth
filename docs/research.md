# How other companies manage deployment configuration

The companies with public material on this all moved away from a single human gate in the same direction. The platform owns the schema, the generator, the validators and the rollout machinery. Service owners write values. A human approver is required only where a rule says the change is risky. This page records what each company has published, gathered on 2026-11-07, and which parts berth borrows. Claims are from the company's own engineers unless marked "secondary". "Excerpt" means only a search excerpt could be read, because the site blocks automated fetching.

## Google

Service owners declare the end state they want, and shared generators expand it. Most services use "integrators" that provide "opinionated service models for specific service archetypes". Changing that intent "usually involves manual work and approval". After that, no human steps in, except for "particularly critical changes (e.g., data deletion)", which are kept few "to avoid desensitization". Every snapshot runs through "an extensive series of validators". Rollout checks delay a push rather than reject it: holidays, firing alerts, capacity below peak. The paper also warns that a configuration system returning partial output is the most common failure, so deletion needs an explicit marker rather than absence. ([Prodspec and Annealing](https://www.usenix.org/publications/loginonline/prodspec-and-annealing-intent-based-actuation-google-production))

Google's retrospective on Borg says application configuration became a catch-all and that configuration languages drift towards Turing-completeness. Its advice is to keep data in JSON or YAML and computation in a real language. ([Borg, Omega, and Kubernetes](https://queue.acm.org/detail.cfm?id=2898444))

The SRE Workbook says to ask as few mandatory questions as possible and default the rest, to validate meaning at commit time, and to reject unknown fields. In its example, an infra team maintains one Kubernetes template per component and app teams instantiate it. ([ch. 14](https://sre.google/workbook/configuration-design/), [ch. 15](https://sre.google/workbook/configuration-specifics/)) The release engineering chapter says to "fit the deployment process to the risk profile of a given service". ([SRE book, ch. 8](https://sre.google/sre-book/release-engineering/))

OWNERS files are per directory, hierarchical, list individuals, and need no central registration. Low-risk generated changes go to codebase-wide approvers and risky ones to local owners. ([Software Engineering at Google, ch. 9](https://abseil.io/resources/swe-book/html/ch09.html)) Breakglass bypasses must exist, alert, and be audited. ([Building Secure and Reliable Systems, ch. 14](https://google.github.io/building-secure-and-reliable-systems/raw/ch14.html)) Autopilot sets resource limits instead of owners: autopiloted jobs have 23% slack against 46% for manually set ones. ([Autopilot](https://research.google/pubs/autopilot-workload-autoscaling-at-google-scale/))

## Meta

Configerator treats configuration as code: a Thrift schema plus Python, compiled to JSON. In the paper's example, which it labels hypothetical, the scheduler team owns the schema, a reusable `create_job` module and a validator that "ensures that configs provided by other teams do not accidentally break the scheduler". Other teams only call `create_job(...)`. Review covers both the source and the generated JSON. The paper names a "central committee for approval" as the conservative extreme. Of configuration-caused issues it studied, 42% were obvious errors, 36% subtle ones, and 22% valid changes that exposed code bugs. ([Holistic Configuration Management, SOSP 2015](https://sigops.org/s/conferences/sosp/2015/current/2015-Monterey/printable/008-tang.pdf))

In Twine, owners request quota and the platform places, autoscales and sizes from fixed menus. Per-task customisation was "abused" and is being removed. ([Twine, OSDI 2020](https://www.usenix.org/conference/osdi20/presentation/tang)) In Conveyor, 97% of pipelines deploy with no manual validation, and health checks revert by default. ([Conveyor, OSDI 2023](https://www.usenix.org/conference/osdi23/presentation/grubic)) With AutoCanary, a configuration change runs the canary specs of every service that consumes it, and is blocked if any fails. ([AutoCanary](https://atscaleconference.com/?p=76100)) The 2026 preprint RADAR describes tiering human review of diffs by risk, with SOX-scoped code excluded. ([RADAR](https://arxiv.org/abs/2605.30208))

## Uber

Up has about 4,500 stateless services and over 100,000 deploys a week. Rollouts go in small increments with health integrations, and any observed issue reverts the configuration. The post does not show the service spec format. ([Up](https://www.uber.com/us/en/blog/up-portable-microservices-ready-for-the-cloud/)) Up's continuous deployment is on by default. Owners customise gating conditions from predefined options: soak time, deploy windows, firing alerts. There is no manual approval step. Automated deploys rose from about 7% to 65% of services. ([Continuous deployment](https://www.uber.com/blog/continuous-deployment/)) Cross-cutting commits roll out by service tier, least critical first. ([Large-scale monorepo changes](https://www.uber.com/blog/controlling-the-rollout-of-large-scale-monorepo-changes)) Developers work only through Up, which "abstracted away cluster technology". ([Kubernetes migration](https://www.uber.com/blog/migrating-ubers-compute-platform-to-kubernetes-a-technical-journey/))

## Netflix

Managed Delivery (Keel) declares environments and the constraints that decide when a version is promoted: `depends-on`, `allowed-times`, manual judgement, pipeline and canary. ([constraints](https://spinnaker.io/docs/guides/user/managed-delivery/environment-constraints/)) Central teams inject opinions through resolvers at resolution time. A central change then reaches every service without editing any spec. ([Keel](https://github.com/spinnaker/spinnaker/tree/main/keel)) The "paved road" lets teams deviate from the central defaults (secondary: [InfoQ](https://www.infoq.com/news/2018/06/netflix-full-cycle-developers/)).

## Stripe

Little is public. Each change runs about 1.4 million tests, goes to pre-production, then rolls out from one machine to 0.5%, 1%, 5% and 20% of the fleet, checked against 55,000 metrics. ([2023 annual letter](https://stripe.com/annual-updates/2023)) For cron, engineers declare jobs in "a simple YAML format" (name, schedule, command, CPU, memory), and a small program renders them. During migration, one YAML source generated both the old Chronos and the new Kubernetes configuration, and rollback was "a three-line configuration change". ([Operating Kubernetes](https://stripe.com/blog/operating-kubernetes))

## Others

- **Airbnb.** Each service has a `kubegen.yaml` that generates per-environment Kubernetes files. Global CI validation cannot be opted out of, an admission controller rejects workloads with missing ownership, and namespaces follow `svc-env`. ([InfoQ talk by Airbnb engineers](https://www.infoq.com/presentations/airbnb-kubernetes-services/))
- **Monzo.** Production gets "one review from an engineer on the owning team", and a build cannot reach production before staging. ([How we deploy](https://monzo.com/blog/2022/05/16/how-we-deploy-to-production-over-100-times-a-day))
- **Spotify.** Infrastructure is declared as CRDs. Client-side checks "can easily be bypassed", so admission control is the real enforcement. Bot pull requests auto-merge after tests, during the owners' working hours. ([Fleet management, part 2](https://engineering.atspotify.com/2023/05/fleet-management-at-spotify-part-2-the-path-to-declarative-infrastructure/), [part 3](https://engineering.atspotify.com/2023/05/fleet-management-at-spotify-part-3-fleet-wide-refactoring/))
- **Shopify** generated manifests from a services database, and gave developers no kubectl access (excerpt, 2018 slides). **Zalando** kept manifests in each repository with a four-eyes rule for production (excerpt, 2017–19 slides).

## GitOps practice

The [OpenGitOps v1.0 principles](https://opengitops.dev/) are declarative state, versioned and immutable storage, automatic pulling, and continuous reconciliation. They leave approval and ownership to the git host. The [rendered manifests pattern](https://akuity.io/blog/the-rendered-manifests-pattern/) commits CI-rendered plain YAML, because without it "the reviewer approves a parameter change, not a manifest change": a one-line chart bump can change a thousand lines of output. It is incompatible with secrets decrypted at apply time. Environment-per-branch is widely called an anti-pattern; environments as directories on main make promotion a copy ([Kapelonis](https://dev.to/codefreshio/stop-using-branches-for-deploying-to-different-gitops-environments-5bmj), [Flux repository structure](https://fluxcd.io/flux/guides/repository-structure/)). [Kargo](https://docs.kargo.io/user-guide/core-concepts/) automates promotion between stages on top of Argo CD.

## Application-spec tools

| Tool | What it is | Maturity, 2026-11 | Batch jobs |
|---|---|---|---|
| [Score](https://github.com/score-spec/score-k8s) | developer-written `score.yaml`; the platform writes provisioners | CNCF Sandbox; score-k8s 0.22 | Deployment or StatefulSet only |
| [KubeVela](https://kubevela.io/docs/getting-started/core-concept/) | components, traits and workflow over CUE definitions | CNCF Incubating; the OAM spec's last release was 2021 | `task`, `cron-task` |
| [Radius](https://github.com/radius-project/radius) | apps plus platform-curated Bicep or Terraform recipes | CNCF Sandbox; v0.62 | not covered |
| [Kratix](https://docs.kratix.io/main/reference/promises/intro) | Promises (CRD APIs plus workflows) written to a Git state store | no CNCF status found | via a Promise |
| [Humanitec](https://developer.humanitec.com/platform-orchestrator/docs/platform-orchestrator/overview/) | commercial orchestrator reading Score | rebuilt in 2025 (secondary) | not stated |
| [Crossplane](https://docs.crossplane.io/latest/whats-new/) | composite resources composed by function pipelines | CNCF Graduated (secondary) | any kind |

## What berth takes from this

| Pattern | Source | In berth |
|---|---|---|
| Platform owns schema, generator and validators; owners write values | Meta, Google, Airbnb, Stripe | `model.py`, `render/`, `validate.py` |
| Review the rendered output as well as the parameters | Meta, Akuity | `berth diff` on every pull request; what deployed is published to the `rendered` branch |
| Owners listed as individuals per directory | Google OWNERS | `owners:` in `app.yaml`, judged from the target branch |
| Approval by rule and risk; humans only where needed | Google, Meta RADAR, Uber | `platform/review.yaml`, `review.py` |
| Admission control as the real enforcement | Spotify, Airbnb | built-in ValidatingAdmissionPolicies, rendered by `render/admission.py` |
| A version reaches prod only after the previous stage | Monzo, Netflix `depends-on` | promotion escalation from git history |
| Checks that delay rather than reject | Google Annealing, Uber windows | `freezes` |
| Breakglass that is fast and audited | Google BSRS | `berth breakglass` marks, the `break-glass` label, and the daily drift report |
| No deletion by absence | Google Prodspec | prune only with `--prune`, never namespaces or volumes |
| Shared changes reported to the services they reach | Meta AutoCanary | "rendered output changes without a spec change" |
| One source rendering two schedulers | Stripe | `orchestrator: airflow` or `controlm` |
| Curated menus plus a counted escape hatch | Meta Twine, SRE Workbook | `sizes`, `overrides` with a required reason |

berth does not borrow a custom configuration language, statistical canaries (too few pods for a reliable signal), ML risk scoring (no incident history to train on), or Netflix's freedom to leave the paved road, which would contradict "no raw Kubernetes". Team ownership is the default in every source except Google. berth's per-person ownership has no precedent beyond OWNERS files, which is why it requires two owners.
