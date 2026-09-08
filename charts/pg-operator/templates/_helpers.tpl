{{/* Chart name, overridable with nameOverride. */}}
{{- define "pg-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified release name. fullnameOverride wins; otherwise the release name
is used as-is when it already contains the chart name, so a release called
"pg-operator" does not become "pg-operator-pg-operator".
*/}}
{{- define "pg-operator.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "pg-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "pg-operator.labels" -}}
helm.sh/chart: {{ include "pg-operator.chart" . }}
{{ include "pg-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: pg-operator
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "pg-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pg-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "pg-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pg-operator.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "pg-operator.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "pg-operator.ui.image" -}}
{{- $tag := .Values.ui.image.tag | default .Chart.AppVersion -}}
{{- if .Values.ui.image.digest -}}
{{- printf "%s@%s" .Values.ui.image.repository .Values.ui.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.ui.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/*
The dashboard is a static bundle plus a proxy to the API on loopback; without
the API container in the pod there is nothing for it to proxy to. Failing at
render time beats shipping a pod that serves nothing but 502s.
*/}}
{{- define "pg-operator.ui.validate" -}}
{{- if and .Values.ui.enabled (not .Values.api.enabled) -}}
{{- fail "ui.enabled requires api.enabled: the dashboard proxies the state API on 127.0.0.1 in the same pod" -}}
{{- end -}}
{{- end -}}

{{/*
Environment shared by the operator and API containers, so both read the same
configuration from one place.
*/}}
{{- define "pg-operator.env" -}}
- name: PG_OPERATOR_NAMESPACE
  value: {{ .Release.Namespace | quote }}
{{- if .Values.watchNamespaces }}
- name: PG_OPERATOR_WATCH_NAMESPACES
  value: {{ join "," .Values.watchNamespaces | quote }}
{{- end }}
- name: PG_OPERATOR_LOG_LEVEL
  value: {{ .Values.logging.level | quote }}
- name: PG_OPERATOR_LOG_FORMAT
  value: {{ .Values.logging.format | quote }}
- name: PG_OPERATOR_RECONCILE_INTERVAL
  value: {{ .Values.reconcile.interval | quote }}
- name: PG_OPERATOR_RECONCILE_IDLE
  value: {{ .Values.reconcile.idle | quote }}
- name: PG_OPERATOR_RETRY_BACKOFF
  value: {{ .Values.reconcile.retryBackoff | quote }}
- name: PG_OPERATOR_CONNECT_TIMEOUT
  value: {{ .Values.postgres.connectTimeout | quote }}
- name: PG_OPERATOR_STATEMENT_TIMEOUT_MS
  value: {{ .Values.postgres.statementTimeoutMs | quote }}
- name: PG_OPERATOR_POOL_MAX_SIZE
  value: {{ .Values.postgres.poolMaxSize | quote }}
- name: PG_OPERATOR_POOL_MAX_IDLE
  value: {{ .Values.postgres.poolMaxIdle | quote }}
- name: PG_OPERATOR_PASSWORD_LENGTH
  value: {{ .Values.credentials.passwordLength | quote }}
- name: PG_OPERATOR_PUSHSECRET_ENABLED
  value: {{ .Values.pushSecret.enabled | quote }}
- name: PG_OPERATOR_PUSHSECRET_API_VERSION
  value: {{ .Values.pushSecret.apiVersion | quote }}
- name: PG_OPERATOR_CERT_DIR
  value: /tmp
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
