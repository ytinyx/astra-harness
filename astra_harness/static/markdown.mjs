// A deliberately small Markdown dialect: no raw HTML, images, embedded content, or extensions.
// Every leaf is created with textContent; unrecognized syntax remains readable text.
export function safeHref(value) {
  if (typeof value !== 'string' || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function inlineTokens(text) {
  const result = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) result.push({type: 'text', text: text.slice(offset, match.index)});
    const token = match[0];
    if (token.startsWith('`')) result.push({type: 'code', text: token.slice(1, -1)});
    else if (token.startsWith('**')) result.push({type: 'strong', text: token.slice(2, -2)});
    else if (token.startsWith('*')) result.push({type: 'em', text: token.slice(1, -1)});
    else {
      const split = token.lastIndexOf(']('), href = safeHref(token.slice(split + 2, -1));
      result.push(href ? {type: 'link', text: token.slice(1, split), href} : {type: 'text', text: token});
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) result.push({type: 'text', text: text.slice(offset)});
  return result;
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
}

export function parseMarkdown(input) {
  const lines = String(input || '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let index = 0;
  const startsBlock = line => /^(#{1,6}\s|```|~~~|>\s?|\s*[-*+]\s|\s*\d+\.\s|---+$)/.test(line);
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const fence = line.match(/^(```|~~~)([^\s]*)\s*$/);
    if (fence) {
      const code = []; index++;
      while (index < lines.length && !lines[index].startsWith(fence[1])) code.push(lines[index++]);
      if (index < lines.length) index++;
      blocks.push({type: 'code', language: fence[2], text: code.join('\n')}); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { blocks.push({type: 'heading', level: heading[1].length, text: heading[2]}); index++; continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { blocks.push({type: 'hr'}); index++; continue; }
    if (index + 1 < lines.length && line.includes('|') && cells(lines[index + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) {
      const header = cells(line), rows = []; index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(cells(lines[index++]));
      blocks.push({type: 'table', header, rows}); continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ''));
      blocks.push({type: 'quote', text: quote.join('\n')}); continue;
    }
    const bullet = line.match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
    if (bullet) {
      const ordered = /\d/.test(bullet[1]), items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
        if (!item || /\d/.test(item[1]) !== ordered) break;
        items.push(item[2]); index++;
      }
      blocks.push({type: 'list', ordered, items}); continue;
    }
    const paragraph = [line]; index++;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index])) {
      if (index + 1 < lines.length && lines[index].includes('|') && cells(lines[index + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) break;
      paragraph.push(lines[index++]);
    }
    blocks.push({type: 'paragraph', text: paragraph.join('\n')});
  }
  return blocks;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function inline(element, text) {
  for (const token of inlineTokens(text)) {
    if (token.type === 'text') element.append(document.createTextNode(token.text));
    else {
      const item = node(token.type === 'link' ? 'a' : token.type, token.text);
      if (token.type === 'link') { item.href = token.href; item.target = '_blank'; item.rel = 'noopener noreferrer'; }
      element.append(item);
    }
  }
  return element;
}

export function renderMarkdown(container, text) {
  const fragment = document.createDocumentFragment();
  for (const block of parseMarkdown(text)) {
    let element;
    if (block.type === 'code') {
      element = node('div', undefined, 'code-block');
      const bar = node('div', undefined, 'code-toolbar'), button = node('button', '复制', 'copy-code');
      button.type = 'button';
      button.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(block.text); button.textContent = '已复制'; }
        catch { button.textContent = '请选中文本复制'; }
      });
      bar.append(node('span', block.language || '代码'), button);
      const pre = node('pre'); pre.append(node('code', block.text)); element.append(bar, pre);
    } else if (block.type === 'table') {
      element = node('div', undefined, 'table-scroll');
      const table = node('table'), head = node('thead'), body = node('tbody'), row = node('tr');
      for (const cell of block.header) row.append(inline(node('th'), cell));
      head.append(row);
      for (const cells of block.rows) {
        const tr = node('tr');
        for (let index = 0; index < block.header.length; index++) tr.append(inline(node('td'), cells[index] || ''));
        body.append(tr);
      }
      table.append(head, body); element.append(table);
    } else if (block.type === 'list') {
      element = node(block.ordered ? 'ol' : 'ul');
      for (const item of block.items) element.append(inline(node('li'), item));
    } else if (block.type === 'hr') element = node('hr');
    else element = inline(node(block.type === 'heading' ? `h${block.level}` : block.type === 'quote' ? 'blockquote' : 'p'), block.text);
    fragment.append(element);
  }
  container.replaceChildren(fragment);
}
