import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// React 19 的 act 环境标记；Testing Library 需要它来刷新状态。
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
