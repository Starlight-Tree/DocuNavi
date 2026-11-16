import { describe, expect, it } from 'vitest';
import { DISPLAY_NAME_MAX, MSG_NAME_CONTROL, MSG_NAME_EMPTY, MSG_NAME_TOO_LONG, displayNameLength, validateDisplayName } from './displayName';

describe('validateDisplayName', () => {
  it('前後の空白を除いて通す。日本語・絵文字・間の空白もそのまま', () => {
    expect(validateDisplayName('  山田 太郎 ')).toEqual({ ok: true, name: '山田 太郎' });
    expect(validateDisplayName('たろう🌳')).toEqual({ ok: true, name: 'たろう🌳' });
    expect(validateDisplayName('👨‍👩‍👧')).toEqual({ ok: true, name: '👨‍👩‍👧' }); // ZWJ（制御文字ではない）
  });

  it('空・空白だけは通さない', () => {
    expect(validateDisplayName('')).toEqual({ ok: false, message: MSG_NAME_EMPTY });
    expect(validateDisplayName(' 　 ')).toEqual({ ok: false, message: MSG_NAME_EMPTY });
  });

  it('30 文字までは通し、31 文字は通さない（コードポイント単位）', () => {
    expect(validateDisplayName('あ'.repeat(DISPLAY_NAME_MAX)).ok).toBe(true);
    expect(validateDisplayName('🌳'.repeat(DISPLAY_NAME_MAX)).ok).toBe(true);
    expect(validateDisplayName('あ'.repeat(DISPLAY_NAME_MAX + 1)).ok).toBe(false);
    expect(validateDisplayName('あ'.repeat(DISPLAY_NAME_MAX + 1))).toEqual({ ok: false, message: MSG_NAME_TOO_LONG });
  });

  it('改行・タブなどの制御文字は通さない', () => {
    expect(validateDisplayName('山田\n太郎')).toEqual({ ok: false, message: MSG_NAME_CONTROL });
    expect(validateDisplayName('山田\t太郎')).toEqual({ ok: false, message: MSG_NAME_CONTROL });
    expect(validateDisplayName('山田\u0000')).toEqual({ ok: false, message: MSG_NAME_CONTROL });
  });

  it('displayNameLength は前後の空白を除いた文字数', () => {
    expect(displayNameLength('  山田 太郎 ')).toBe(5);
  });
});
