export interface ModelProfileSettings {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  has_api_key: boolean;
  wire_api: string;
  user_agent?: string;
  max_tokens_param?: string;
  context_window_tokens?: number | null;
  max_input_tokens?: number | null;
  max_output_tokens?: number | null;
  output_reserve_tokens?: number | null;
  tokenizer_model?: string;
  bpe_tokenizer_enabled?: boolean;
  supports_thinking?: boolean;
  thinking_param?: string;
  thinking_value?: unknown;
  fallbacks?: string[];
  [key: string]: unknown;
}

export interface ModelSettingsResponse {
  revision: number;
  configured: boolean;
  error?: string;
  default_profile: string;
  profiles: ModelProfileSettings[];
  routes: Record<string, string[]>;
  provider_defaults: Record<string, { base_url: string; model: string; max_tokens_param: string }>;
  route_purposes: string[];
}

export interface ModelSettingsPayload {
  revision: number;
  default_profile: string;
  profiles: Record<string, unknown>[];
  routes: Record<string, string[]>;
}

export interface ModelProbeResponse {
  status: "ok" | "unsupported" | "authentication_failed" | "unavailable" | "invalid_response";
  message: string;
  elapsed_ms: number;
  models: string[];
  truncated: boolean;
}

export interface ModelRoutePreview {
  purpose: string;
  source: "explicit" | "alias" | "default" | "default_profile";
  source_purpose: string | null;
  profiles: { name: string; provider: string; model: string; source: "route" | "profile_fallback"; fallback_of?: string }[];
}

export interface ModelSettingsPreviewResponse {
  revision: number;
  configured: boolean;
  routes: ModelRoutePreview[];
}
