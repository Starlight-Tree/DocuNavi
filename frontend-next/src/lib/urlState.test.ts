import { describe, expect, it } from 'vitest';
import { afterEach } from 'vitest';
import { buildSearch, currentStateSearch, parseUrlState, replaceUrlState } from './urlState';

describe('parseUrlState', () => {
  it('何も無ければチャット', () => {
    expect(parseUrlState('')).toEqual({ tab: 'chat', doc: null, view: null, item: null, page: null, collection: null });
  });

  it('資料・表示・項まで読む', () => {
    expect(parseUrlState('?tab=documents&doc=3&view=graph&item=a11p1')).toEqual({
      tab: 'documents',
      doc: 3,
      view: 'graph',
      item: 'a11p1',
      page: null,
      collection: null,
    });
  });

  it('PDF のページは、表示が pdf のときだけ読む', () => {
    expect(parseUrlState('?tab=documents&doc=3&view=pdf&page=2').page).toBe(2);
    expect(parseUrlState('?tab=documents&doc=3&view=text&page=2').page).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&page=2').page).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&view=pdf&page=0').page).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&view=pdf&page=abc').page).toBeNull();
  });

  it('不正な値は、安全な既定値にする', () => {
    expect(parseUrlState('?tab=admin').tab).toBe('chat');
    expect(parseUrlState('?tab=documents&doc=abc').doc).toBeNull();
    expect(parseUrlState('?tab=documents&doc=0').doc).toBeNull();
    expect(parseUrlState('?tab=documents&doc=-1').doc).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&view=evil').view).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&view=graph&item=<script>').item).toBeNull();
  });

  it('意味が通らない組み合わせは落とす（チャットに資料は無く、資料が無ければ表示も項も無い）', () => {
    expect(parseUrlState('?tab=chat&doc=3&view=graph&item=a1p1')).toEqual({
      tab: 'chat',
      doc: null,
      view: null,
      item: null,
      page: null,
      collection: null,
    });
    expect(parseUrlState('?tab=documents&view=graph&item=a1p1')).toMatchObject({ view: null, item: null });
    expect(parseUrlState('?tab=documents&doc=3&view=pdf&item=a1p1').item).toBeNull();
  });
});

describe('buildSearch', () => {
  it('必要なキーだけ更新し、この画面が扱わないパラメータは残す', () => {
    expect(buildSearch('?utm=x&tab=documents', { doc: 3 })).toBe('?utm=x&tab=documents&doc=3');
  });

  it('資料を閉じると、表示と項も外れる', () => {
    expect(buildSearch('?tab=documents&doc=3&view=graph&item=a1p1', { doc: null })).toBe('?tab=documents');
  });

  it('表示を変えると、関係図の項は外れる', () => {
    expect(buildSearch('?tab=documents&doc=3&view=graph&item=a1p1', { view: 'pdf' })).toBe(
      '?tab=documents&doc=3&view=pdf',
    );
  });

  it('ページを指定して PDF を開くと、page が付き、別の表示にすると外れる', () => {
    expect(buildSearch('?tab=chat', { tab: 'documents', doc: 3, view: 'pdf', page: 2 })).toBe(
      '?tab=documents&doc=3&view=pdf&page=2',
    );
    expect(buildSearch('?tab=documents&doc=3&view=pdf&page=2', { view: 'text' })).toBe('?tab=documents&doc=3&view=text');
  });

  it('チャットに切り替えると、資料の状態は外れる', () => {
    expect(buildSearch('?tab=documents&doc=3&view=text', { tab: 'chat' })).toBe('?tab=chat');
  });
});

describe('一覧の絞り込み（collection）', () => {
  it('資料の一覧を見ているときだけ読む（チャットでも、資料を開いていても無い）', () => {
    expect(parseUrlState('?tab=documents&collection=cafe').collection).toBe('cafe');
    expect(parseUrlState('?tab=chat&collection=cafe').collection).toBeNull();
    expect(parseUrlState('?tab=documents&doc=3&collection=cafe').collection).toBeNull();
  });

  it('名前の形が合わない値は捨てる', () => {
    expect(parseUrlState('?tab=documents&collection=a%20b').collection).toBeNull();
    expect(parseUrlState('?tab=documents&collection=').collection).toBeNull();
    expect(parseUrlState(`?tab=documents&collection=${'x'.repeat(41)}`).collection).toBeNull();
  });

  it('絞り込みを URL に書き、資料を開くと外し、戻す（null）と消える', () => {
    expect(buildSearch('?tab=documents', { collection: 'guides' })).toBe('?tab=documents&collection=guides');
    expect(buildSearch('?tab=documents&collection=guides', { doc: 3 })).toBe('?tab=documents&doc=3');
    expect(buildSearch('?tab=documents&collection=guides', { collection: null })).toBe('?tab=documents');
  });
});

describe('currentStateSearch / replaceUrlState', () => {
  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('チャットが既定のとき（と、画面の指定が無いとき）は null', () => {
    window.history.replaceState(null, '', '/?code=abc');
    expect(currentStateSearch()).toBeNull();
  });

  it('資料の画面は、整えたクエリで返す。この画面が扱わないパラメータは含めない', () => {
    window.history.replaceState(null, '', '/?tab=documents&doc=3&view=graph&item=a11p1&code=secret&x=1');
    expect(currentStateSearch()).toBe('?tab=documents&doc=3&view=graph&item=a11p1');
  });

  it('replaceUrlState は、履歴を増やさずに URL を書き換え、変更を通知する', () => {
    const length = window.history.length;
    let notified = 0;
    const onChange = () => (notified += 1);
    window.addEventListener('docunavi:urlchange', onChange);
    replaceUrlState('?tab=documents&doc=3&view=pdf&page=2');
    window.removeEventListener('docunavi:urlchange', onChange);

    expect(window.location.search).toBe('?tab=documents&doc=3&view=pdf&page=2');
    expect(window.history.length).toBe(length);
    expect(notified).toBe(1);
  });

  it('replaceUrlState は、預けられた文字列も整えてから書く（不正な値・未知のパラメータは落ちる）', () => {
    replaceUrlState('?tab=documents&doc=abc&evil=1');
    expect(window.location.search).toBe('?tab=documents');
  });
});
