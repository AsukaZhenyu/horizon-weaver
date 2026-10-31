// Machine-local paths for the Terminal-Bench evaluation harness (Spine runner).
//
// Mirror of `paths.py`: the same environment variables, the same defaults, so
// the Python and Node halves of one run always agree on where things are.
// Overrides are recorded in each case's run-config.json.
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const env = process.env;
const repoRoot = resolve(here, '../../..');
const cache = resolve(env.HW_CACHE || resolve(repoRoot, '.cache'));

export const paths = {
  repoRoot,
  cache,
  tbHome: resolve(env.HW_TB_HOME || resolve(cache, 'terminal-bench')),
  taskRoot: resolve(env.HW_TB_HOME || resolve(cache, 'terminal-bench'), 'original-tasks'),
  runs: resolve(env.HW_RUNS || resolve(cache, 'runs')),
  roundDir: resolve(env.HW_ROUND_DIR || resolve(repoRoot, 'experiments/tb-eval/config')),
  spineSrc: resolve(env.HW_SPINE_SRC || resolve(cache, 'upstream/spine-plugin')),
  dockerConfig: resolve(env.HW_DOCKER_CONFIG || resolve(cache, 'docker-config')),
  python: env.HW_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'),
  spineVolume: env.HW_SPINE_VOLUME || 'week8-spine-linux-20261030',
};

export const envOverrides = Object.fromEntries(
  [
    'HW_TB_HOME', 'HW_CACHE', 'HW_RUNS', 'HW_ROUND_DIR', 'HW_PYTHON',
    'HW_SPINE_VOLUME', 'HW_SPINE_SRC', 'HW_DOCKER_CONFIG', 'HW_MODEL_GGUF',
  ].filter((k) => env[k]).map((k) => [k, env[k]]),
);

// The path set a run was produced with; written into each case's source.json.
export const resolvedPaths = {
  repoRoot,
  tbHome: paths.tbHome,
  runs: paths.runs,
  roundDir: paths.roundDir,
  spineSrc: paths.spineSrc,
  python: paths.python,
  spineVolume: paths.spineVolume,
  envOverrides,
};
