export class ApiClient {
  constructor(
    private base: string,
    private token: string,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(this.base + path, {
      ...init,
      headers: {
        "content-type": "application/json",
        "x-token": this.token,
        ...(init.headers ?? {}),
      },
    });
    const text = await res.text();
    let data: unknown = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { error: text };
    }
    if (!res.ok) {
      const body = data as { detail?: unknown; error?: string };
      const detail = body.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((d: { msg?: string }) => d?.msg ?? "").join("; ")
            : (body.error ?? `HTTP ${res.status}`);
      throw new Error(message);
    }
    return data as T;
  }

  get<T>(path: string): Promise<T> {
    return this.request<T>(path);
  }

  /** URL живого превью окна потока: <img> не умеет отправлять x-token. */
  previewUrl(threadId: number, tick = 0): string {
    const token = encodeURIComponent(this.token);
    return `${this.base}/api/threads/${threadId}/preview?token=${token}&t=${tick}`;
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  put<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, { method: "PUT", body: JSON.stringify(body) });
  }

  delete<T>(path: string): Promise<T> {
    return this.request<T>(path, { method: "DELETE" });
  }
}
