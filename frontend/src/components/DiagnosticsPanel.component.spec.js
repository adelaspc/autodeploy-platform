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

    const buttons = wrapper.findAll("button");
    await buttons[0].trigger("click");
    await buttons[1].trigger("click");
    expect(wrapper.emitted("copy")).toHaveLength(1);
    expect(wrapper.emitted("download")).toHaveLength(1);
  });

  it("does not render when diagnostics are unavailable", () => {
    const wrapper = mount(DiagnosticsPanel, {
      props: { diagnostics: null, view: diagnosticsView(), formatTime: String },
    });

    expect(wrapper.html()).toBe("<!--v-if-->");
  });
});
