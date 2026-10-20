import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, test } from 'node:test';

const directory = mkdtempSync(join(tmpdir(), 'pipmind-release-gates-'));
const bash = process.platform === 'win32'
  ? join(process.env.ProgramFiles, 'Git/bin/bash.exe') : 'bash';
function git(...args) {
  const result = spawnSync('git', args, { cwd: directory, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}
git('init', '-q');
git('config', 'user.name', 'Synthetic release check');
git('config', 'user.email', 'release@example.invalid');
function commit(path, value) {
  const file = join(directory, path);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, value);
  git('add', path);
  git('commit', '-qm', 'Synthetic release input');
  return git('rev-parse', 'HEAD');
}
const base = commit('docs/test.md', 'base');
const site = commit('web/index.html', 'site');
const docs = commit('docs/test.md', 'docs');
const backend = commit('pipmind/shadow/test.py', 'backend');
const pipeline = commit('.github/workflows/deploy.yml', 'pipeline');
const gateLogic = commit('.github/scripts/landing-changes.sh', 'gate');
const source = readFileSync(fileURLToPath(new URL('../.github/scripts/landing-changes.sh', import.meta.url)), 'utf8');
const script = join(directory, 'gate.sh');
writeFileSync(script, `
function gh() {
  if test "$PIPMIND_TEST_MODE" = api-error; then return 17; fi
  printf '%s' "$PIPMIND_TEST_BASE"
}
function git() {
  if test "$PIPMIND_TEST_MODE" = diff-error && test "$1" = diff; then return 7; fi
  command git "$@"
}
${source}`);

const cases = [
  ['site change releases', base, site, 'ok', 'true'],
  ['queued site change followed by docs still releases', base, docs, 'ok', 'true'],
  ['docs after a successful site release do not release', site, docs, 'ok', 'false'],
  ['private backend alone does not release', docs, backend, 'ok', 'false'],
  ['delivery workflow changes release', backend, pipeline, 'ok', 'true'],
  ['change detection changes release', pipeline, gateLogic, 'ok', 'true'],
  ['first run releases after CI', '', gateLogic, 'ok', 'true'],
  ['API error refuses a release decision', base, gateLogic, 'api-error', null],
  ['invalid baseline refuses a release decision', 'bad', gateLogic, 'ok', null],
  ['unknown commit refuses a release decision', '1'.repeat(40), gateLogic, 'ok', null],
  ['divergent history refuses a release decision', gateLogic, base, 'ok', null],
  ['diff failure refuses a release decision', base, gateLogic, 'diff-error', null],
];
for (const [name, prior, current, mode, expected] of cases) {
  test(name, () => {
    const output = join(directory, 'output.txt');
    rmSync(output, { force: true });
    const result = spawnSync(bash, ['-e', script], {
      cwd: directory, encoding: 'utf8',
      env: { ...process.env, PIPMIND_TEST_BASE: prior, PIPMIND_TEST_MODE: mode,
        GITHUB_SHA: current, GITHUB_REPOSITORY: 'example/synthetic', GITHUB_OUTPUT: output },
    });
    assert.equal(result.error, undefined);
    const written = existsSync(output) ? readFileSync(output, 'utf8').trim() : '';
    if (expected === null) {
      assert.notEqual(result.status, 0);
      assert.equal(written, '');
    } else {
      assert.equal(result.status, 0, result.stderr);
      assert.equal(written, `changed=${expected}`);
    }
  });
}
after(() => {
  assert.ok(resolve(directory).startsWith(resolve(tmpdir()) + sep));
  assert.ok(directory.includes('pipmind-release-gates-'));
  rmSync(directory, { recursive: true, force: true });
});
