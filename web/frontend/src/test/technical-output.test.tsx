import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TechnicalOutput, outputMeta, outputText } from "../components/chat/TechnicalOutput";

describe("TechnicalOutput", () => {
  it("starts collapsed and exposes a compact summary", () => {
    const value = { score: 0.67, source: "advisory.json", text: "long result" };
    const { container } = render(<TechnicalOutput label="工具输出 · search" value={value} />);
    const details = container.querySelector("details[data-technical-output]")!;
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("工具输出 · search")).toBeInTheDocument();
    expect(screen.getAllByText(/行 · \d+ B/).length).toBeGreaterThan(0);
  });

  it("can be expanded and collapsed with the native summary control", () => {
    const { container } = render(<TechnicalOutput label="运行输出" value="first\nsecond" />);
    const details = container.querySelector("details")!;
    const summary = container.querySelector("summary")!;
    fireEvent.click(summary);
    expect(details).toHaveAttribute("open");
    fireEvent.click(summary);
    expect(details).not.toHaveAttribute("open");
  });

  it("normalizes structured output and reports its size", () => {
    const text = outputText({ ok: true });
    expect(text).toContain('"ok": true');
    expect(outputMeta(text)).toMatch(/^3 行 · \d+ B$/);
  });
});
