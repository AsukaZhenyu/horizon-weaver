import { readFileSync } from 'node:fs';
import { createHost } from '../runtime/host.mjs';
import { openAICompatible } from '../runtime/model.mjs';

if (!process.env.HW_BASE_URL || !process.env.HW_MODEL) throw new Error('Set HW_BASE_URL (including /v1) and HW_MODEL');
const host = await createHost({
  root: '.horizon/model-demo',
  model: openAICompatible({ baseURL: process.env.HW_BASE_URL, model: process.env.HW_MODEL, apiKey: process.env.HW_API_KEY }),
});
try {
  const entry = host.kernel.define(readFileSync(new URL('../seeds/generate.py', import.meta.url), 'utf8'), { grants: ['model', 'install'] });
  const result = await host.kernel.run(entry, { instruction: 'Return the sum of numbers as a JSON number.', numbers: [2, 5, -1] }, { timeoutMs: 60000 });
  console.log(JSON.stringify(result, null, 2));
  if (result.status !== 'submitted') process.exitCode = 1;
} finally { await host.dispose(); }
