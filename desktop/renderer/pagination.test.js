const test = require('node:test');
const assert = require('node:assert/strict');
const { splitText, splitSegments, splitLines } = require('./pagination.js');

test('splits a long chapter into bounded, ordered pages', () => {
  const text = '第一段。'.repeat(80);
  const pages = splitText(text, 120);
  assert.ok(pages.length > 2);
  assert.equal(pages.join(''), text);
  assert.ok(pages.every((page) => page.length <= 120));
});

test('breaks the current page into line-sized chunks', () => {
  const lines = splitLines('这是第一行。这里是第二行。这里是第三行。', 8);
  assert.ok(lines.length >= 3);
  assert.ok(lines.every((line) => line.length <= 8));
});

test('keeps source offsets while splitting a chapter block', () => {
  const source = `第一段。${'正文'.repeat(20)}。第二段。`;
  const segments = splitSegments(source, 12);
  assert.ok(segments.length > 1);
  assert.equal(segments[0].start, 0);
  for (let index = 1; index < segments.length; index += 1) {
    assert.ok(segments[index].start >= segments[index - 1].end);
  }
  assert.equal(segments.map((segment) => segment.text).join(''), source);
});
