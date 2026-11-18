// Better Auth の設定値の検査。依存を持たない（node:test で単体に試せる）。
// Better Auth は BETTER_AUTH_SECRET が無いと、本番（NODE_ENV=production）以外では公開されている既定の Secret で動く。
// Worker には NODE_ENV が無いので、設定漏れに気づけない。ここで先に止める（値は出さず、変数名だけ返す）。

export interface AuthConfigValues {
  BETTER_AUTH_SECRET?: string;
  BETTER_AUTH_URL?: string;
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
}

const REQUIRED = ['BETTER_AUTH_SECRET', 'BETTER_AUTH_URL', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'] as const;
/** Better Auth のソースに書かれている既定の Secret の接頭辞（公開されている値）。 */
const DEFAULT_SECRET_PREFIX = 'better-auth-secret';

/** 問題があれば、変数名だけを含む説明を返す（値は含めない）。問題が無ければ null。 */
export function authConfigProblem(env: AuthConfigValues): string | null {
  const missing = REQUIRED.filter((name) => (env[name] ?? '').trim() === '');
  if (missing.length > 0) return `missing: ${missing.join(', ')}`;
  if ((env.BETTER_AUTH_SECRET ?? '').trim().startsWith(DEFAULT_SECRET_PREFIX)) {
    return 'BETTER_AUTH_SECRET is the public default value';
  }
  return null;
}
