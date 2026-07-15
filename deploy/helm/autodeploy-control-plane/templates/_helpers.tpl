{{- define "autodeploy-control-plane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "autodeploy-control-plane.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "autodeploy-control-plane.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "autodeploy-control-plane.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.workspaceClaimName" -}}
{{- if .Values.workspace.existingClaim -}}
{{- .Values.workspace.existingClaim -}}
{{- else -}}
{{- printf "%s-workspace" (include "autodeploy-control-plane.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.workspaceMountPath" -}}
{{- default "/tmp/paas-workspaces" .Values.config.CONTROL_PLANE_WORKSPACE_ROOT -}}
{{- end -}}

{{- define "autodeploy-control-plane.workspaceVolume" -}}
{{- if eq .Values.workspace.type "hostPath" -}}
hostPath:
  path: {{ required "workspace.hostPath is required when workspace.type=hostPath" .Values.workspace.hostPath }}
  type: DirectoryOrCreate
{{- else -}}
persistentVolumeClaim:
  claimName: {{ include "autodeploy-control-plane.workspaceClaimName" . }}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.workspaceInitContainer" -}}
{{- if .Values.workspace.initPermissions.enabled -}}
- name: workspace-permissions
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - sh
    - -c
    - >
      mkdir -p {{ include "autodeploy-control-plane.workspaceMountPath" . }}
      && chown -R {{ .Values.workspace.initPermissions.user }}:{{ .Values.workspace.initPermissions.group }} {{ include "autodeploy-control-plane.workspaceMountPath" . }}
  securityContext:
    runAsUser: 0
  volumeMounts:
    - name: workspace
      mountPath: {{ include "autodeploy-control-plane.workspaceMountPath" . }}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.labels" -}}
app.kubernetes.io/name: {{ include "autodeploy-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "autodeploy-control-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "autodeploy-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
