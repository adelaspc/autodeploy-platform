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

{{- define "autodeploy-control-plane.runtimeSecretName" -}}
{{- $create := .Values.secrets.create -}}
{{- $existingSecret := .Values.secrets.existingSecret | default "" | trim -}}
{{- if and $create $existingSecret -}}
{{- fail "secrets.create=true and secrets.existingSecret cannot be used together" -}}
{{- else if and (not $create) (not $existingSecret) -}}
{{- fail "secrets.existingSecret is required when secrets.create=false" -}}
{{- else if $create -}}
{{- printf "%s-secret" (include "autodeploy-control-plane.fullname" .) -}}
{{- else -}}
{{- $existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.secretEnv" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $secretName := include "autodeploy-control-plane.runtimeSecretName" $root -}}
{{- $keys := list "CONTROL_PLANE_DATABASE_URL" -}}
{{- if eq $component "api" -}}
{{- $keys = concat $keys (list "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET" "CONTROL_PLANE_API_TOKEN_READ_ONLY" "CONTROL_PLANE_API_TOKEN_DEPLOYER" "CONTROL_PLANE_API_TOKEN_ADMIN" "CONTROL_PLANE_API_TOKENS_JSON" "CONTROL_PLANE_METRICS_TOKEN") -}}
{{- end -}}
{{- if eq $component "worker" -}}
{{- $keys = concat $keys (list "CONTROL_PLANE_REGISTRY_USERNAME" "CONTROL_PLANE_REGISTRY_PASSWORD") -}}
{{- end -}}
{{- if or (eq $component "api") (eq $component "worker") -}}
{{- range $key := $root.Values.secrets.gitTokenKeys -}}
{{- if not (regexMatch "^CONTROL_PLANE_GIT_TOKEN_[A-Za-z_][A-Za-z0-9_]*$" ($key | toString)) -}}
{{- fail (printf "invalid secrets.gitTokenKeys entry %q" $key) -}}
{{- end -}}
{{- $keys = append $keys ($key | toString) -}}
{{- end -}}
{{- if $root.Values.secrets.create -}}
{{- range $key, $value := $root.Values.secrets.values -}}
{{- if and (hasPrefix "CONTROL_PLANE_GIT_TOKEN_" $key) (ne ($value | toString) "") -}}
{{- $keys = append $keys $key -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $key := uniq $keys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $secretName }}
      key: {{ $key }}
      optional: true
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.validateRuntime" -}}
{{- $executor := index .Values.config "CONTROL_PLANE_EXECUTOR" | default "fake" | toString | trim -}}
{{- $workloadNamespace := index .Values.config "CONTROL_PLANE_K8S_NAMESPACE" | default "default" | toString | trim -}}
{{- if and (eq $executor "kubernetes") (empty .Values.kubeconfig.existingSecret) (ne $workloadNamespace .Release.Namespace) -}}
{{- fail (printf "CONTROL_PLANE_K8S_NAMESPACE=%s must match the Helm release namespace %s when the Kubernetes executor uses the chart ServiceAccount; configure kubeconfig.existingSecret for a separately authorized namespace" $workloadNamespace .Release.Namespace) -}}
{{- end -}}
{{- if and .Values.dockerSocket.enabled (empty (.Values.dockerSocket.groupId | toString | trim)) -}}
{{- fail "dockerSocket.groupId is required when dockerSocket.enabled=true" -}}
{{- end -}}
{{- end -}}

{{- define "autodeploy-control-plane.runtimePodSecurityContext" -}}
{{- $context := deepCopy .Values.podSecurityContext -}}
{{- if .Values.dockerSocket.enabled -}}
{{- $groups := get $context "supplementalGroups" | default (list) -}}
{{- $_ := set $context "supplementalGroups" (append $groups (int .Values.dockerSocket.groupId)) -}}
{{- end -}}
{{- toYaml $context -}}
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
