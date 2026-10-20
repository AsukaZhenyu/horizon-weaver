import { isJsonObject } from './protocol.js';
import type { ModelAdapter } from './protocol.js';
import { parseJson } from './values.js';

export type ModelConfiguration = { baseURL: string; model: string; apiKey?: string; maxTokens?: number };

// REST transport only: context selection and interpretation belong to components.
export function openAICompatible({ baseURL, model, apiKey, maxTokens = 2048 }: ModelConfiguration): ModelAdapter {
  return async (messages, { signal }) => {
    const response = await fetch(`${baseURL.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({ model, messages, max_tokens: maxTokens, temperature: 0 }),
    });
    if (!response.ok) throw new Error(`Model HTTP ${response.status}`);
    const body = parseJson(await response.text());
    const choices = isJsonObject(body) ? body['choices'] : undefined;
    const choice = Array.isArray(choices) ? choices[0] : undefined;
    const message = isJsonObject(choice) ? choice['message'] : undefined;
    if (!isJsonObject(body) || !isJsonObject(choice) || !isJsonObject(message) || typeof message['content'] !== 'string') {
      throw new Error('Model returned no text content');
    }
    return { content: message['content'], usage: body['usage'] ?? null, finishReason: choice['finish_reason'] ?? null };
  };
}
