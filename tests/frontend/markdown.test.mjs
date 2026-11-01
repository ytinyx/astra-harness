import test from 'node:test';
import assert from 'node:assert/strict';
import {safeHref, inlineTokens, parseMarkdown, renderMarkdown} from '../../astra_harness/static/markdown.mjs';

test('allows only explicitly navigable HTTP(S) links', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,test', 'file:///private', '//example.com', 'java\nscript:alert(1)', 'https://a.com\n@b.com']) assert.equal(safeHref(url), null);
  assert.equal(safeHref('https://example.com/path?q=one'), 'https://example.com/path?q=one');
});

test('inline formatting leaves raw HTML and unsafe links as text', () => {
  const tokens = inlineTokens('<img src=x onerror=alert(1)> **加粗** [坏链接](javascript:test)');
  assert.equal(tokens[0].text, '<img src=x onerror=alert(1)> ');
  assert.equal(tokens[1].type, 'strong');
  assert.equal(tokens.at(-1).type, 'text');
  assert.equal(tokens.some(token => token.type === 'link'), false);
});

test('heading, table, list, quote, and fenced code stay distinct', () => {
  const blocks = parseMarkdown('# 待办\n\n| 任务 | 人员 |\n| --- | --- |\n| 编码 | 小明 |\n\n- 测试\n- 发布\n\n> 备注\n\n```python\nprint("<script>")\n```');
  assert.deepEqual(blocks.map(block => block.type), ['heading', 'table', 'list', 'quote', 'code']);
  assert.deepEqual(blocks[1].rows, [['编码', '小明']]);
  assert.equal(blocks[4].text, 'print("<script>")');
});

test('an unfinished streaming code block can be rendered safely', () => {
  const blocks = parseMarkdown('```json\n{"value": "<script>"');
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, 'code');
  assert.equal(blocks[0].text, '{"value": "<script>"');
});

test('renderer creates only safe elements and puts model text into text nodes', () => {
  class Element {
    constructor(tag) { this.tag = tag; this.children = []; this._text = ''; }
    set textContent(value) { this._text = String(value); }
    get textContent() { return this._text; }
    set innerHTML(value) { throw new Error('HTML injection is forbidden'); }
    append(...values) { this.children.push(...values); }
    replaceChildren(...values) { this.children = values; }
    addEventListener() {}
  }
  const previous = globalThis.document;
  globalThis.document = {createElement: tag => new Element(tag), createDocumentFragment: () => new Element('fragment'), createTextNode: text => Object.assign(new Element('#text'), {_text: text})};
  try {
    const container = new Element('div');
    renderMarkdown(container, '# 标题\n\n<img src=x onerror=alert(1)>\n\n[链接](https://example.com)\n\n```html\n<script>alert(1)</script>\n```');
    const flattened = [];
    const walk = value => { flattened.push(value); for (const child of value.children) walk(child); };
    walk(container);
    assert.equal(flattened.some(value => ['img', 'script', 'iframe'].includes(value.tag)), false);
    const link = flattened.find(value => value.tag === 'a');
    assert.equal(link.href, 'https://example.com/');
    assert.equal(link.rel, 'noopener noreferrer');
    assert.ok(flattened.some(value => value.textContent === '<script>alert(1)</script>'));
  } finally { globalThis.document = previous; }
});
