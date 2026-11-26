import type { AppInfo } from "./types";

declare global {
  interface Window {
    pf: {
      info: () => Promise<AppInfo>;
      pickExcel: () => Promise<string | null>;
    };
  }
}

export {};
