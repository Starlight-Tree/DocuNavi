import assert from 'node:assert/strict';
import { test } from 'node:test';
import { authConfigProblem } from './authConfig.ts';

const ok = {
  BETTER_AUTH_SECRET: 'x'.repeat(40),
  BETTER_AUTH_URL: 'https://auth.example.test',
  GOOGLE_CLIENT_ID: 'id',
  GOOGLE_CLIENT_SECRET: 'secret',
};

test('全部そろっていれば問題なし', () => {
  assert.equal(authConfigProblem(ok), null);
});

test('未設定・空白だけの変数名を挙げる（値は出さない）', () => {
  assert.equal(authConfigProblem({ ...ok, BETTER_AUTH_SECRET: undefined }), 'missing: BETTER_AUTH_SECRET');
  assert.equal(authConfigProblem({ ...ok, GOOGLE_CLIENT_ID: '  ', GOOGLE_CLIENT_SECRET: '' }), 'missing: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET');
});

test('公開されている既定の Secret は通さない', () => {
  const problem = authConfigProblem({ ...ok, BETTER_AUTH_SECRET: 'better-auth-secret-123456789' });
  assert.equal(problem, 'BETTER_AUTH_SECRET is the public default value');
});
