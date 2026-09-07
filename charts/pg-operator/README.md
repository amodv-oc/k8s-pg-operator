# pg-operator

Kubernetes operator that manages databases, roles and grants on **external**
PostgreSQL servers — typically Amazon RDS — from `PostgresInstance`,
`PostgresDB` and `PostgresUser` custom resources.

Full documentation: the [repository README](../../README.md).

## Install

```bash
helm install pg-operator ./charts/pg-operator \
  --namespace pg-operator --create-namespace
```

Requires Kubernetes ≥ 1.25 (CEL validation rules in the CRDs).

### CRDs

The CRDs live in `crds/`, so Helm installs them on first install but will
**not** upgrade or delete them. Apply changes explicitly when upgrading:

```bash
kubectl apply -f charts/pg-operator/crds/
```

This is intentional: an accidental CRD deletion would take every
`PostgresInstance`, `PostgresDB` and `PostgresUser` object with it.

## What gets created

| Resource | Condition |
| --- | --- |
| `Deployment` | Always. Two containers: `operator` and `api` |
| `ServiceAccount` | `serviceAccount.create` |
| `ClusterRole` / `ClusterRoleBinding` | `rbac.create` |
| `ClusterRole` (`-viewer`, `-editor`) | `rbac.createViewerRole`; aggregate into the built-in `view` / `edit` roles |
| `Role` / `RoleBinding` per namespace | `rbac.namespacedSecrets` |
| `Service` (`-api`) | `api.enabled` |
| `Service` (`-metrics`) | `metrics.enabled` |
| `ServiceMonitor` | `metrics.serviceMonitor.enabled` |
| `Ingress` | `api.ingress.enabled` |
| `PodDisruptionBudget` | `podDisruptionBudget.enabled` |
| `NetworkPolicy` | `networkPolicy.enabled` |
| `ClusterKopfPeering` | `peering.enabled` |
| `PostgresInstance` | one per entry in `instances` |

## Values

### Image

| Key | Default | Description |
| --- | --- | --- |
| `image.repository` | `ghcr.io/your-org/pg-operator` | **Set this.** |
| `image.tag` | `""` | Defaults to `.Chart.AppVersion` |
| `image.digest` | `""` | Pin by digest in production; takes precedence over `tag` |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | |

### Scope and scheduling

| Key | Default | Description |
| --- | --- | --- |
| `watchNamespaces` | `[]` | Namespaces to watch for `PostgresDB`/`PostgresUser`. Empty watches all. `PostgresInstance` is cluster-scoped and always watched cluster-wide |
| `replicaCount` | `1` | Only one reconciler may be active — see peering |
| `peering.enabled` | `false` | kopf peering; **required** for more than one replica |
| `peering.name` | `""` | Defaults to the release fullname |
| `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints` | `{}` / `[]` | |
| `priorityClassName` | — | |
| `terminationGracePeriodSeconds` | `30` | |

The Deployment uses the `Recreate` strategy: a rolling update would briefly run
two reconcilers issuing DDL against the same server.

### Reconciliation

| Key | Default | Description |
| --- | --- | --- |
| `reconcile.interval` | `300` | Full convergence pass, in seconds. This is what corrects drift applied directly to the database |
| `reconcile.idle` | `30` | Quiet period after a change before a timer fires |
| `reconcile.retryBackoff` | `30` | Backoff after a temporary failure |

### PostgreSQL connections

| Key | Default | Description |
| --- | --- | --- |
| `postgres.connectTimeout` | `10` | Seconds |
| `postgres.statementTimeoutMs` | `30000` | Ceiling on any single statement, so a reconcile cannot wedge on a lock |
| `postgres.poolMaxSize` | `4` | Per (instance, database) |
| `postgres.poolMaxIdle` | `120` | Seconds before an idle connection is closed |

### Credentials and PushSecret

| Key | Default | Description |
| --- | --- | --- |
| `credentials.passwordLength` | `32` | Overridable per `PostgresUser` |
| `pushSecret.enabled` | `true` | Emit `external-secrets.io` PushSecrets |
| `pushSecret.apiVersion` | `external-secrets.io/v1` | Use `external-secrets.io/v1alpha1` below external-secrets 0.14 |

If the PushSecret CRD is absent, each `PostgresInstance` reports it in one
condition rather than failing every user reconcile.

### RBAC

| Key | Default | Description |
| --- | --- | --- |
| `rbac.create` | `true` | |
| `rbac.namespacedSecrets` | `false` | Narrow Secret access to `Role`s in `rbac.secretNamespaces` |
| `rbac.secretNamespaces` | `[]` | Required when the above is true |
| `rbac.createViewerRole` | `true` | Aggregate the CRDs into the built-in `view` and `edit` cluster roles |
| `rbac.extraRules` | `[]` | Appended to the ClusterRole |

The operator reads instance credentials from wherever they live and writes
generated credentials into consuming namespaces, so it holds cluster-wide
Secret access by default. Kubernetes cannot narrow that with a label selector.
Where the namespaces are known and fixed:

```yaml
rbac:
  namespacedSecrets: true
  secretNamespaces: [pg-operator, team-a, team-b]
```

### State API

| Key | Default | Description |
| --- | --- | --- |
| `api.enabled` | `true` | Runs as a second container in the operator pod |
| `api.port` | `8000` | |
| `api.cacheTtl` | `5` | Response cache, in seconds; absorbs dashboard polling |
| `api.rootPath` | `""` | Set when served under a path prefix behind a proxy |
| `api.corsOrigins` | `[]` | Allowed origins for a browser frontend |
| `api.service.type` / `.port` / `.annotations` | `ClusterIP` / `8000` / `{}` | |
| `api.ingress.*` | disabled | `className`, `annotations`, `hosts`, `tls` |
| `api.resources` | 25m / 96Mi request | |
| `operator.healthPort` | `8080` | kopf liveness endpoint |
| `operator.resources` | 100m / 192Mi request | |

The API is read-only and never returns credential values.

### Observability

| Key | Default | Description |
| --- | --- | --- |
| `metrics.enabled` | `true` | Prometheus metrics on `:9090/metrics` |
| `metrics.port` | `9090` | |
| `metrics.serviceMonitor.enabled` | `false` | Requires the Prometheus Operator |
| `metrics.serviceMonitor.namespace` / `.labels` / `.interval` / `.scrapeTimeout` | release ns / `{}` / `30s` / `10s` | |
| `logging.level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `logging.format` | `json` | `json` or `console` |

Without a ServiceMonitor, `prometheus.io/scrape` annotations are added to the
pod instead.

### Security and networking

| Key | Default | Description |
| --- | --- | --- |
| `podSecurityContext` | non-root 65532, `RuntimeDefault` seccomp | |
| `containerSecurityContext` | read-only root, no privilege escalation, all caps dropped | |
| `tmpSizeLimit` | `16Mi` | In-memory scratch for CA bundles read from Secrets — libpq needs `sslrootcert` as a file path |
| `podDisruptionBudget.*` | disabled | |
| `networkPolicy.enabled` | `false` | |
| `networkPolicy.apiIngressFrom` / `.metricsIngressFrom` | `[]` | Who may reach the API / metrics |
| `networkPolicy.egress` | `[]` | **Empty allows all egress**, because the RDS endpoints are outside the cluster and unknown at install time |

Pin egress to your database CIDRs in production:

```yaml
networkPolicy:
  enabled: true
  egress:
    - to: [{ ipBlock: { cidr: 10.0.0.0/16 } }]
      ports: [{ port: 5432, protocol: TCP }]
```

### Declaring instances from the chart

Ship instances with the operator instead of applying them separately. Keys
become resource names and values are the spec verbatim. Credentials are always
referenced, never templated in.

```yaml
instances:
  prod-rds:
    host: prod.abc123.ap-southeast-2.rds.amazonaws.com
    credentialsSecretRef:
      name: prod-rds-superuser
      namespace: pg-operator
    sslMode: verify-full
    sslRootCertSecretRef:
      name: rds-ca-bundle
      namespace: pg-operator
    retentionPolicy: RETAIN
```

These carry `helm.sh/resource-policy: keep`, so uninstalling the chart does not
delete them — and therefore cannot trigger a `DROP` retention policy.

### Extension points

| Key | Default |
| --- | --- |
| `extraEnv` | `[]` |
| `extraVolumes`, `extraVolumeMounts` | `[]` |
| `commonLabels`, `annotations`, `podLabels`, `podAnnotations` | `{}` |
| `nameOverride`, `fullnameOverride` | `""` |

## Uninstall

```bash
helm uninstall pg-operator --namespace pg-operator
```

Removing the operator does not touch any database. `PostgresDB` and
`PostgresUser` objects keep their finalizers, so they will not delete while the
operator is gone — delete them *before* uninstalling if you want their
retention policies applied.

The CRDs are not removed by `helm uninstall`. To remove them (which deletes
every `PostgresInstance`, `PostgresDB` and `PostgresUser` object):

```bash
kubectl delete -f charts/pg-operator/crds/
```
