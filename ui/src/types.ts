export type LogEvent = {
  ts: string;
  thread: number | null;
  level: "info" | "success" | "warn" | "error" | string;
  msg: string;
};

export type ThreadState = {
  id: number;
  status: string;
  task_id: number | null;
  browser_visible: boolean;
  sku?: string;
  profile?: string | null;
  profile_kind?: string | null;
  proxy?: string | null;
  hwnd?: number | null;
};

export type Stats = {
  pending: number;
  done: number;
  errors: number;
  paused: number;
  total: number;
  threads_active: number;
  run_state: string;
  ok: number;
};

export type RunRow = {
  id: number;
  task_id: number | null;
  thread_id: number | null;
  result: string;
  position: number | null;
  cart_added: number;
  liked: number;
  sku: string | null;
  query: string | null;
  marketplace: string | null;
  started_at: string;
  finished_at: string;
  error_msg: string | null;
};

export type QuickActions = {
  cart_from_search: boolean;
  goto_cart: boolean;
  view_photos: boolean;
  favorite: boolean;
};

export type Settings = {
  general: {
    fingerprint_key: string;
    google_credentials: string;
    spreadsheet_id: string;
    sheet_tasks: string;
    sheet_accounts: string;
    sheet_orders: string;
    proxies_file: string;
    sheet_xlsx: string;
    dir_active: string;
    dir_done: string;
    dir_noauth: string;
    threads: number;
    proxy_max_threads: number;
    thread_start_interval_sec: number;
    task_gap_min_sec: number;
    task_gap_max_sec: number;
    repeat_limit: number;
    error_limit: number;
    sync_enabled: boolean;
    sync_interval_sec: number;
    quick_actions: QuickActions;
  };
  view: {
    profile_use_pct: number;
    like_pct: number;
    cart_pct: number;
    other_sellers_pct: number;
    see_also_pct: number;
    compare_pct: number;
    review_like_pct: number;
    max_search_pages: number;
    card_time_sec: [number, number];
    reviews_time_sec: [number, number];
  };
  browser: {
    provider: "vanilla" | "camoufox" | "cdp";
    cdp_endpoint: string;
    channel: "auto" | "chrome" | "edge" | "chromium";
    headless: boolean;
    locale: string;
    timezone: string;
    enabled: boolean;
    check_url: string;
  };
  license: { key: string; server: string };
};

export type LicenseStatus = {
  enabled: boolean;
  valid: boolean;
  mode: string;
  hwid: string;
  server: string;
  key: string;
  subject: string | null;
  expires: string | null;
  days_left: number | null;
  features: string[];
  error: string | null;
  warning: string | null;
  offline_until: string | null;
  last_check: string | null;
};

export type SyncState = {
  backend: string;
  mode: string;
  enabled: boolean;
  last_pull: string | null;
  last_push: string | null;
  pulled: number;
  pushed: number;
  last_error: string | null;
  interval: number;
};

export type SchedulerState = {
  enabled: boolean;
  interval_min: number;
  last_run: string | null;
  next_run: string | null;
  last_result: string | null;
  run_state: string;
};

export type Report = {
  tasks: { pending: number; done: number; errors: number; paused: number; total: number };
  runs: { total: number; ok: number; errors: number; today: number };
  profiles: { active: number; done: number; noauth: number; busy: number };
  proxies: { total: number; busy: number; invalid: number };
  sessions: { threads: number; manual: number };
  processes: { chromium: number; chromium_mb: number; backend_mb: number };
  disk: { db_mb: number; artifacts_mb: number; artifacts_files: number };
  scheduler: SchedulerState;
};

export type AppInfo = { apiBase: string; token: string; wsUrl: string };

export type ManualItem = {
  id: string;
  created_at: string;
  kind: string;
  profile: string;
  profile_kind: string;
  proxy: string;
  pid: number | null;
  hwnd: number | null;
  visible: boolean;
  url: string;
};

export type ProfileEntry = { name: string; path: string };
export type ProfileLists = {
  active: ProfileEntry[];
  done: ProfileEntry[];
  noauth: ProfileEntry[];
};
