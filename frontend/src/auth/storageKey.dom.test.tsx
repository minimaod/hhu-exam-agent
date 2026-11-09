import { expect, test } from "vitest";
import { storageKeyFor } from "./AuthContext";

test("会话恢复键按用户隔离", () => {
  expect(storageKeyFor("a")).not.toBe(storageKeyFor("b"));
  expect(storageKeyFor("a")).toContain("a");
  expect(storageKeyFor(null)).toBeNull();
});