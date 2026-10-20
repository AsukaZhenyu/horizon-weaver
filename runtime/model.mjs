// REST transport only: context selection and interpretation belong to components.
export function openAICompatible({ baseURL, model, apiKey, maxTokens = 2048 }) {
  return async (messages, { signal }) => {
    const response = await fetch(`${baseURL.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({ model, messages, max_tokens: maxTokens, temperature: 0 }),
    });
    if (!response.ok) throw new Error(`Model HTTP ${response.status}`);
    const body = await response.json();
    const content = body.choices?.[0]?.message?.content;
    if (typeof content !== 'string') throw new Error('Model returned no text content');
    return { content, usage: body.usage ?? null, finishReason: body.choices[0].finish_reason ?? null };
  };
}
