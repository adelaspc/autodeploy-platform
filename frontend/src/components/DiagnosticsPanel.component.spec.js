// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DiagnosticsPanel from "./DiagnosticsPanel.vue";
import { DIAGNOSTICS_BUNDLE_NOTICE } from "../diagnosticsBundle.js";


function diagnosticsView() {
  return {
    stage: "rollout",
    stageLabel: "Rollout failure",
    summary: "Pod did not become ready",
    eventType: "kubernetes.rollout_failed",
    eventAt: "2026-01-01T10:00:00Z",
    snapshotAt: "2026-01-01T10:00:00Z",
    insights: [],
    hasHelm: false,
    helmFields: [],
    helmStderrSummary: null,
    helmStdoutSummary: null,
    hasResourceContext: true,
    podFields: [],
    resourceFields: [],
    missingResources: ["Secret/demo-secret"],
    checkedResources: ["Secret/demo-secret"],
    podNames: ["demo-pod"],
    podDescribeSummary: "not ready",
    podLogsSummary: "startup failed",
    podPreviousLogsSummary: null,
    rawJson: "{}",
  };
}


describe("DiagnosticsPanel", () => {
  it("renders safe-export guidance and emits both bundle actions", async () => {
    const wrapper = mount(DiagnosticsPanel, {
      props: {
        diagnostics: { deployment_id: 8 },
        view: diagnosticsView(),
        formatTime: (value) => `formatted:${value}`,
      },
    });

    expect(wrapper.get('[role="note"]').text()).toContain(DIAGNOSTICS_BUNDLE_NOTICE);
    expect(wrapper.text()).toContain("Secret/demo-secret");
    expect(wrapper.text()).toContain("formatted:2026-01-01T10:00:00Z");
    expect(wrapper.text()).toContain("Refresh reloads this evidence; it does not query the cluster.");

    const buttons = wrapper.findAll("button");
    await buttons[0].trigger("click");
    await buttons[1].trigger("click");
    expect(wrapper.emitted("copy")).toHaveLength(1);
    expect(wrapper.emitted("download")).toHaveLength(1);
  });

  it("labels current and previous logs and explains partial snapshots", () => {
    const wrapper = mount(DiagnosticsPanel, {
      props: {
        diagnostics: { deployment_id: 8 },
        view: { ...diagnosticsView(), collectionStatus: "partial", collectedAt: "2026-09-06T10:00:00Z",
          collectionErrors: [{ operation: "service", reason: "access_denied" }],
          podLogsSummary: "Current attempt", podPreviousLogsSummary: "Boot failed" },
        formatTime: String,
      },
    });
    expect(wrapper.text()).toContain("Persisted diagnostic snapshot");
    expect(wrapper.text()).toContain("Collection status: partial");
    expect(wrapper.text()).toContain("The original Helm error is preserved");
    expect(wrapper.text()).toContain("service: access_denied");
    expect(wrapper.text()).toContain("Current logs");
    expect(wrapper.text()).toContain("Previous logs");
    expect(wrapper.text()).toContain("Boot failed");
  });

  it("keeps the snapshot warning when legacy evidence has no capture time", () => {
    const wrapper = mount(DiagnosticsPanel, {
      props: {
        diagnostics: { deployment_id: 8 },
        view: { ...diagnosticsView(), snapshotAt: null },
        formatTime: String,
      },
    });

    expect(wrapper.text()).toContain("Capture time was not recorded.");
    expect(wrapper.text()).toContain("Refresh reloads persisted evidence; it does not query the cluster.");
  });

  it("does not render when diagnostics are unavailable", () => {
    const wrapper = mount(DiagnosticsPanel, {
      props: { diagnostics: null, view: diagnosticsView(), formatTime: String },
    });

    expect(wrapper.html()).toBe("<!--v-if-->");
  });
});
