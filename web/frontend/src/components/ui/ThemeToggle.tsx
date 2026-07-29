import { Moon, Sun } from "lucide-react";
import { IconButton } from "./index";
import type { Theme } from "../../app/theme";

export function ThemeToggle({ theme, toggleTheme }: { theme: Theme; toggleTheme(): void }) {
  const next = theme === "dark" ? "浅色" : "暗色";
  const Icon = theme === "dark" ? Sun : Moon;
  return <IconButton
    className="theme-toggle"
    icon={Icon}
    label={`切换到${next}主题`}
    onClick={toggleTheme}
    size="md"
  />;
}

