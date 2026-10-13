const loginRedirect = () => {
  const next = `${window.location.pathname}${window.location.hash}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
};

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = "ApiError"; }
}

async function parseResponse<T>(response: Response): Promise<T> {
  const type = response.headers.get("Content-Type") || "";
  const data = type.includes("application/json")
    ? await response.json() as T & { error?: string }
    : { error: await response.text() } as T & { error?: string };
  if (response.status === 401) { loginRedirect(); throw new Error("登录状态已失效"); }
  if (!response.ok) throw new ApiError(data.error || `HTTP ${response.status}`, response.status);
  return data;
}

export async function getJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  return parseResponse<T>(await fetch(path, init));
}

export async function sendJson<T>(path: string, method: "POST" | "DELETE", body: unknown): Promise<T> {
  return parseResponse<T>(await fetch(path, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }));
}

export const postJson = <T>(path: string, body: unknown) => sendJson<T>(path, "POST", body);
export const deleteJson = <T>(path: string, body: unknown) => sendJson<T>(path, "DELETE", body);

export async function uploadFormData<T>(path: string, body: FormData): Promise<T> {
  return parseResponse<T>(await fetch(path, { method: "POST", body }));
}

export async function subscribeNdjson<T>(path: string, onEvent: (event: T) => void, signal?: AbortSignal) {
  await readNdjson(await fetch(path, { signal }), onEvent);
}

async function readNdjson<T>(response: Response, onEvent: (event: T) => void) {
  if (response.status === 401) { loginRedirect(); throw new ApiError("登录状态已失效", 401); }
  if (!response.ok) throw new ApiError((await response.text()) || `HTTP ${response.status}`, response.status);
  if (!response.body) throw new Error("当前浏览器不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as T);
    if (done) break;
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer) as T);
  } catch (reason) {
    await reader.cancel().catch(() => undefined);
    throw reason;
  } finally { reader.releaseLock(); }
}
