// @vitest-environment jsdom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import LogViewer from "./LogViewer.vue";


describe("LogViewer", () => {
  it("distinguishes retained-artifact removal from logs that were never produced", () => {
    const wrapper = mount(LogViewer, {
      props: {
        summary: { build_log_state: "retention_removed", runtime_log_state: "not_produced" },
        enabled: true,
      },
    });

    expect(wrapper.text()).toContain("Build log removed by the retention policy.");
    expect(wrapper.text()).toContain("No runtime log was produced.");
  });

  it("renders available log content instead of its artifact state", () => {
    const wrapper = mount(LogViewer, {
      props: {
        buildLog: { content: "build complete" },
        runtimeLog: { content: "application ready" },
        summary: { build_log_state: "retention_removed", runtime_log_state: "retention_removed" },
      },
    });

    expect(wrapper.text()).toContain("build complete");
    expect(wrapper.text()).toContain("application ready");
    expect(wrapper.text()).not.toContain("removed by the retention policy");
  });
});
