// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DeploymentSummary from "./DeploymentSummary.vue";


const baseSummary = {
  deployment_id: 13,
  deployment_status: "pending",
  build_status: "pending",
  current_step: "deployment",
  image_tag: "0123456789ab",
  image_ref: "example/app@sha256:abc",
};


describe("DeploymentSummary", () => {
  it("identifies a retry as using historical inputs", () => {
    const wrapper = mount(DeploymentSummary, {
      props: {
        summary: {
          ...baseSummary,
          creation_action: "retry",
          source_deployment_id: 12,
          configuration_source: "historical_snapshot",
        },
        statusTone: () => "neutral",
        formatTime: String,
      },
    });

    expect(wrapper.text()).toContain("retry of #12");
    expect(wrapper.text()).toContain("Historical snapshot");
  });

  it("identifies a redeploy as using current project inputs", () => {
    const wrapper = mount(DeploymentSummary, {
      props: {
        summary: {
          ...baseSummary,
          creation_action: "redeploy",
          source_deployment_id: 12,
          configuration_source: "current_project",
        },
        statusTone: () => "neutral",
        formatTime: String,
      },
    });

    expect(wrapper.text()).toContain("redeploy of #12");
    expect(wrapper.text()).toContain("Current project");
  });

  it("distinguishes the API health probe from opening the service in a browser", () => {
    const wrapper = mount(DeploymentSummary, {
      props: {
        summary: { ...baseSummary, service_url: "https://demo.example" },
        liveHealth: {
          status: "healthy",
          message: "Workload healthcheck returned HTTP 200.",
          checked_at: "2026-09-08T12:00:00Z",
        },
        statusTone: () => "positive",
        formatTime: (value) => `formatted:${value}`,
      },
    });

    expect(wrapper.get(".service-link").text()).toBe("Open service in browser");
    expect(wrapper.text()).toContain("Live health (control-plane API)");
    expect(wrapper.text()).toContain("checked from the control-plane API at formatted:2026-09-08T12:00:00Z");
  });
});
