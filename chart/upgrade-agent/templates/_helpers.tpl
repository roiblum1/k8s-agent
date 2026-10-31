{{- define "upgrade-agent.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "upgrade-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "upgrade-agent.labels" -}}
{{ include "upgrade-agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "upgrade-agent.serviceAccountName" -}}
{{- default (include "upgrade-agent.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{/* The read-only rules on the MCE, shared by the per-namespace Roles and the all-namespaces ClusterRole. Never Secrets. */}}
{{- define "upgrade-agent.hcpReadRules" -}}
- apiGroups: [""]
  resources: [pods, pods/log, events, services, configmaps]
  verbs: [get, list]
- apiGroups: [apps]
  resources: [deployments, statefulsets, replicasets]
  verbs: [get, list]
- apiGroups: [batch]
  resources: [jobs]
  verbs: [get, list]
- apiGroups: [policy]
  resources: [poddisruptionbudgets]
  verbs: [get, list]
- apiGroups: [hypershift.openshift.io]
  resources: [hostedcontrolplanes]
  verbs: [get, list]
- apiGroups: [cluster.x-k8s.io]
  resources: [machines, machinesets, machinedeployments]
  verbs: [get, list]
- apiGroups: [capi-provider.agent-install.openshift.io]
  resources: [agentmachines]
  verbs: [get, list]
- apiGroups: [infrastructure.cluster.x-k8s.io]
  resources: [kubevirtmachines]
  verbs: [get, list]
{{- end -}}
