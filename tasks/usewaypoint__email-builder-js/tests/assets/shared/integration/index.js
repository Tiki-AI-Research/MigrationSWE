'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');
const childProcess = require('child_process');
const { pathToFileURL } = require('url');

const ASSET_ROOT = path.resolve(__dirname, '..', '..');
const INVENTORY = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'observations.json'), 'utf8')
);
const FIXTURES = JSON.parse(
  fs.readFileSync(path.join(ASSET_ROOT, 'fixtures', 'integration', 'fixtures.json'), 'utf8')
);

function resolveRepoRoot() {
  for (const name of ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO']) {
    const value = process.env[name];
    if (value) {
      return path.resolve(value);
    }
  }
  const defaultRepoRoot = path.resolve('/workspace/repo');
  if (fs.existsSync(path.join(defaultRepoRoot, 'packages', 'block-text', 'package.json'))) {
    return defaultRepoRoot;
  }
  const cwdRepoRoot = path.resolve(process.cwd());
  if (fs.existsSync(path.join(cwdRepoRoot, 'packages', 'block-text', 'package.json'))) {
    return cwdRepoRoot;
  }
  throw new Error('No repository root configured; set MIGRATION_REPO_ROOT (or an allowed alias)');
}

function makeRequire(repoRoot) {
  return Module.createRequire(path.join(repoRoot, 'package.json'));
}

function readText(filePath) {
  return fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf8') : '';
}

function readJson(filePath) {
  return JSON.parse(readText(filePath) || '{}');
}

function normalizeWhitespace(value) {
  return (value || '').replace(/\s+/g, ' ').trim();
}

function normalizeText(element) {
  if (!element) {
    return '';
  }
  const clone = element.cloneNode(true);
  clone.querySelectorAll('br').forEach((br) => br.replaceWith(element.ownerDocument.createTextNode(' ')));
  return normalizeWhitespace(clone.textContent || '');
}

function attributes(element) {
  if (!element) {
    return null;
  }
  return Array.from(element.attributes)
    .sort((left, right) => left.name.localeCompare(right.name))
    .reduce((result, attribute) => {
      result[attribute.name] = attribute.value;
      return result;
    }, {});
}

function attr(element, name) {
  if (!element || !element.hasAttribute(name)) {
    return null;
  }
  return element.getAttribute(name);
}

function uniqueSorted(values) {
  return Array.from(new Set(values)).sort();
}

function loadedModuleNames() {
  const names = new Set();
  for (const key of Object.keys(require.cache)) {
    const match = key.match(/node_modules[\\/]((?:@[^\\/]+\/)?[^\\/]+)/);
    if (match) {
      names.add(match[1]);
    }
  }
  return names;
}

function purgeModules(repoRoot) {
  const repoPrefix = repoRoot.endsWith(path.sep) ? repoRoot : repoRoot + path.sep;
  for (const key of Object.keys(require.cache)) {
    if (key.startsWith(repoPrefix) || /node_modules[\\/](marked|markdown-it|insane)[\\/]/.test(key)) {
      delete require.cache[key];
    }
  }
}

function ensureTsNode(repoRoot) {
  process.env.TS_NODE_TRANSPILE_ONLY = 'true';
  process.env.TS_NODE_COMPILER_OPTIONS = JSON.stringify({
    allowJs: true,
    esModuleInterop: true,
    jsx: 'react',
    module: 'commonjs',
    target: 'es2019',
  });
  const requirement = makeRequire(repoRoot);
  try {
    requirement('ts-node/register/transpile-only');
  } catch (error) {
    require('ts-node/register/transpile-only');
  }
}

function loadFile(repoRoot, relativePath) {
  return require(path.join(repoRoot, relativePath));
}

function packageVersion(repoRoot, name) {
  const requirement = Module.createRequire(path.join(repoRoot, 'packages', 'block-text', 'package.json'));
  try {
    const manifestPath = requirement.resolve(name + '/package.json');
    return JSON.parse(fs.readFileSync(manifestPath, 'utf8')).version || null;
  } catch (error) {
    return null;
  }
}

function canResolve(repoRoot, name) {
  const requirement = Module.createRequire(path.join(repoRoot, 'packages', 'block-text', 'package.json'));
  try {
    requirement.resolve(name);
    return true;
  } catch (error) {
    return false;
  }
}

function installMarkdownItRendererProbe(requirement, rendererProbe) {
  // The package's CommonJS and UMD entries have separate renderer classes.
  // A production import of either entry must be observed by the same probe.
  const observed = new Set();
  for (const specifier of ['markdown-it', 'markdown-it/dist/markdown-it.js']) {
    try {
      const MarkdownIt = requirement(specifier);
      const prototype = Object.getPrototypeOf(new MarkdownIt().renderer);
      if (observed.has(prototype)) continue;
      observed.add(prototype);
      const original = prototype.render;
      prototype.render = function observedRenderer() {
        const output = original.apply(this, arguments);
        if (!rendererProbe.marker) return output;
        rendererProbe.calls += 1;
        return output + '<span>' + rendererProbe.marker + '</span>';
      };
    } catch (error) {
      // A package version without this optional entry needs no probe there.
    }
  }
}

function createContext(repoRoot) {
  ensureTsNode(repoRoot);
  purgeModules(repoRoot);
  const requirement = makeRequire(repoRoot);
  const React = requirement('react');
  const server = requirement('react-dom/server');
  const JSDOM = requirement('jsdom').JSDOM;
  // Install before production imports, so cached/bound methods see the same
  // probe. The renderer seam also covers parse()+renderer.render() layouts.
  const rendererProbe = { marker: null, calls: 0 };
  if (canResolve(repoRoot, 'markdown-it')) {
    installMarkdownItRendererProbe(requirement, rendererProbe);
  }
  const emailModule = loadFile(repoRoot, 'packages/block-text/src/EmailMarkdown.tsx');
  const indexModule = loadFile(repoRoot, 'packages/block-text/src/index.tsx');

  const context = {
    repoRoot,
    requirement,
    React,
    server,
    JSDOM,
    rendererProbe,
    EmailMarkdown: emailModule.default || emailModule,
    Text: indexModule.Text,
    indexModule,
    emailModule,
    renderEmail(markdown, style) {
      return server.renderToStaticMarkup(
        React.createElement(context.EmailMarkdown, { markdown, style: style || {} })
      );
    },
    renderText(props, style) {
      return server.renderToStaticMarkup(
        React.createElement(context.Text, style === undefined ? { props } : { props, style })
      );
    },
    parse(html) {
      const dom = new JSDOM('<!doctype html><html><body>' + html + '</body></html>');
      const document = dom.window.document;
      return { document, root: document.body.firstElementChild, roots: Array.from(document.body.children) };
    },
  };
  return context;
}

function tableShape(document) {
  const table = document.querySelector('table');
  if (!table) {
    return { table: false, width: null, thead: 0, tbody: 0, tr: 0, th: 0, td: 0 };
  }
  return {
    table: true,
    width: table.getAttribute('width'),
    thead: document.querySelectorAll('thead').length,
    tbody: document.querySelectorAll('tbody').length,
    tr: document.querySelectorAll('tr').length,
    th: document.querySelectorAll('th').length,
    td: document.querySelectorAll('td').length,
  };
}

function eventAttributeNames(root) {
  const names = [];
  root.querySelectorAll('*').forEach((element) => {
    Array.from(element.attributes).forEach((attribute) => {
      if (attribute.name.toLowerCase().startsWith('on')) {
        names.push(attribute.name.toLowerCase());
      }
    });
  });
  return uniqueSorted(names);
}

function blockedSchemeValues(root) {
  const values = [];
  root.querySelectorAll('[href], [src]').forEach((element) => {
    for (const name of ['href', 'src']) {
      const value = element.getAttribute(name);
      if (value && /^\s*(?:javascript|data|ftp)\s*:/i.test(value)) {
        values.push(value);
      }
    }
  });
  return uniqueSorted(values);
}

function blockHtmlProjection(context) {
  const { Html } = loadFile(context.repoRoot, 'packages/block-html/src/index.tsx');
  const root = context.parse(context.server.renderToStaticMarkup(context.React.createElement(Html))).root;
  return { tag: root ? root.tagName.toLowerCase() : null, childElementCount: root ? root.childElementCount : null, text: normalizeWhitespace(root && root.textContent) };
}

function buildBlockComponentProjection(context, variant) {
  const z = context.requirement('zod');
  const buildBlockComponent = loadFile(context.repoRoot, 'packages/document-core/src/builders/buildBlockComponent.tsx').default;
  const BlockComponent = buildBlockComponent({
    SampleBlock: {
      schema: z.object({ text: z.string() }),
      Component: ({ text }) => context.React.createElement('div', null, text.toUpperCase()),
    },
  });
  const root = context.parse(
    context.server.renderToStaticMarkup(
      context.React.createElement(BlockComponent, { type: 'SampleBlock', data: { text: 'Test text!' } })
    )
  ).root;
  return variant === 'surface'
    ? { div: root ? (root.tagName.toLowerCase() === 'div' ? 1 : 0) + root.querySelectorAll('div').length : 0, text: normalizeWhitespace(root && root.textContent) }
    : { text: normalizeWhitespace(root && root.textContent) };
}

function buttonProjection(context) {
  const { Button } = loadFile(context.repoRoot, 'packages/block-button/src/index.tsx');
  const root = context.parse(context.server.renderToStaticMarkup(context.React.createElement(Button))).root;
  const anchor = root && root.querySelector('a');
  return { tag: root ? root.tagName.toLowerCase() : null, anchor: Boolean(anchor), target: attr(anchor, 'target') };
}

function columnsProjection(context, count) {
  const { ColumnsContainer } = loadFile(context.repoRoot, 'packages/block-columns-container/src/index.tsx');
  const columns = [
    context.React.createElement('span', { key: 'a' }, 'bread'),
    context.React.createElement('span', { key: 'b' }, 'tomato'),
    context.React.createElement('span', { key: 'c' }, 'lettuce'),
  ];
  const root = context.parse(
    context.server.renderToStaticMarkup(
      context.React.createElement(ColumnsContainer, { props: { columnsCount: count }, columns })
    )
  ).root;
  const text = normalizeWhitespace(root && root.textContent);
  return { td: root ? root.querySelectorAll('td').length : 0, hasBread: text.includes('bread'), hasTomato: text.includes('tomato'), hasLettuce: text.includes('lettuce') };
}

function dividerProjection(context, withProps) {
  const { Divider } = loadFile(context.repoRoot, 'packages/block-divider/src/index.tsx');
  const element = withProps
    ? context.React.createElement(Divider, {
        style: { padding: { top: 1, left: 2, bottom: 3, right: 4 }, backgroundColor: '#fff000' },
        props: { lineColor: '#444222', lineHeight: 10 },
      })
    : context.React.createElement(Divider);
  const root = context.parse(context.server.renderToStaticMarkup(element)).root;
  const hr = root && root.querySelector('hr');
  return {
    tag: root ? root.tagName.toLowerCase() : null,
    padding: root ? root.style.padding : null,
    borderTopWidth: hr ? hr.style.borderTopWidth : null,
  };
}

function headingProjection(context, withStyle) {
  const { Heading } = loadFile(context.repoRoot, 'packages/block-heading/src/index.tsx');
  const element = withStyle
    ? context.React.createElement(Heading, {
        style: {
          backgroundColor: '#444333',
          color: '#101010',
          fontFamily: 'HEAVY_SANS',
          fontWeight: 'normal',
          padding: { top: 15, bottom: 10, left: 24, right: 8 },
          textAlign: 'center',
        },
        props: { text: 'Hello world!', level: 'h1' },
      })
    : context.React.createElement(Heading);
  const root = context.parse(context.server.renderToStaticMarkup(element)).root;
  return {
    tag: root ? root.tagName.toLowerCase() : null,
    text: normalizeWhitespace(root && root.textContent),
    textAlign: root ? root.style.textAlign : null,
    padding: root ? root.style.padding : null,
  };
}

function spacerProjection(context, height) {
  const { Spacer } = loadFile(context.repoRoot, 'packages/block-spacer/src/index.tsx');
  const element = height === undefined
    ? context.React.createElement(Spacer)
    : context.React.createElement(Spacer, { props: { height } });
  const root = context.parse(context.server.renderToStaticMarkup(element)).root;
  return { tag: root ? root.tagName.toLowerCase() : null, height: root ? root.style.height : null };
}

function containerProjection(context) {
  const { Container } = loadFile(context.repoRoot, 'packages/block-container/src/index.tsx');
  const root = context.parse(context.server.renderToStaticMarkup(context.React.createElement(Container))).root;
  return { tag: root ? root.tagName.toLowerCase() : null };
}

function exportProjection(context) {
  const expected = {
    'block-avatar': ['AvatarPropsSchema', 'AvatarPropsDefaults', 'Avatar'],
    'block-button': ['ButtonPropsSchema', 'ButtonPropsDefaults', 'Button'],
    'block-columns-container': ['ColumnsContainerPropsSchema', 'ColumnsContainer'],
    'block-container': ['ContainerPropsSchema', 'Container'],
    'block-divider': ['DividerPropsSchema', 'DividerPropsDefaults', 'Divider'],
    'block-heading': ['HeadingPropsSchema', 'HeadingPropsDefaults', 'Heading'],
    'block-html': ['HtmlPropsSchema', 'Html'],
    'block-image': ['ImagePropsSchema', 'Image'],
    'block-spacer': ['SpacerPropsSchema', 'SpacerPropsDefaults', 'Spacer'],
    'block-text': ['TextPropsSchema', 'TextPropsDefaults', 'Text'],
  };
  const missing = [];
  for (const packageName of Object.keys(expected)) {
    const moduleExports = loadFile(context.repoRoot, 'packages/' + packageName + '/src/index.tsx');
    for (const name of expected[packageName]) {
      if (moduleExports[name] === undefined) {
        missing.push(packageName + ':' + name);
      }
    }
  }
  const documentCore = loadFile(context.repoRoot, 'packages/document-core/src/index.ts');
  for (const name of ['buildBlockComponent', 'buildBlockConfigurationSchema', 'buildBlockConfigurationDictionary']) {
    if (documentCore[name] === undefined) {
      missing.push('document-core:' + name);
    }
  }
  if (!loadFile(context.repoRoot, 'packages/block-text/src/EmailMarkdown.tsx').default) {
    missing.push('block-text:EmailMarkdown');
  }
  return { missing };
}

function textDefaultsProjection(context) {
  const defaults = context.parse(context.server.renderToStaticMarkup(context.React.createElement(context.Text))).root;
  return { tag: defaults ? defaults.tagName.toLowerCase() : null, text: normalizeWhitespace(defaults && defaults.textContent) };
}

function emptyVariantsProjection(context) {
  const variants = [
    { markdown: true, text: '' },
    { markdown: true, text: null },
    { text: '' },
    { text: null },
  ];
  return {
    variants: variants.map((props) => {
      const root = context.parse(context.server.renderToStaticMarkup(context.React.createElement(context.Text, { props }))).root;
      return { tag: root ? root.tagName.toLowerCase() : null, childElementCount: root ? root.childElementCount : null, text: normalizeWhitespace(root && root.textContent) };
    }),
  };
}

function textTableProjection(context, markdown) {
  const root = context.parse(context.renderText({ markdown: true, text: markdown || FIXTURES.gfm_table })).root;
  return tableShape(root ? root.ownerDocument : context.parse('').document);
}

function textTableProjectionFromRoot(context, markdown) {
  const parsed = context.parse(context.renderText({ markdown: true, text: markdown || FIXTURES.gfm_table }));
  return tableShape(parsed.document);
}

function tableFromEmail(context, markdown) {
  const parsed = context.parse(context.renderEmail(markdown || FIXTURES.gfm_table_simple));
  return tableShape(parsed.document);
}

function alignedTableProjection(context, includeAttributes = false) {
  const markdown = FIXTURES.gfm_aligned_table;
  function cells(root) {
    const table = root && root.querySelector('table');
    const project = (selector) => table
      ? Array.from(table.querySelectorAll(selector)).map((cell) => ({
        align: attr(cell, 'align'),
        text: normalizeWhitespace(cell.textContent),
        ...(includeAttributes ? { attributes: attributes(cell) } : {}),
      }))
      : [];
    return {
      width: attr(table, 'width'),
      ...(includeAttributes ? { attributes: attributes(table) } : {}),
      headers: project('thead th'),
      body: project('tbody td'),
    };
  }
  return {
    email: cells(context.parse(context.renderEmail(markdown)).root),
    text: cells(context.parse(context.renderText({ markdown: true, text: markdown })).root),
  };
}

function headerOnlyTableProjection(context) {
  const markdown = FIXTURES.gfm_header_only_table;
  function structure(root) {
    const table = root && root.querySelector('table');
    const tbody = table && table.querySelector('tbody');
    return {
      width: attr(table, 'width'),
      thead: table ? table.querySelectorAll('thead').length : 0,
      tbody: table ? table.querySelectorAll('tbody').length : 0,
      headers: table ? table.querySelectorAll('th').length : 0,
      bodyCells: table ? table.querySelectorAll('tbody td').length : 0,
      bodyElements: tbody ? tbody.children.length : null,
      bodyText: tbody ? normalizeWhitespace(tbody.textContent) : null,
    };
  }
  return {
    email: structure(context.parse(context.renderEmail(markdown)).root),
    text: structure(context.parse(context.renderText({ markdown: true, text: markdown })).root),
  };
}

function lineBreakProjection(context) {
  const single = context.parse(context.renderText({ markdown: true, text: FIXTURES.single_newline })).document;
  const double = context.parse(context.renderText({ markdown: true, text: FIXTURES.double_newline })).document;
  return {
    single: {
      br: single.querySelectorAll('br').length,
      paragraphs: single.querySelectorAll('p').length,
      text: normalizeText(single.querySelector('p')),
    },
    double: {
      br: double.querySelectorAll('br').length,
      paragraphs: double.querySelectorAll('p').length,
    },
  };
}

function markdownBranchProjection(context) {
  const explicitFalse = context.parse(context.renderText({ markdown: false, text: FIXTURES.headings_branch })).root;
  const absent = context.parse(context.renderText({ text: FIXTURES.headings_branch })).root;
  return {
    absentH2: absent ? absent.querySelectorAll('h2').length : 0,
    absentText: normalizeWhitespace(absent && absent.textContent),
    explicitFalseH2: explicitFalse ? explicitFalse.querySelectorAll('h2').length : 0,
    explicitFalseText: normalizeWhitespace(explicitFalse && explicitFalse.textContent),
  };
}

function nonMarkdownLiteralProjection(context) {
  const root = context.parse(context.renderText({ markdown: false, text: FIXTURES.non_markdown_literal })).root;
  return {
    text: normalizeWhitespace(root && root.textContent),
    h2: root ? root.querySelectorAll('h2').length : 0,
    strong: root ? root.querySelectorAll('strong').length : 0,
  };
}

function safeMarkdownProjection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.safe_markdown })).root;
  const anchor = root && root.querySelector('a');
  const span = root && root.querySelector('span');
  return {
    span: root ? root.querySelectorAll('span').length : 0,
    strong: root && root.querySelector('strong') ? normalizeWhitespace(root.querySelector('strong').textContent) : null,
    em: root && root.querySelector('em') ? normalizeWhitespace(root.querySelector('em').textContent) : null,
    li: root ? root.querySelectorAll('ul > li').length : 0,
    href: attr(anchor, 'href'),
    target: attr(anchor, 'target'),
    onclick: attr(span, 'onclick'),
  };
}

function markdownBlocksProjection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.markdown_blocks })).root;
  const link = root && root.querySelector('a');
  return {
    h1: root ? root.querySelectorAll('h1').length : 0,
    h2: root ? root.querySelectorAll('h2').length : 0,
    ul: root ? root.querySelectorAll('ul').length : 0,
    ulLi: root ? root.querySelectorAll('ul > li').length : 0,
    ol: root ? root.querySelectorAll('ol').length : 0,
    olLi: root ? root.querySelectorAll('ol > li').length : 0,
    strong: root ? root.querySelectorAll('strong').length : 0,
    em: root ? root.querySelectorAll('em').length : 0,
    link: { href: attr(link, 'href'), target: attr(link, 'target') },
  };
}

function markdownBranchH2Projection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.headings_branch })).root;
  return {
    h2: root ? root.querySelectorAll('h2').length : 0,
    headingText: normalizeWhitespace(root && root.querySelector('h2') && root.querySelector('h2').textContent),
    tag: root ? root.tagName.toLowerCase() : null,
  };
}

function safeMarkdownSurfaceProjection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.safe_markdown })).root;
  const nonMarkdown = context.parse(context.renderText({ text: '## This is not <span>markdown</span>' })).root;
  const link = root && root.querySelector('a');
  const span = root && root.querySelector('span');
  return {
    defaultsText: context.indexModule.TextPropsDefaults ? context.indexModule.TextPropsDefaults.text : null,
    emailMarkdownType: typeof context.EmailMarkdown,
    link: { href: attr(link, 'href'), target: attr(link, 'target') },
    nonMarkdownSpan: nonMarkdown ? nonMarkdown.querySelectorAll('span').length : 0,
    nonMarkdownText: normalizeWhitespace(nonMarkdown && nonMarkdown.textContent),
    safeEm: root ? root.querySelectorAll('em').length : 0,
    safeListItems: root ? root.querySelectorAll('ul > li').length : 0,
    safeSpanOnClick: attr(span, 'onclick'),
    safeStrong: root ? root.querySelectorAll('strong').length : 0,
    schemaSuccess: context.indexModule.TextPropsSchema.safeParse({ style: {}, props: { markdown: true, text: 'x' } }).success,
    textType: typeof context.Text,
  };
}

function linkProjection(context, markdown) {
  const root = context.parse(context.renderEmail(markdown)).root;
  const anchor = root && root.querySelector('a');
  return { href: attr(anchor, 'href'), target: attr(anchor, 'target'), title: attr(anchor, 'title') };
}

function linksProjection(context) {
  const titled = linkProjection(context, FIXTURES.link_title);
  const plain = linkProjection(context, FIXTURES.link_plain);
  return { links: [titled, plain] };
}

function gfmAutolinkProjection(context) {
  const markdown = FIXTURES.gfm_autolink;
  const email = context.parse(context.renderEmail(markdown)).root;
  const text = context.parse(context.renderText({ markdown: true, text: markdown })).root;
  const literal = context.parse(context.renderText({ markdown: false, text: markdown })).root;
  function link(root) {
    const anchor = root && root.querySelector('a');
    return {
      href: attr(anchor, 'href'),
      target: attr(anchor, 'target'),
      text: normalizeWhitespace(anchor && anchor.textContent),
    };
  }
  return {
    email: link(email),
    text: link(text),
    literal: {
      anchors: literal ? literal.querySelectorAll('a').length : 0,
      text: normalizeWhitespace(literal && literal.textContent),
    },
  };
}

function bareUrlHrefProjection(context) {
  return [FIXTURES.gfm_autolink_unicode, FIXTURES.gfm_autolink_escaped].map((url) => {
    function link(root) {
      const anchor = root && root.querySelector('a');
      return { href: attr(anchor, 'href'), target: attr(anchor, 'target') };
    }
    return {
      url,
      email: link(context.parse(context.renderEmail(url)).root),
      text: link(context.parse(context.renderText({ markdown: true, text: url })).root),
    };
  });
}

function bareDomainLiteralProjection(context) {
  const markdown = FIXTURES.bare_domain_literal;
  function shape(root) {
    return {
      anchors: root ? root.querySelectorAll('a').length : 0,
      text: normalizeWhitespace(root && root.textContent),
    };
  }
  return {
    expected: markdown,
    email: shape(context.parse(context.renderEmail(markdown)).root),
    text: shape(context.parse(context.renderText({ markdown: true, text: markdown })).root),
  };
}

function markdownUrlHrefProjection(context) {
  return [FIXTURES.gfm_autolink_unicode, FIXTURES.gfm_autolink_escaped].map((url) => {
    const markdown = '[site](' + url + ' "Title")';
    function link(root) {
      const anchor = root && root.querySelector('a');
      return { href: attr(anchor, 'href'), target: attr(anchor, 'target'), title: attr(anchor, 'title') };
    }
    return {
      url,
      email: link(context.parse(context.renderEmail(markdown)).root),
      text: link(context.parse(context.renderText({ markdown: true, text: markdown })).root),
    };
  });
}

function gfmStrikethroughProjection(context) {
  const markdown = FIXTURES.gfm_strikethrough;
  const email = context.parse(context.renderEmail(markdown)).root;
  const text = context.parse(context.renderText({ markdown: true, text: markdown })).root;
  const literal = context.parse(context.renderText({ markdown: false, text: markdown })).root;
  function strike(root) {
    const deleted = root && root.querySelector('del');
    return {
      del: root ? root.querySelectorAll('del').length : 0,
      text: normalizeWhitespace(deleted && deleted.textContent),
    };
  }
  return {
    email: strike(email),
    text: strike(text),
    literal: {
      del: literal ? literal.querySelectorAll('del').length : 0,
      text: normalizeWhitespace(literal && literal.textContent),
    },
  };
}

function imageRawProjection(context, markdown) {
  const root = context.parse(context.renderEmail(markdown || FIXTURES.images_inline)).root;
  const image = root && root.querySelector('img');
  const span = root && root.querySelector('span');
  const anchors = root ? Array.from(root.querySelectorAll('a')) : [];
  const rawAnchor = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'raw') || null;
  return {
    image: { alt: attr(image, 'alt'), src: attr(image, 'src') },
    rawAnchor: { href: attr(rawAnchor, 'href'), target: attr(rawAnchor, 'target') },
    spanCount: span ? 1 : 0,
    spanText: normalizeWhitespace(span && span.textContent),
  };
}

function rawHtmlTextProjection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.raw_html_text })).root;
  const span = root && root.querySelector('span');
  const anchors = root ? Array.from(root.querySelectorAll('a')) : [];
  const rawAnchor = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'raw') || null;
  return {
    divCount: root ? root.querySelectorAll('div').length : 0,
    spanCount: root ? root.querySelectorAll('span').length : 0,
    spanText: normalizeWhitespace(span && span.textContent),
    scripts: root ? root.querySelectorAll('script').length : 0,
    rawAnchor: { href: attr(rawAnchor, 'href'), target: attr(rawAnchor, 'target') },
  };
}

function sanitizerEdgeProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.security_links)).root;
  const anchors = root ? Array.from(root.querySelectorAll('a')) : [];
  const images = root ? Array.from(root.querySelectorAll('img')) : [];
  const missingHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'no href') || null;
  const emptyHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'h') || null;
  const ftpHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'f') || null;
  const okHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'ok') || null;
  return {
    anchorCount: anchors.length,
    imageCount: images.length,
    missingHrefOk: missingHref ? [null, ''].includes(attr(missingHref, 'href')) : false,
    emptyHref: attr(emptyHref, 'href'),
    ftpHref: attr(ftpHref, 'href'),
    okHref: attr(okHref, 'href'),
    imageSrcOk: images.every((image) => [null, ''].includes(attr(image, 'src'))),
  };
}

function schemeProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.security_schemes)).root;
  return {
    http: attr(root && root.querySelector('a[href="http://e.com"]'), 'href'),
    https: attr(root && root.querySelector('a[href="https://e.com"]'), 'href'),
    mailto: attr(root && root.querySelector('a[href="mailto:a@b.c"]'), 'href'),
  };
}

function xssProjection(context, markdown) {
  const root = context.parse(context.renderText({ markdown: true, text: markdown || FIXTURES.xss_inline })).root;
  return {
    blockedSchemes: blockedSchemeValues(root || context.parse('').document),
    eventAttributes: eventAttributeNames(root || context.parse('').document),
    scripts: root ? root.querySelectorAll('script').length : 0,
  };
}

function emailStyleProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.strong, { color: '#ff0000' })).root;
  const strong = root && root.querySelector('strong');
  const paragraph = root && root.querySelector('p');
  return {
    tag: root ? root.tagName.toLowerCase() : null,
    childCount: root ? root.children.length : null,
    firstChildTag: root && root.firstElementChild ? root.firstElementChild.tagName.toLowerCase() : null,
    color: root ? root.style.color : null,
    strongCount: root ? root.querySelectorAll('strong').length : 0,
    paragraphCount: root ? root.querySelectorAll('p').length : 0,
    text: normalizeWhitespace(strong && strong.textContent),
    paragraphFirstChild: paragraph && paragraph.firstElementChild ? paragraph.firstElementChild.tagName.toLowerCase() : null,
  };
}

function fullDocumentProjection(context) {
  const root = context.parse(context.renderText({ markdown: true, text: FIXTURES.full_document })).root;
  const link = root && root.querySelector('a');
  const image = root && root.querySelector('img');
  const shape = root ? tableShape(root.ownerDocument) : { table: false };
  return {
    h1: root ? root.querySelectorAll('h1').length : 0,
    ulLi: root ? root.querySelectorAll('ul > li').length : 0,
    strong: root ? root.querySelectorAll('strong').length : 0,
    em: root ? root.querySelectorAll('em').length : 0,
    link: { href: attr(link, 'href'), target: attr(link, 'target') },
    image: { src: attr(image, 'src'), alt: attr(image, 'alt') },
    br: root ? root.querySelectorAll('br').length : 0,
    tableWidth: shape.width,
    thead: shape.thead || 0,
    tbody: shape.tbody || 0,
    th: shape.th || 0,
    td: shape.td || 0,
    rawBold: root ? root.querySelectorAll('b').length : 0,
    scripts: root ? root.querySelectorAll('script').length : 0,
  };
}

function headerRowProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.gfm_table)).root;
  const shape = root ? tableShape(root.ownerDocument) : { table: false };
  return {
    width: shape.width,
    thead: shape.thead || 0,
    tbody: shape.tbody || 0,
    th: root && root.querySelector('th') ? normalizeWhitespace(root.querySelector('th').textContent) : null,
    td: root && root.querySelector('td') ? normalizeWhitespace(root.querySelector('td').textContent) : null,
    tr: shape.tr || 0,
  };
}

function breakProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.single_newline)).root;
  const paragraph = root && root.querySelector('p');
  return {
    br: root ? root.querySelectorAll('br').length : 0,
    paragraphText: normalizeText(paragraph),
  };
}

function javascriptLinkProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.blocked_scheme)).root;
  const anchor = root && root.querySelector('a');
  return { anchor: Boolean(anchor), target: attr(anchor, 'target'), href: attr(anchor, 'href'), blocked: blockedSchemeValues(root || context.parse('').document).length > 0 };
}

function spanProjection(context) {
  const root = context.parse(context.renderEmail('<span style="color:red">x</span>')).root;
  const span = root && root.querySelector('span');
  return {
    span: Boolean(span),
    style: span ? (span.getAttribute('style') || '').replace(/\s+/g, '').toLowerCase() : null,
    text: normalizeWhitespace(span && span.textContent),
  };
}

function renderToStaticMarkupProjection(context) {
  const output = context.server.renderToStaticMarkup(
    context.React.createElement(context.Text, { style: { fontSize: 12 }, props: { markdown: true, text: FIXTURES.strong } })
  );
  return {
    output,
    hasBlockShell: output.includes('<div style="font-size:12px">'),
    hasStrong: output.includes('<p><strong>x</strong></p>'),
  };
}

function withWorkspacePackages(repoRoot, fn) {
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function patchedResolve(request, parent, isMain, options) {
    const match = /^@usewaypoint\/([^/]+)$/.exec(request);
    if (match) {
      const candidates = [
        path.join(repoRoot, 'packages', match[1], 'src/index.tsx'),
        path.join(repoRoot, 'packages', match[1], 'src/index.ts'),
        path.join(repoRoot, 'packages', match[1], 'src/index.js'),
      ];
      for (const candidate of candidates) {
        if (fs.existsSync(candidate)) {
          return candidate;
        }
      }
    }
    return originalResolve.call(this, request, parent, isMain, options);
  };
  try {
    return fn();
  } finally {
    Module._resolveFilename = originalResolve;
  }
}

function renderToStaticMarkupShellProjection(context) {
  const renderToStaticMarkup = withWorkspacePackages(context.repoRoot, () =>
    loadFile(context.repoRoot, 'packages/email-builder/src/renderers/renderToStaticMarkup').default
  );
  return {
    output: renderToStaticMarkup(
      { root: { data: { props: { childrenIds: [] } }, type: 'Container' } },
      { rootBlockId: 'root' }
    ),
  };
}

function schemaProjection(context) {
  const z = context.requirement('zod');
  const buildBlockConfigurationSchema = loadFile(context.repoRoot, 'packages/document-core/src/builders/buildBlockConfigurationSchema.ts').default;
  const schema = buildBlockConfigurationSchema({
    SampleBlock: {
      schema: z.object({ text: z.string() }),
      Component: ({ text }) => context.React.createElement('div', null, text.toUpperCase()),
    },
  });
  return { parsed: schema.safeParse({ type: 'SampleBlock', data: { text: 'Test text!' } }) };
}

function textContractProjection(context) {
  const styled = context.parse(context.renderText({ text: 'x' }, { fontSize: 12 })).root;
  const defaults = context.parse(context.server.renderToStaticMarkup(context.React.createElement(context.Text))).root;
  return {
    defaultTag: defaults ? defaults.tagName.toLowerCase() : null,
    fontSize: styled ? styled.style.fontSize : null,
    schemaSuccess: context.indexModule.TextPropsSchema.safeParse({ style: {}, props: { markdown: true, text: 'x' } }).success,
    text: normalizeWhitespace(styled && styled.textContent),
  };
}

function specInfo(context) {
  const spec = readText(path.join(context.repoRoot, 'packages/block-text/src/index.spec.tsx'));
  const snapshot = readText(path.join(context.repoRoot, 'packages/block-text/src/__snapshots__/index.spec.tsx.snap'));
  const names = ['renders with default values', 'sanitizes HTML', 'renders with safe markdown', 'renders without markdown'];
  return { spec, snapshot, names };
}

function existingTestsProjection(context) {
  const { spec, snapshot, names } = specInfo(context);
  return {
    testNames: names.filter((name) => spec.includes(name)),
    snapshotNames: names.filter((name) => snapshot.includes('block-text ' + name + ' 1')),
    hasSnapshotMatcher: /toMatchSnapshot\(\)/.test(spec),
    hasSkippedOrOnly: /\b(it|test|describe)\.(skip|only)\b/.test(spec),
  };
}

function assertionStrictnessProjection(context) {
  const { spec } = specInfo(context);
  const tokens = [
    'script>alert(1)</script>',
    'onerror=alert(1)',
    'onload="alert',
    'javascript:',
    'data:text/html',
    'This <span onClick="alert',
    '## This is not <span>markdown</span>',
  ];
  return {
    tokenMatches: tokens.filter((token) => spec.includes(token)).length,
    itCount: (spec.match(/it\(/g) || []).length,
    hasSnapshotMatcher: spec.includes('toMatchSnapshot()'),
    hasSkippedOrOnly: /\.skip\(|\.only\(/.test(spec),
  };
}

function snapshotSurfaceProjection(context) {
  const manifestText = readText(path.join(context.repoRoot, 'packages/block-text/package.json'));
  const snapshot = readText(path.join(context.repoRoot, 'packages/block-text/src/__snapshots__/index.spec.tsx.snap'));
  return {
    manifestMarked: /"marked"/.test(manifestText),
    snapshotMarked: /marked/i.test(snapshot),
    snapshotPresent: snapshot.length > 0,
  };
}

function parserSpyProjection(context) {
  let spy = null;
  let restore = null;
  try {
    if (canResolve(context.repoRoot, 'marked')) {
      const marked = context.requirement('marked');
      if (marked && typeof marked.parse === 'function') {
        let calls = 0;
        const original = marked.parse;
        marked.parse = function patchedParse() {
          calls += 1;
          return original.apply(this, arguments);
        };
        restore = () => { marked.parse = original; };
        spy = () => calls;
      }
    }
    if (!spy && canResolve(context.repoRoot, 'markdown-it')) {
      const MarkdownIt = context.requirement('markdown-it');
      const prototype = MarkdownIt && MarkdownIt.prototype;
      if (prototype && typeof prototype.render === 'function') {
        let calls = 0;
        const original = prototype.render;
        prototype.render = function patchedRender() {
          calls += 1;
          return original.apply(this, arguments);
        };
        restore = () => { prototype.render = original; };
        spy = () => calls;
      }
    }
    const root = context.parse(context.renderText({ markdown: false, text: FIXTURES.non_markdown_literal })).root;
    const calls = spy ? spy() : 0;
    return {
      text: normalizeWhitespace(root && root.textContent),
      h2: root ? root.querySelectorAll('h2').length > 0 : false,
      strong: root ? root.querySelectorAll('strong').length > 0 : false,
      parserCalls: calls,
    };
  } finally {
    if (restore) {
      restore();
    }
  }
}

function moduleGraphProjection(context) {
  const loaded = loadedModuleNames();
  return {
    markdownIt: loaded.has('markdown-it'),
    insane: loaded.has('insane'),
    marked: loaded.has('marked'),
  };
}

function markdownItRenderUsageProjection(context) {
  const probe = context.rendererProbe;
  if (!canResolve(context.repoRoot, 'markdown-it')) return { calls: 0, consumed: false };
  const crypto = require('crypto');
  const checks = [];
  try {
    for (const entry of ['EmailMarkdown', 'Text']) {
      const text = crypto.randomBytes(16).toString('hex');
      probe.marker = crypto.randomBytes(24).toString('hex');
      probe.calls = 0;
      const markdown = '# ' + text + '\n\n**' + text + '**';
      const html = entry === 'Text'
        ? context.renderText({ markdown: true, text: markdown })
        : context.renderEmail(markdown);
      const root = context.parse(html).root;
      const spans = root ? Array.from(root.querySelectorAll('span')) : [];
      checks.push({
        entry,
        calls: probe.calls,
        consumed: probe.calls > 0 && spans.some((span) => span.textContent === probe.marker),
        variedInput: Boolean(root && root.querySelector('h1') && root.querySelector('strong')
          && root.querySelector('h1').textContent === text && root.querySelector('strong').textContent === text),
      });
    }
  } finally {
    probe.marker = null;
  }
  return {
    calls: checks.reduce((count, check) => count + check.calls, 0),
    consumed: checks.every((check) => check.consumed),
    variedInput: checks.every((check) => check.variedInput),
    checks,
  };
}

function productionSourceInfo(context) {
  const email = readText(path.join(context.repoRoot, 'packages/block-text/src/EmailMarkdown.tsx'));
  const index = readText(path.join(context.repoRoot, 'packages/block-text/src/index.tsx'));
  return email + '\n' + index;
}

function importsInstalledMarkdownIt(context) {
  const packageRequire = Module.createRequire(path.join(context.repoRoot, 'packages/block-text/package.json'));
  let packageRoot;
  try {
    packageRoot = path.dirname(packageRequire.resolve('markdown-it/package.json'));
  } catch (error) {
    return false;
  }
  for (const relative of ['packages/block-text/src/EmailMarkdown.tsx', 'packages/block-text/src/index.tsx']) {
    const filename = path.join(context.repoRoot, relative);
    const source = readText(filename);
    const fileRequire = Module.createRequire(filename);
    const imports = /\b(?:from\s*|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]/g;
    for (const match of source.matchAll(imports)) {
      try {
        const resolved = fileRequire.resolve(match[1]);
        if (resolved.startsWith(packageRoot + path.sep)) return true;
      } catch (error) {
        // Unresolved candidate imports cannot establish package use.
      }
    }
  }
  return false;
}

function manifestProjection(context) {
  const manifest = readJson(path.join(context.repoRoot, 'packages/block-text/package.json'));
  const dependencies = manifest.dependencies || {};
  const devDependencies = manifest.devDependencies || {};
  const peerDependencies = manifest.peerDependencies || {};
  const runtimeDependency = Object.prototype.hasOwnProperty.call(dependencies, 'markdown-it');
  return {
    markdownIt: runtimeDependency
      ? dependencies['markdown-it']
      : devDependencies['markdown-it'] || null,
    markdownItPlacement: runtimeDependency
      ? 'dependencies'
      : devDependencies['markdown-it'] ? 'devDependencies' : null,
    marked: ('marked' in dependencies) || ('marked' in devDependencies) || ('marked' in peerDependencies),
    insane: dependencies.insane || null,
    react: peerDependencies.react || null,
    zod: peerDependencies.zod || null,
    buildScript: manifest.scripts && manifest.scripts.build || null,
    hasTsupConfig: Object.prototype.hasOwnProperty.call(manifest, 'tsup'),
    installedMarkdownIt: packageVersion(context.repoRoot, 'markdown-it'),
    markedResolvable: canResolve(context.repoRoot, 'marked'),
  };
}

function declaresMarkdownIt14(spec) {
  return spec === '14.1.0' || spec === '^14.1.0' || spec === '~14.1.0';
}

function entryProjection(context) {
  const source = productionSourceInfo(context);
  return {
    importsMarkdownIt: importsInstalledMarkdownIt(context),
    breaks: /breaks\s*:\s*true/.test(source),
    html: /html\s*:\s*true/.test(source),
    linkify: /linkify\s*:\s*true/.test(source),
    linkOpen: /link_open/.test(source),
    tableOpen: /table_open/.test(source),
    insane: /insane/.test(source),
    markedReference: /(?:from\s+['"]marked['"]|require\(\s*['"]marked['"]\s*\)|import\(\s*['"]marked['"]\s*\)|\bmarked\.parse\b|\bmarked\s*\()/.test(source),
    subprocess: /\bchild_process\b|\bworker_threads\b|\bexecSync\b|\bspawn\s*\(|\bfork\s*\(/.test(source),
  };
}

function repoRequirement(repoRoot) {
  return Module.createRequire(path.join(repoRoot, 'package.json'));
}

function nodeModulesRootFor(repoRoot, name) {
  try {
    const resolved = repoRequirement(repoRoot).resolve(name + '/package.json');
    const parts = resolved.split(path.sep);
    const index = parts.lastIndexOf('node_modules');
    if (index >= 0) {
      return parts.slice(0, index + 1).join(path.sep) || path.sep;
    }
  } catch (error) {
    return null;
  }
  return null;
}

function resolvePackageBin(repoRoot, name, binName) {
  try {
    const pkgPath = repoRequirement(repoRoot).resolve(name + '/package.json');
    const pkg = readJson(pkgPath);
    const bin = typeof pkg.bin === 'string'
      ? pkg.bin
      : pkg.bin && (pkg.bin[binName] || pkg.bin[name]);
    if (bin) {
      const candidate = path.resolve(path.dirname(pkgPath), bin);
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  } catch (error) {
    return null;
  }
  return null;
}

function inspectBuildDirectory(context, directory) {
  const cjsPath = path.join(directory, 'index.js');
  const esmPath = path.join(directory, 'index.mjs');
  const dtsPath = path.join(directory, 'index.d.ts');
  const cjsExists = fs.existsSync(cjsPath);
  const esmExists = fs.existsSync(esmPath);
  const dtsExists = fs.existsSync(dtsPath);
  let cjsTable = false;
  if (cjsExists) {
    try {
      const cjs = require(cjsPath);
      const TextCjs = cjs.Text || (cjs.default && cjs.default.Text);
      if (TextCjs) {
        const html = context.server.renderToStaticMarkup(
          context.React.createElement(TextCjs, { props: { markdown: true, text: FIXTURES.gfm_table_simple } })
        );
        cjsTable = /<table\b[^>]*width="100%"/.test(html);
      }
    } catch (error) {
      cjsTable = false;
    }
  }
  let esmTable = false;
  if (esmExists) {
    const script = [
      "import React from 'react';",
      "import { renderToStaticMarkup } from 'react-dom/server';",
      'import { Text } from ' + JSON.stringify(pathToFileURL(esmPath).href) + ';',
      'const text = ' + JSON.stringify(FIXTURES.gfm_table_simple) + ';',
      'const html = renderToStaticMarkup(React.createElement(Text, { props: { markdown: true, text } }));',
      'process.stdout.write(html);',
    ].join('\n');
    try {
      const output = childProcess.execFileSync(process.execPath, ['--input-type=module', '--eval', script], {
        cwd: path.dirname(directory),
        encoding: 'utf8',
        timeout: 120000,
        env: Object.assign({}, process.env),
      });
      esmTable = /<table\b[^>]*width="100%"/.test(output);
    } catch (error) {
      esmTable = false;
    }
  }
  return { cjs: cjsExists, esm: esmExists, dts: dtsExists, cjsTable, esmTable };
}

function inspectStandaloneBuild(context, directory) {
  // Install the built package with its runtime dependencies next to the package,
  // as an isolated package manager would. The consumer has only peer dependencies.
  const consumer = fs.mkdtempSync(path.join(os.tmpdir(), 'migration-block-text-consumer-'));
  const result = { cjs: false, esm: false, errors: {} };
  try {
    const packageRoot = path.join(consumer, 'node_modules', '@usewaypoint', 'block-text');
    const runtimeRoot = path.join(packageRoot, 'node_modules');
    fs.mkdirSync(runtimeRoot, { recursive: true });
    fs.cpSync(directory, path.join(packageRoot, 'dist'), { recursive: true });
    fs.copyFileSync(
      path.join(context.repoRoot, 'packages', 'block-text', 'package.json'),
      path.join(packageRoot, 'package.json')
    );
    const requirement = Module.createRequire(path.join(context.repoRoot, 'packages/block-text/package.json'));
    const manifest = readJson(path.join(packageRoot, 'package.json'));
    for (const name of ['react', 'react-dom', 'zod']) {
      const packageDir = path.dirname(requirement.resolve(name + '/package.json'));
      fs.symlinkSync(packageDir, path.join(consumer, 'node_modules', name), 'dir');
    }
    for (const name of Object.keys(manifest.dependencies || {})) {
      const packageDir = path.dirname(requirement.resolve(name + '/package.json'));
      fs.symlinkSync(packageDir, path.join(runtimeRoot, name), 'dir');
    }
    const render = [
      "const html = renderToStaticMarkup(React.createElement(Text, { props: { markdown: true, text: '| A |\\n| --- |\\n| B |' } }));",
      "if (!/<table\\b[^>]*width=\"100%\"/.test(html)) process.exit(1);",
    ].join('\n');
    const cjs = [
      "const React = require('react');",
      "const { renderToStaticMarkup } = require('react-dom/server');",
      "const { Text } = require('@usewaypoint/block-text');",
      render,
    ].join('\n');
    const esm = [
      "import React from 'react';",
      "import { renderToStaticMarkup } from 'react-dom/server';",
      "import { Text } from '@usewaypoint/block-text';",
      render,
    ].join('\n');
    const environment = Object.assign({}, process.env);
    delete environment.NODE_PATH;
    for (const [format, args] of [
      ['cjs', ['--eval', cjs]],
      ['esm', ['--input-type=module', '--eval', esm]],
    ]) {
      try {
        childProcess.execFileSync(process.execPath, args, {
          cwd: consumer,
          stdio: 'pipe',
          timeout: 120000,
          env: environment,
        });
        result[format] = true;
      } catch (error) {
        result[format] = false;
        result.errors[format] = String(error.message || error) + '\n' + String(error.stderr || '');
      }
    }
  } catch (error) {
    result.errors.setup = String(error.message || error);
    return result;
  } finally {
    fs.rmSync(consumer, { recursive: true, force: true });
  }
  return result;
}

function prepareBuildWorkspace(context) {
  const buildRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'migration-block-text-build-'));
  const packageSource = path.join(context.repoRoot, 'packages', 'block-text');
  const packageTarget = path.join(buildRoot, 'packages', 'block-text');
  fs.mkdirSync(packageTarget, { recursive: true });
  const nodeModules = nodeModulesRootFor(context.repoRoot, 'react') || nodeModulesRootFor(context.repoRoot, 'insane');
  if (nodeModules) {
    try {
      fs.symlinkSync(nodeModules, path.join(buildRoot, 'node_modules'), 'dir');
    } catch (error) {
      // Resolution may still succeed through the environment.
    }
  }
  const rootTsconfig = path.join(context.repoRoot, 'tsconfig.json');
  if (fs.existsSync(rootTsconfig)) {
    fs.copyFileSync(rootTsconfig, path.join(buildRoot, 'tsconfig.json'));
  }
  for (const name of ['package.json', 'tsconfig.json']) {
    const file = path.join(packageSource, name);
    if (fs.existsSync(file)) {
      fs.copyFileSync(file, path.join(packageTarget, name));
    }
  }
  const sourceDir = path.join(packageSource, 'src');
  if (fs.existsSync(sourceDir)) {
    fs.cpSync(sourceDir, path.join(packageTarget, 'src'), {
      recursive: true,
      filter: (file) => !file.split(path.sep).includes('dist'),
    });
  }
  return { buildRoot, packageDir: packageTarget, outDir: path.join(buildRoot, 'dist') };
}

function buildProjection(context) {
  const workspace = prepareBuildWorkspace(context);
  const errors = [];
  const tsupCli = resolvePackageBin(context.repoRoot, 'tsup', 'tsup');
  if (tsupCli) {
    try {
      childProcess.execFileSync(
        process.execPath,
        [tsupCli, './src/index.tsx', '--outDir', workspace.outDir, '--format', 'cjs,esm', '--dts'],
        {
          cwd: workspace.packageDir,
          stdio: 'pipe',
          timeout: 180000,
          env: Object.assign({}, process.env),
        }
      );
    } catch (error) {
      errors.push(String(error.message || error));
    }
  } else {
    errors.push('tsup executable could not be resolved');
  }
  let buildDirectory = workspace.outDir;
  let result = inspectBuildDirectory(context, buildDirectory);
  if (!(result.cjs && result.esm && result.dts && result.cjsTable && result.esmTable)) {
    try {
      childProcess.execFileSync('npm', ['run', 'build', '-w', '@usewaypoint/block-text'], {
        cwd: context.repoRoot,
        stdio: 'pipe',
        timeout: 180000,
        env: Object.assign({}, process.env),
      });
    } catch (error) {
      errors.push(String(error.message || error));
    }
    const distDirectory = path.join(context.repoRoot, 'packages', 'block-text', 'dist');
    const distResult = inspectBuildDirectory(context, distDirectory);
    if (distResult.cjs || distResult.esm || distResult.dts) {
      result = distResult;
      buildDirectory = distDirectory;
    }
  }
  const standalone = result.cjs && result.esm
    ? inspectStandaloneBuild(context, buildDirectory)
    : { cjs: false, esm: false };
  try {
    fs.rmSync(workspace.buildRoot, { recursive: true, force: true });
  } catch (error) {
    // Best-effort cleanup only.
  }
  return {
    cjs: result.cjs,
    esm: result.esm,
    dts: result.dts,
    cjsTable: result.cjsTable,
    esmTable: result.esmTable,
    standaloneCjs: standalone.cjs,
    standaloneEsm: standalone.esm,
    standaloneErrors: standalone.errors,
    buildError: errors.length > 0 ? errors.join('; ') : undefined,
  };
}

function targetTechnology(context) {
  const manifest = manifestProjection(context);
  const entry = entryProjection(context);
  const loaded = moduleGraphProjection(context);
  const rendererUsage = markdownItRenderUsageProjection(context);
  const failures = [];
  if (!declaresMarkdownIt14(manifest.markdownIt)) failures.push('manifest markdown-it does not select 14.1.0');
  if (manifest.marked) failures.push('manifest still declares marked');
  if (manifest.insane !== '^2.6.2') failures.push('insane dependency changed');
  if (manifest.react !== '^16 || ^17 || ^18') failures.push('react peer range changed');
  if (manifest.zod !== '^1 || ^2 || ^3') failures.push('zod peer range changed');
  if (manifest.buildScript !== 'tsup ./src/index.tsx --outDir dist --format cjs,esm --dts') failures.push('block-text build script changed');
  if (manifest.hasTsupConfig) failures.push('block-text tsup configuration changed');
  if (manifest.installedMarkdownIt !== '14.1.0') failures.push('installed markdown-it is not 14.1.0');
  if (manifest.markedResolvable) failures.push('marked is still resolvable');
  if (!loaded.markdownIt) failures.push('markdown-it was not loaded on the production path');
  if (!loaded.insane) failures.push('insane was not loaded on the production path');
  if (loaded.marked) failures.push('marked was loaded on the production path');
  if (!rendererUsage.consumed) failures.push('markdown-it renderer output did not reach production HTML');
  if (!rendererUsage.variedInput) failures.push('markdown-it rendering did not preserve fresh Markdown input');
  if (!entry.importsMarkdownIt) failures.push('EmailMarkdown does not import markdown-it');
  if (entry.markedReference) failures.push('production source still references marked');
  if (entry.subprocess) failures.push('production source contains a subprocess or dynamic fallback marker');
  return {
    passed: failures.length === 0,
    failures,
    manifest,
    entry,
    loaded,
    rendererUsage,
  };
}

const P2P_EXPECTED = {
  'P2P-001': { tag: 'div', childElementCount: 0, text: '' },
  'P2P-002': { text: 'TEST TEXT!' },
  'P2P-003': { tag: 'div', anchor: true, target: '_blank' },
  'P2P-004': { td: 2, hasBread: true, hasTomato: true, hasLettuce: false },
  'P2P-005': { text: '## literal **x**', h2: false, strong: false, parserCalls: 0 },
  'P2P-006': { tag: 'div', padding: '1px 4px 3px 2px', borderTopWidth: '10px' },
  'P2P-007': { tag: 'h1', text: 'Hello world!', textAlign: 'center', padding: '15px 8px 10px 24px' },
  'P2P-008': { tag: 'table', width: '100%', thead: 1, tbody: 1, tr: 2, th: 2, td: 2 },
  'P2P-009': [
    { tag: 'div', childElementCount: 0, text: '' },
    { tag: 'div', childElementCount: 0, text: '' },
    { tag: 'div', childElementCount: 0, text: '' },
    { tag: 'div', childElementCount: 0, text: '' },
  ],
  'P2P-010': { td: 3, hasBread: true, hasTomato: true, hasLettuce: true },
  'P2P-011': { tag: 'div', padding: '', borderTopWidth: '1px' },
  'P2P-012': { div: 1, text: 'TEST TEXT!' },
  'P2P-013': {
    testNames: ['renders with default values', 'sanitizes HTML', 'renders with safe markdown', 'renders without markdown'],
    snapshotNames: ['renders with default values', 'sanitizes HTML', 'renders with safe markdown', 'renders without markdown'],
    hasSnapshotMatcher: true,
    hasSkippedOrOnly: false,
  },
  'P2P-014': { tag: 'div', text: '' },
  'P2P-015': { output: '<div style="font-size:12px"><p><strong>x</strong></p>\n</div>', hasBlockShell: true, hasStrong: true },
  'P2P-016': { tag: 'div', height: '16px' },
  'P2P-017': { tag: 'div', height: '10px' },
  'P2P-018': { checked: 30, missing: [] },
  'P2P-019': { tag: 'div', childElementCount: 0 },
  'P2P-020': { span: 1, strong: 'Markdown', em: 'turned on', li: 3, href: 'https://usewaypoint.com', target: '_blank', onclick: null },
  'P2P-021': {
    missingHref: '',
    emptyHref: '',
    ftpHasHref: false,
    okHref: 'https://ok.example',
    imageCount: 2,
    imageSrcs: ['', ''],
    literalText: '**x**',
    literalStrong: false,
  },
  'P2P-022': { tag: 'div' },
  'P2P-023': { tokenMatches: 7, itCount: 4, hasSnapshotMatcher: true, hasSkippedOrOnly: false },
  'P2P-024': { parsed: { success: true, data: { type: 'SampleBlock', data: { text: 'Test text!' } } } },
  'P2P-025': {
    defaults: { tag: 'h2', text: '', textAlign: '', padding: '' },
    styled: { tag: 'h1', text: 'Hello world!', textAlign: 'center', padding: '15px 8px 10px 24px' },
  },
  'P2P-026': {
    links: [
      { href: 'https://example.com', target: '_blank', title: 'Title' },
      { href: 'https://example.com', target: '_blank', title: null },
    ],
  },
  'P2P-027': { output: '<!DOCTYPE html><html><body><div></div></body></html>' },
  'P2P-028': {
    image: { alt: 'alt', src: 'https://e.com/i.png' },
    rawAnchor: { href: 'https://e.com', target: null },
    spanCount: 1,
    spanText: 's',
  },
  'P2P-029': { blockedSchemes: [], eventAttributes: [], scripts: 0 },
  'P2P-030': {
    defaultsText: '',
    emailMarkdownType: 'function',
    link: { href: 'https://usewaypoint.com', target: '_blank' },
    nonMarkdownSpan: 0,
    nonMarkdownText: '## This is not <span>markdown</span>',
    safeEm: 1,
    safeListItems: 3,
    safeSpanOnClick: null,
    safeStrong: 1,
    schemaSuccess: true,
    textType: 'function',
  },
  'P2P-031': {
    double: { br: 0, paragraphs: 2 },
    single: { br: 1, paragraphs: 1, text: 'line one line two' },
  },
  'P2P-032': {
    absentH2: 0,
    absentText: '## H',
    explicitFalseH2: 0,
    explicitFalseText: '## H',
  },
  'P2P-033': {
    anchor: { href: 'https://e.com', target: null },
    divCount: 1,
    scripts: 0,
    spanCount: 1,
    spanText: 'ok',
  },
  'P2P-034': {
    table: { width: '100%' },
    tbody: 1,
    td: 2,
    th: 2,
    thead: 1,
    tr: 2,
  },
  'P2P-035': {
    anchors: [
      { href: null, text: 'no href' },
      { href: '', text: 'h' },
      { href: null, text: 'f' },
      { href: 'https://x.com', text: 'https' },
      { href: 'mailto:a@b.com', text: 'mail' },
    ],
    images: [{ src: null }, { src: '' }],
  },
  'P2P-036': {
    color: 'rgb(255, 0, 0)',
    firstChildTag: 'p',
    paragraphCount: 1,
    strongCount: 1,
    tag: 'div',
    text: 'x',
  },
  'P2P-037': { defaultTag: 'div', fontSize: '12px', schemaSuccess: true, text: 'x' },
  'P2P-038': {
    em: 1,
    h1: 1,
    h2: 1,
    link: { href: 'https://e.com', target: '_blank' },
    ol: 1,
    olLi: 1,
    strong: 1,
    ul: 1,
    ulLi: 2,
  },
  'P2P-039': { h2: 1, headingText: 'H', tag: 'div' },
  'P2P-040': { blockedSchemes: [], eventAttributes: [], scripts: 0 },
  'P2P-041': {
    email: { href: 'https://example.com/docs', target: '_blank', text: 'https://example.com/docs' },
    text: { href: 'https://example.com/docs', target: '_blank', text: 'https://example.com/docs' },
    literal: { anchors: 0, text: 'https://example.com/docs' },
  },
  'P2P-042': {
    email: { del: 1, text: 'obsolete' },
    text: { del: 1, text: 'obsolete' },
    literal: { del: 0, text: '~~obsolete~~' },
  },
  'P2P-043': {
    email: {
      width: '100%',
      headers: [
        { align: 'left', text: 'Left' },
        { align: 'center', text: 'Center' },
        { align: 'right', text: 'Right' },
      ],
      body: [
        { align: 'left', text: 'a' },
        { align: 'center', text: 'b' },
        { align: 'right', text: 'c' },
      ],
    },
    text: {
      width: '100%',
      headers: [
        { align: 'left', text: 'Left' },
        { align: 'center', text: 'Center' },
        { align: 'right', text: 'Right' },
      ],
      body: [
        { align: 'left', text: 'a' },
        { align: 'center', text: 'b' },
        { align: 'right', text: 'c' },
      ],
    },
  },
  'P2P-044': {
    email: { width: '100%', thead: 1, tbody: 1, headers: 1, bodyCells: 0, bodyElements: 0, bodyText: '' },
    text: { width: '100%', thead: 1, tbody: 1, headers: 1, bodyCells: 0, bodyElements: 0, bodyText: '' },
  },
};

function p2p35Projection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.p2p35_links)).root;
  const anchors = root ? Array.from(root.querySelectorAll('a')).map((anchor) => ({ href: attr(anchor, 'href'), text: normalizeWhitespace(anchor.textContent) })) : [];
  const images = root ? Array.from(root.querySelectorAll('img')).map((image) => ({ src: attr(image, 'src') })) : [];
  return { anchors, images };
}

function p2pSanitizerProjection(context) {
  const root = context.parse(context.renderEmail(FIXTURES.security_links)).root;
  const anchors = root ? Array.from(root.querySelectorAll('a')) : [];
  const images = root ? Array.from(root.querySelectorAll('img')) : [];
  const missingHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'no href') || null;
  const emptyHref = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'h') || null;
  const ftpAnchor = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'f') || null;
  const okAnchor = anchors.find((anchor) => normalizeWhitespace(anchor.textContent) === 'ok') || null;
  const literal = context.parse(context.renderText({ markdown: false, text: '**x**' })).root;
  return {
    missingHref: attr(missingHref, 'href') || '',
    emptyHref: attr(emptyHref, 'href') || '',
    ftpHasHref: ftpAnchor ? ftpAnchor.hasAttribute('href') : false,
    okHref: attr(okAnchor, 'href'),
    imageCount: images.length,
    imageSrcs: images.map((image) => attr(image, 'src') || ''),
    literalText: normalizeWhitespace(literal && literal.textContent),
    literalStrong: literal ? literal.querySelectorAll('strong').length > 0 : false,
  };
}

function p2pProjection(context, observationId) {
  switch (observationId) {
    case 'P2P-001':
      return blockHtmlProjection(context);
    case 'P2P-019': {
      const projection = blockHtmlProjection(context);
      return { tag: projection.tag, childElementCount: projection.childElementCount };
    }
    case 'P2P-002':
      return buildBlockComponentProjection(context, 'default');
    case 'P2P-003':
      return buttonProjection(context);
    case 'P2P-004':
      return columnsProjection(context, 2);
    case 'P2P-005':
      return parserSpyProjection(context);
    case 'P2P-006':
      return dividerProjection(context, true);
    case 'P2P-007':
      return headingProjection(context, true);
    case 'P2P-008': {
      const projection = textTableProjectionFromRoot(context, '| a | b |\n| - | - |\n| 1 | 2 |');
      return { tag: projection.table ? 'table' : null, width: projection.width, thead: projection.thead, tbody: projection.tbody, tr: projection.tr, th: projection.th, td: projection.td };
    }
    case 'P2P-009':
      return emptyVariantsProjection(context).variants;
    case 'P2P-010':
      return columnsProjection(context, 3);
    case 'P2P-011':
      return dividerProjection(context, false);
    case 'P2P-012':
      return buildBlockComponentProjection(context, 'surface');
    case 'P2P-013':
      return existingTestsProjection(context);
    case 'P2P-014':
      return textDefaultsProjection(context);
    case 'P2P-015':
      return renderToStaticMarkupProjection(context);
    case 'P2P-016':
      return spacerProjection(context, undefined);
    case 'P2P-017':
      return spacerProjection(context, 10);
    case 'P2P-018': {
      const projection = exportProjection(context);
      return { checked: 30, missing: projection.missing };
    }
    case 'P2P-020':
      return safeMarkdownProjection(context);
    case 'P2P-021':
      return p2pSanitizerProjection(context);
    case 'P2P-022':
      return containerProjection(context);
    case 'P2P-023':
      return assertionStrictnessProjection(context);
    case 'P2P-024':
      return schemaProjection(context);
    case 'P2P-025':
      return { defaults: headingProjection(context, false), styled: headingProjection(context, true) };
    case 'P2P-026':
      return linksProjection(context);
    case 'P2P-027':
      return renderToStaticMarkupShellProjection(context);
    case 'P2P-028':
      return imageRawProjection(context);
    case 'P2P-029':
      return xssProjection(context);
    case 'P2P-030':
      return safeMarkdownSurfaceProjection(context);
    case 'P2P-031':
      return lineBreakProjection(context);
    case 'P2P-032':
      return markdownBranchProjection(context);
    case 'P2P-033': {
      const projection = rawHtmlTextProjection(context);
      return { anchor: projection.rawAnchor, divCount: projection.divCount, scripts: projection.scripts, spanCount: projection.spanCount, spanText: projection.spanText };
    }
    case 'P2P-034': {
      const projection = headerRowProjection(context);
      const root = context.parse(context.renderEmail(FIXTURES.gfm_table)).root;
      return { table: { width: projection.width }, tbody: projection.tbody, td: root ? root.querySelectorAll('td').length : 0, th: root ? root.querySelectorAll('th').length : 0, thead: projection.thead, tr: projection.tr };
    }
    case 'P2P-035':
      return p2p35Projection(context);
    case 'P2P-036': {
      const projection = emailStyleProjection(context);
      return { color: projection.color, firstChildTag: projection.firstChildTag, paragraphCount: projection.paragraphCount, strongCount: projection.strongCount, tag: projection.tag, text: projection.text };
    }
    case 'P2P-037':
      return textContractProjection(context);
    case 'P2P-038':
      return markdownBlocksProjection(context);
    case 'P2P-039':
      return markdownBranchH2Projection(context);
    case 'P2P-040':
      return xssProjection(context, FIXTURES.existing_xss_fixture);
    case 'P2P-041':
      return gfmAutolinkProjection(context);
    case 'P2P-042':
      return gfmStrikethroughProjection(context);
    case 'P2P-043':
      return alignedTableProjection(context);
    case 'P2P-044':
      return headerOnlyTableProjection(context);
    default:
      throw new Error('Unknown P2P observation ' + observationId);
  }
}

function deepEqual(left, right) {
  if (left === right) {
    return true;
  }
  if (typeof left !== typeof right || left === null || right === null) {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  if (typeof left === 'object') {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return deepEqual(leftKeys, rightKeys) && leftKeys.every((key) => deepEqual(left[key], right[key]));
  }
  return false;
}

function runP2P(context, observationId) {
  const projection = p2pProjection(context, observationId);
  const expected = P2P_EXPECTED[observationId];
  const matched = deepEqual(projection, expected);
  return {
    status: matched ? 'PASS' : 'FAIL',
    value: { type: 'json', value: projection },
    detail: matched ? 'projection matched' : 'projection differed from frozen P2P expectation',
  };
}

const F2P_CHECKS = {
  'F2P-001': ['anchors', 'span'],
  'F2P-002': ['links', 'textTable'],
  'F2P-003': ['full'],
  'F2P-004': ['imageRaw', 'breaks'],
  'F2P-005': ['h2', 'nonMarkdown'],
  'F2P-006': ['span'],
  'F2P-007': ['imageRaw', 'span'],
  'F2P-008': ['javascriptLink'],
  'F2P-009': ['entry', 'breaks'],
  'F2P-010': ['anchors', 'javascriptLink'],
  'F2P-011': ['entry', 'textTable'],
  'F2P-012': ['links', 'span'],
  'F2P-013': ['imageRaw', 'full'],
  'F2P-014': ['headerRow', 'full'],
  'F2P-015': ['rawHtml', 'span'],
  'F2P-016': ['rawHtml', 'javascriptLink'],
  'F2P-017': ['emailStyle', 'span'],
  'F2P-018': ['manifest', 'build'],
  'F2P-019': ['manifest', 'textTable'],
  'F2P-020': ['imageRaw', 'javascriptLink'],
  'F2P-021': ['headerRow', 'breaks', 'alignedAttributes'],
  'F2P-022': ['headerRow', 'javascriptLink'],
  'F2P-023': ['anchors', 'textTable'],
  'F2P-024': ['headerRow', 'span'],
  'F2P-025': ['manifest'],
  'F2P-026': ['manifest', 'textTable'],
  'F2P-027': ['xss', 'anchors'],
  'F2P-028': ['entry'],
  'F2P-029': ['xss', 'manifest'],
  'F2P-030': ['rawHtml', 'moduleGraph'],
  'F2P-031': ['emailStyle', 'breaks'],
  'F2P-032': ['links'],
  'F2P-033': ['xss', 'moduleGraph'],
  'F2P-034': ['h2', 'nonMarkdown'],
  'F2P-035': ['safeMarkdown'],
  'F2P-036': ['full'],
  'F2P-037': ['full', 'rawHtml'],
  'F2P-038': ['xss', 'breaks'],
  'F2P-039': ['rawHtml'],
  'F2P-040': ['manifest', 'snapshotSurface'],
  'F2P-041': ['manifest'],
  'F2P-042': ['headerRow', 'breaks'],
  'F2P-043': ['links'],
  'F2P-044': ['links', 'breaks'],
  'F2P-045': ['build'],
  'F2P-046': ['imageRaw', 'breaks'],
  'F2P-047': ['moduleGraph', 'textTable'],
  'F2P-048': ['links', 'javascriptLink'],
  'F2P-049': ['moduleGraph', 'textTable'],
  'F2P-050': ['full', 'nonMarkdown'],
  'F2P-051': ['full'],
  'F2P-052': ['javascriptLink'],
  'F2P-053': ['xss'],
  'F2P-054': ['headerRow', 'moduleGraph'],
  'F2P-055': ['full'],
  'F2P-056': ['emailStyle', 'snapshotSurface'],
  'F2P-057': ['links', 'bareUrlHref', 'markdownUrlHref', 'bareDomainLiteral'],
  'F2P-058': ['nonMarkdown', 'full'],
  'F2P-059': ['rawHtml', 'moduleGraph'],
  'F2P-060': ['imageRaw', 'moduleGraph'],
  'F2P-061': ['headerRow', 'snapshotSurface'],
  'F2P-062': ['links', 'full'],
  'F2P-063': ['rawHtml'],
  'F2P-064': ['nonMarkdown', 'snapshotSurface'],
  'F2P-065': ['breaks'],
  'F2P-066': ['headerRow', 'full'],
  'F2P-067': ['safeMarkdown', 'full'],
  'F2P-068': ['emailStyle'],
  'F2P-069': ['imageRaw', 'snapshotSurface'],
  'F2P-070': ['imageRaw', 'full'],
  'F2P-071': ['emailStyle', 'full'],
  'F2P-072': ['xss', 'full'],
};

function targetBehaviorCheck(context, name) {
  try {
    switch (name) {
      case 'full': {
        const p = fullDocumentProjection(context);
        const ok = p.h1 === 1 && p.ulLi === 1 && p.strong === 1 && p.em === 1 && p.link.href === 'https://example.com' && p.link.target === '_blank' && p.image.src === 'https://example.com/i.png' && p.image.alt === 'img' && p.br >= 1 && p.tableWidth === '100%' && p.thead >= 1 && p.tbody >= 1 && p.th >= 1 && p.td >= 1 && p.rawBold >= 1 && p.scripts === 0;
        return { ok, value: p };
      }
      case 'headerRow': {
        const p = headerRowProjection(context);
        const ok = p.width === '100%' && p.thead === 1 && p.tbody === 1 && p.th === 'h1' && p.td === 'a' && p.tr >= 2;
        return { ok, value: p };
      }
      case 'alignedAttributes': {
        const p = alignedTableProjection(context, true);
        const alignments = ['left', 'center', 'right'];
        const ok = [p.email, p.text].every((table) =>
          deepEqual(table.attributes, { width: '100%' })
          && [table.headers, table.body].every((cells) =>
            cells.length === alignments.length && cells.every((cell, index) =>
              deepEqual(cell.attributes, { align: alignments[index] }))));
        return { ok, value: p };
      }
      case 'textTable': {
        const p = textTableProjectionFromRoot(context);
        const ok = p.table && p.width === '100%' && p.thead === 1 && p.tbody === 1 && p.th >= 1 && p.td >= 1;
        return { ok, value: p };
      }
      case 'links': {
        const p = linksProjection(context);
        const ok = p.links[0].href === 'https://example.com' && p.links[0].target === '_blank' && p.links[0].title === 'Title' && p.links[1].href === 'https://example.com' && p.links[1].target === '_blank' && p.links[1].title === null;
        return { ok, value: p };
      }
      case 'bareUrlHref': {
        const p = bareUrlHrefProjection(context);
        const ok = p.every(({ url, email, text }) =>
          email.href === url && email.target === '_blank'
          && text.href === url && text.target === '_blank');
        return { ok, value: p };
      }
      case 'bareDomainLiteral': {
        const p = bareDomainLiteralProjection(context);
        const ok = [p.email, p.text].every(({ anchors, text }) =>
          anchors === 0 && text === p.expected);
        return { ok, value: p };
      }
      case 'markdownUrlHref': {
        const p = markdownUrlHrefProjection(context);
        const ok = p.every(({ url, email, text }) =>
          email.href && new URL(email.href).href === new URL(url).href && email.target === '_blank' && email.title === 'Title'
          && text.href && new URL(text.href).href === new URL(url).href && text.target === '_blank' && text.title === 'Title');
        return { ok, value: p };
      }
      case 'imageRaw': {
        const p = imageRawProjection(context);
        const ok = p.image.alt === 'alt' && p.image.src === 'https://e.com/i.png' && p.rawAnchor.href === 'https://e.com' && p.rawAnchor.target === null && p.spanCount === 1;
        return { ok, value: p };
      }
      case 'breaks': {
        const p = breakProjection(context);
        const ok = p.br === 1 && p.paragraphText.replace(/\s+/g, '') === 'lineoneline two'.replace(/\s+/g, '');
        return { ok, value: p };
      }
      case 'anchors': {
        const p = { ...sanitizerEdgeProjection(context), schemes: schemeProjection(context) };
        const ok = p.anchorCount === 4 && p.imageCount === 2 && p.missingHrefOk && p.emptyHref === '' && p.ftpHref === null && p.okHref === 'https://ok.example' && p.imageSrcOk && p.schemes.http === 'http://e.com' && p.schemes.https === 'https://e.com' && p.schemes.mailto === 'mailto:a@b.c';
        return { ok, value: p };
      }
      case 'javascriptLink': {
        const p = javascriptLinkProjection(context);
        const ok = p.anchor && p.target === '_blank' && !p.blocked && (p.href === null || p.href === '');
        return { ok, value: p };
      }
      case 'span': {
        const p = spanProjection(context);
        const ok = p.span && p.style && p.style.includes('color:red') && p.text === 'x';
        return { ok, value: p };
      }
      case 'rawHtml': {
        const p = rawHtmlTextProjection(context);
        const ok = p.spanCount === 1 && p.spanText === 'ok' && p.scripts === 0 && p.rawAnchor.href === 'https://e.com' && p.rawAnchor.target === null;
        return { ok, value: p };
      }
      case 'h2': {
        const p = markdownBranchH2Projection(context);
        const ok = p.h2 === 1 && p.headingText === 'H' && p.tag === 'div';
        return { ok, value: p };
      }
      case 'nonMarkdown': {
        const p = nonMarkdownLiteralProjection(context);
        const ok = p.text === '## literal **x**' && p.h2 === 0 && p.strong === 0;
        return { ok, value: p };
      }
      case 'safeMarkdown': {
        const p = safeMarkdownProjection(context);
        const ok = p.span === 1 && p.strong === 'Markdown' && p.em === 'turned on' && p.li === 3 && p.href === 'https://usewaypoint.com' && p.target === '_blank' && p.onclick === null;
        return { ok, value: p };
      }
      case 'emailStyle': {
        const p = emailStyleProjection(context);
        const ok = p.tag === 'div' && p.childCount === 1 && p.firstChildTag === 'p' && p.strongCount === 1 && p.paragraphCount === 1 && p.text === 'x' && (p.color === 'rgb(255, 0, 0)' || p.color === '#ff0000') && p.paragraphFirstChild === 'strong';
        return { ok, value: p };
      }
      case 'xss': {
        const p = xssProjection(context);
        const ok = p.blockedSchemes.length === 0 && p.eventAttributes.length === 0 && p.scripts === 0;
        return { ok, value: p };
      }
      case 'moduleGraph': {
        const p = moduleGraphProjection(context);
        const ok = p.markdownIt && p.insane && !p.marked;
        return { ok, value: p };
      }
      case 'manifest': {
        const p = manifestProjection(context);
        const ok = declaresMarkdownIt14(p.markdownIt) && !p.marked && p.insane === '^2.6.2' && p.react === '^16 || ^17 || ^18' && p.zod === '^1 || ^2 || ^3' && p.buildScript === 'tsup ./src/index.tsx --outDir dist --format cjs,esm --dts' && !p.hasTsupConfig && p.installedMarkdownIt === '14.1.0' && !p.markedResolvable;
        return { ok, value: p };
      }
      case 'entry': {
        const p = entryProjection(context);
        // A softbreak renderer can provide the required br without breaks: true.
        // Keep source flags as diagnostics and score the observable newline behavior.
        const breaks = targetBehaviorCheck(context, 'breaks');
        const ok = p.importsMarkdownIt && breaks.ok && p.html && p.linkOpen && p.tableOpen && p.insane && !p.markedReference && !p.subprocess;
        return { ok, value: { ...p, newlineBehavior: breaks } };
      }
      case 'snapshotSurface': {
        const p = snapshotSurfaceProjection(context);
        const ok = !p.manifestMarked && !p.snapshotMarked && p.snapshotPresent;
        return { ok, value: p };
      }
      case 'build': {
        const p = buildProjection(context);
        const ok = p.cjs && p.esm && p.dts && p.cjsTable && p.esmTable
          && p.standaloneCjs && p.standaloneEsm;
        return { ok, value: p };
      }
      default:
        throw new Error('Unknown target behavior check ' + name);
    }
  } catch (error) {
    return { ok: false, value: { error: String(error && error.stack ? error.stack : error) } };
  }
}

function runF2P(context, observationId, technology) {
  const names = F2P_CHECKS[observationId] || ['entry'];
  const checks = names.map((name) => {
    const result = targetBehaviorCheck(context, name);
    return { name, ok: result.ok, value: result.value };
  });
  const failed = [];
  if (!technology.passed) {
    failed.push('target technology predicate failed: ' + technology.failures.join('; '));
  }
  for (const check of checks) {
    if (!check.ok) {
      failed.push(check.name + ' behavior check failed');
    }
  }
  return {
    status: failed.length === 0 ? 'PASS' : 'FAIL',
    value: {
      type: 'json',
      value: {
        target_technology: {
          passed: technology.passed,
          failures: technology.failures,
          manifest: technology.manifest,
          entry: technology.entry,
          loaded: technology.loaded,
        },
        checks,
      },
    },
    detail: failed.length === 0 ? 'target postcondition observed' : failed.join('; '),
  };
}

function normalizedStructure(element) {
  if (!element) {
    return null;
  }
  return {
    tag: element.tagName.toLowerCase(),
    attributes: attributes(element),
    text: normalizeText(element),
    children: Array.from(element.children).map(normalizedStructure),
  };
}

function existingFixtureProjection(context) {
  const defaultRoot = context.parse(context.server.renderToStaticMarkup(context.React.createElement(context.Text))).root;
  const sanitizedRoot = context.parse(context.renderText({ markdown: true, text: FIXTURES.existing_xss_fixture })).root;
  const safeRoot = context.parse(context.renderText({ markdown: true, text: FIXTURES.safe_markdown })).root;
  const literalRoot = context.parse(context.renderText({ text: '## This is not <span>markdown</span>' })).root;
  return {
    default: normalizedStructure(defaultRoot),
    sanitized: normalizedStructure(sanitizedRoot),
    safe: normalizedStructure(safeRoot),
    literal: normalizedStructure(literalRoot),
  };
}

function diffProjection(context, observationId) {
  switch (observationId) {
    case 'DIFF-001':
      return {
        textType: typeof context.Text,
        schema: Boolean(context.indexModule.TextPropsSchema),
        defaultsText: context.indexModule.TextPropsDefaults ? context.indexModule.TextPropsDefaults.text : null,
        emailMarkdownType: typeof context.EmailMarkdown,
      };
    case 'DIFF-002':
      return markdownBranchH2Projection(context);
    case 'DIFF-003':
      return markdownBranchProjection(context);
    case 'DIFF-004':
      return emailStyleProjection(context);
    case 'DIFF-005':
      return headerRowProjection(context);
    case 'DIFF-006':
      return linksProjection(context);
    case 'DIFF-007':
      return imageRawProjection(context);
    case 'DIFF-008':
      return sanitizerEdgeProjection(context);
    case 'DIFF-009':
      return xssProjection(context, FIXTURES.existing_xss_fixture);
    case 'DIFF-010':
      return { output: context.server.renderToStaticMarkup(context.React.createElement(context.Text, { style: { fontSize: 12 }, props: { markdown: true, text: FIXTURES.strong } })) };
    case 'DIFF-011':
      return markdownBlocksProjection(context);
    case 'DIFF-012':
      return headerRowProjection(context);
    case 'DIFF-013':
      return lineBreakProjection(context);
    case 'DIFF-014':
      return rawHtmlTextProjection(context);
    case 'DIFF-015':
      return xssProjection(context, FIXTURES.existing_xss_fixture);
    case 'DIFF-016':
      return nonMarkdownLiteralProjection(context);
    case 'DIFF-017':
      return emptyVariantsProjection(context);
    case 'DIFF-018':
      return exportProjection(context);
    case 'DIFF-019':
      return { ...sanitizerEdgeProjection(context), literalText: nonMarkdownLiteralProjection(context).text, literalStrong: nonMarkdownLiteralProjection(context).strong };
    case 'DIFF-020':
      return existingFixtureProjection(context);
    case 'DIFF-021':
      return assertionStrictnessProjection(context);
    default:
      throw new Error('Unknown DIFF observation ' + observationId);
  }
}

function runDiff(context, observationId) {
  try {
    const projection = diffProjection(context, observationId);
    return { status: 'PASS', value: { type: 'json', value: projection }, detail: 'normalized projection captured' };
  } catch (error) {
    return { status: 'INCONCLUSIVE', value: { type: 'json', value: { error: String(error && error.message ? error.message : error) } }, detail: 'probe could not execute' };
  }
}

function runObservation(context, observation, technology) {
  try {
    if (observation.kind === 'P2P') {
      return runP2P(context, observation.id);
    }
    if (observation.kind === 'F2P') {
      return runF2P(context, observation.id, technology);
    }
    if (observation.kind === 'DIFFERENTIAL') {
      return runDiff(context, observation.id);
    }
    return { status: 'INCONCLUSIVE', value: { type: 'json', value: { error: 'unknown observation kind' } }, detail: 'unknown observation kind' };
  } catch (error) {
    return {
      status: 'INCONCLUSIVE',
      value: { type: 'json', value: { error: String(error && error.message ? error.message : error) } },
      detail: 'probe raised an error',
    };
  }
}

function run(side) {
  const repoRoot = resolveRepoRoot();
  const report = {
    schema_version: 'migration-observations:v6',
    batch_id: 'BATCH-INTEGRATION-0001',
    side,
    repo_root: repoRoot,
    observations: [],
  };
  let context;
  let contextError = null;
  try {
    context = createContext(repoRoot);
  } catch (error) {
    contextError = error;
  }
  let technology = null;
  if (context) {
    try {
      technology = targetTechnology(context);
    } catch (error) {
      technology = { passed: false, failures: [String(error && error.message ? error.message : error)], manifest: null, entry: null, loaded: null };
    }
  }
  for (const observation of INVENTORY) {
    if (contextError) {
      report.observations.push({
        id: observation.id,
        kind: observation.kind,
        status: 'INCONCLUSIVE',
        value: { type: 'json', value: { error: String(contextError && contextError.message ? contextError.message : contextError) } },
      });
      continue;
    }
    const result = runObservation(context, observation, technology);
    report.observations.push({
      id: observation.id,
      kind: observation.kind,
      status: result.status,
      value: result.value,
    });
  }
  return report;
}

module.exports = {
  ASSET_ROOT,
  INVENTORY,
  FIXTURES,
  resolveRepoRoot,
  manifestProjection,
  inspectStandaloneBuild,
  run,
};
