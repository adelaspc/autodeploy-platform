// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DeploymentEvents from "./DeploymentEvents.vue";


describe("DeploymentEvents", () => {
  it("renders event data through the real Vue template", () => {
    const wrapper = mount(DeploymentEvents, {
      props: {
        events: [{
          id: 1,
          status: "failed",
          event_type: "kubernetes.rollout_failed",
          step: "deploy.kubernetes.rollout",
          created_at: "2026-01-01T10:00:00Z",
          message: "Pod did not become ready",
        }],
        statusTone: (status) => status === "failed" ? "danger" : "neutral",
        formatTime: () => "10:00",
      },
    });

    expect(wrapper.get(".event-row [data-tone='danger']").text()).toBe("failed");
    expect(wrapper.get(".event-row strong").text()).toBe("kubernetes.rollout_failed");
    expect(wrapper.text()).toContain("deploy.kubernetes.rollout - 10:00");
    expect(wrapper.text()).toContain("Pod did not become ready");
  });

  it("renders an explicit empty state", () => {
    const wrapper = mount(DeploymentEvents, {
      props: { events: [], statusTone: () => "neutral", formatTime: String },
    });

    expect(wrapper.text()).toContain("No events loaded.");
    expect(wrapper.findAll(".event-row")).toHaveLength(0);
  });
});
