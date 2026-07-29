import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SafeMarkdown } from "../components/chat/SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders GFM without enabling raw HTML or dangerous URLs", () => {
    const { container } = render(<SafeMarkdown>{"# 标题\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))"}</SafeMarkdown>);
    expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("x").closest("a")).toHaveAttribute("href", "");
  });

  it("collapses long code blocks while leaving short code directly readable", () => {
    const longCode = Array.from({ length: 24 }, (_, index) => `line ${index + 1}`).join("\n");
    const { container, rerender } = render(<SafeMarkdown>{`\`\`\`text\n${longCode}\n\`\`\``}</SafeMarkdown>);
    const output = container.querySelector("details[data-technical-output]");
    expect(output).toBeInTheDocument();
    expect(output).not.toHaveAttribute("open");
    expect(screen.getByText("长代码块")).toBeInTheDocument();

    rerender(<SafeMarkdown>{"```text\nshort\n```"}</SafeMarkdown>);
    expect(container.querySelector("details[data-technical-output]")).toBeNull();
    expect(screen.getByText("short")).toBeInTheDocument();
  });
});
