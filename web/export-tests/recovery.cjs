// Pure actual-component probes. Authored hook/storage/fetch adapters, not backend proof.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const web = path.resolve(__dirname, '..');
const props = { username: 'authored-account', workspace: '00000000-0000-4000-8000-000000000001', caseId: '00000000-0000-4000-8000-000000000002', version: 7, roles: ['reviewer'], csrf: 'authored-csrf' };
const key = '00000000-0000-4000-8000-000000000010';
const job = '00000000-0000-4000-8000-000000000003';
const storageKey = JSON.stringify(['folio-export-v1', props.username, props.workspace, props.caseId]);
const saved = JSON.stringify({ key, version: 7, job: '' });
let compiled;
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness(initial, opts = {}) {
  const hooks = [], effects = [], writes = [], requests = [], publications = [];
  let cursor = 0, first = true, tree, unmounts = [], readCount = 0, stored = initial;
  const React = { createElement: (type, p, ...children) => ({ type, props: p || {}, children }), Fragment: 'fragment',
    useState: init => { const i = cursor++; if (first) hooks[i] = typeof init === 'function' ? init() : init; return [hooks[i], v => hooks[i] = v]; },
    useRef: init => { const i = cursor++; if (first) hooks[i] = { current: init }; return hooks[i]; },
    useEffect: fn => { cursor++; if (first) effects.push(fn); } };
  React.default = React;
  const context = { require: n => { assert.equal(n, 'react'); return React; }, exports: {}, crypto: opts.crypto || webcrypto, AbortController, TypeError, Error, JSON, Number, Uint8Array, Array, Blob,
    URL: { createObjectURL: () => { publications.push('blob'); return 'blob:owned'; }, revokeObjectURL: () => {} }, setTimeout: () => {},
    document: { createElement: () => ({ click: () => publications.push('download') }) },
    sessionStorage: { getItem: () => { if (opts.failRead && ++readCount === 1) throw new Error('authored transient storage read failure'); return stored; }, setItem: (k, v) => { assert.equal(k, storageKey); stored = v; writes.push(JSON.parse(v)); }, removeItem: () => { stored = null; } },
    fetch: async (url, init) => { requests.push({ url, method: init.method || 'GET', headers: init.headers, body: init.body, signal: init.signal }); if (opts.fetch) return opts.fetch(url, init); return { ok: true, json: async () => url.endsWith('/exports') ? { artifact_job_id: job } : { artifact_job_id: job, state: 'queued', manifest: {}, sha256: null, bytes: null } }; } };
  vm.runInNewContext(compiled, context, { filename: 'EvidenceExport.transpiled.cjs' });
  function render() { cursor = 0; tree = context.exports.EvidenceExport({ ...props, ...opts.props }); first = false; return tree; }
  function nodes(n = tree) { if (!n || typeof n !== 'object') return []; return [n, ...(n.children || []).flat(Infinity).filter(c => c && typeof c === 'object').flatMap(c => nodes(c))]; }
  function text(n) { return (n.children || []).flat(Infinity).map(c => typeof c === 'object' && c ? text(c) : String(c || '')).join(''); }
  function button(label) { const b = nodes().find(n => n.type === 'button' && text(n) === label); assert.ok(b, 'button ' + label); return b; }
  render();
  return { mount: async () => { for (const e of effects) { const clean = e(); if (typeof clean === 'function') unmounts.push(clean); } await tick(); render(); }, render, button,
    click: async label => { const b = button(label); assert.ok(!b.props.disabled, 'enabled ' + label); await b.props.onClick(); await tick(); render(); }, requests, writes, publications,
    getStored: () => stored, unmount: () => unmounts.forEach(fn => fn()), alert: () => nodes().filter(n => n.props.role === 'alert').map(text).join(' ') };
}
const cases = {
  async storageTypes() {
    const invalid = ['{', 'null', 'false', '7', '"text"', '[]', '{}',
      JSON.stringify({ key: [key], version: 7, job: '' }),
      JSON.stringify({ key, version: 7, job: [job] }),
      JSON.stringify({ key, version: '7', job: '' }),
      ...[0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1].map(version => JSON.stringify({ key, version, job: '' })),
      JSON.stringify({ key: 7, version: 7, job: '' }), JSON.stringify({ key, version: 7, job: null })];
    for (const value of invalid) {
      const a = harness(value); await a.mount();
      assert.equal(a.requests.length, 0, 'invalid record must not request status: ' + value);
      assert.equal(a.button('Export evidence JSON').props.disabled, true, 'invalid record blocked: ' + value);
      await a.click('Retry recovery read');
      assert.equal(a.requests.length, 0); assert.equal(a.writes.length, 0); assert.equal(a.getStored(), value);
    }
    const a = harness(JSON.stringify({ key, version: 7, job: '', extra: 'AUTHORED_SHOULD_NOT_BE_COPIED' }));
    await a.mount(); await a.click('Retry same export request');
    assert.ok(a.writes.length > 0);
    for (const value of a.writes) assert.deepEqual(Object.keys(value).sort(), ['job', 'key', 'version']);
    const absent = harness(null); await absent.mount(); await absent.click('Export evidence JSON');
    assert.equal(absent.requests.filter(r => r.method === 'POST').length, 1);
    const ready = harness(JSON.stringify({ key, version: 2147483647, job })); await ready.mount();
    assert.equal(ready.requests.length, 1); assert.equal(ready.requests[0].method, 'GET'); assert.equal(ready.alert(), '');
  },
  async acknowledgementType() {
    const a = harness(saved, { fetch: async () => ({ ok: true, json: async () => ({ artifact_job_id: [job] }) }) });
    await a.mount(); await a.click('Retry same export request');
    assert.match(a.alert(), /Invalid export job identity/);
    assert.equal(a.requests.length, 1, 'array acknowledgement cannot drive a status read');
    assert.equal(a.getStored(), saved, 'retain original frozen command');
  },
  async boundedDownload() {
    const raw = new TextEncoder().encode('{}');
    const hash = Buffer.from(await webcrypto.subtle.digest('SHA-256', raw)).toString('hex');
    for (const scenario of [
      { length: '262145', chunks: [], reject: true, reads: 0 },
      { length: null, chunks: [new Uint8Array(131072), new Uint8Array(131072), new Uint8Array(1)], reject: true, reads: 3 },
      { length: '1', chunks: [new Uint8Array(262144), new Uint8Array(1)], reject: true, reads: 2 },
      { length: null, chunks: [raw.subarray(0, 1), raw.subarray(1)], reject: false, reads: 3 },
      { length: '1', chunks: [raw], reject: false, reads: 2 },
    ]) {
      let reads = 0, cancels = 0, buffered = 0, digests = 0;
      const response = { ok: true,
        arrayBuffer: async () => { buffered++; return raw.buffer; },
        body: { getReader: () => ({ read: async () => { const value = scenario.chunks[reads++]; return value ? { value, done: false } : { done: true }; }, cancel: async () => { cancels++; }, releaseLock: () => {} }) },
        headers: { get: n => n === 'Content-Length' ? scenario.length : n === 'ETag' ? `"sha256-${hash}"` : null } };
      const a = harness(JSON.stringify({ key, version: 7, job }), {
        crypto: { subtle: { digest: async (...args) => { digests++; return webcrypto.subtle.digest(...args); } } },
        fetch: async url => url.endsWith('?download=true') ? response : { ok: true, json: async () => ({ artifact_job_id: job, state: 'ready', manifest: {}, sha256: hash, bytes: raw.length }) }
      });
      await a.mount(); await a.click('Download evidence JSON');
      assert.equal(buffered, 0, 'never materialize unbounded arrayBuffer');
      assert.equal(reads, scenario.reads); assert.equal(cancels, scenario.reject ? 1 : 0);
      assert.equal(digests, scenario.reject ? 0 : 1, 'oversize rejected before digest');
      assert.equal(a.publications.length, scenario.reject ? 0 : 2);
      if (scenario.reject) assert.match(a.alert(), /integrity check failed/);
    }
  },
  async actionableErrors() {
    for (const [status, detail, guidance] of [
      [409, 'IDEMPOTENCY_DIGEST_MISMATCH', /identity conflicts.*operator/i],
      [503, 'OUTCOME_UNKNOWN', /Retry the same export request/],
      [422, 'EXPORT_INPUT_TOO_LARGE', /size limits.*operator/i],
      [422, 'EXPORT_TOO_LARGE', /size limits.*operator/i],
      [412, 'STALE_VERSION', /Original request identity retained/],
      [422, 'REDACTION_INVALID', /metadata-only audit-v1/],
    ]) {
      const a = harness(saved, { fetch: async () => ({ ok: false, status, json: async () => ({ detail }) }) });
      await a.mount(); await a.click('Retry same export request');
      assert.match(a.alert(), guidance, 'actionable POST ' + status + ' ' + detail);
      assert.equal(a.getStored(), saved, 'HTTP errors must not discard identity');
      await a.click('Retry same export request');
      assert.equal(a.requests.length, 2); assert.equal(a.requests[1].headers['Idempotency-Key'], key);
      assert.equal(a.requests[1].body, a.requests[0].body);
    }
  },
  async lateIdentityCallbacks() {
    for (const changed of [{ username: 'other-authored-account' }, { workspace: '00000000-0000-4000-8000-000000000004' }, { caseId: '00000000-0000-4000-8000-000000000005' }]) {
      let release, entered;
      const barrier = new Promise(r => release = r), started = new Promise(r => entered = r);
      const a = harness(saved, { fetch: async () => ({ ok: true, json: async () => { entered(); await barrier; return { artifact_job_id: job }; } }) });
      await a.mount();
      const inflight = a.click('Retry same export request'); await started;
      a.unmount();
      // main.tsx keys the real component by account/workspace/case: switch remounts.
      const b = harness(null, { props: changed }); await b.mount();
      release(); await inflight;
      assert.equal(a.requests[0].signal.aborted, true);
      assert.equal(a.requests.length, 1, 'late acknowledgement must not start status after identity switch');
      assert.equal(a.writes.length, 1, 'late acknowledgement must not persist after unmount');
      assert.equal(b.requests.length, 0); assert.equal(b.writes.length, 0);
    }
  },
  async unmountDuringStream() {
    const raw = new TextEncoder().encode('{}');
    const hash = Buffer.from(await webcrypto.subtle.digest('SHA-256', raw)).toString('hex');
    let release, entered, reads = 0;
    const barrier = new Promise(r => release = r), started = new Promise(r => entered = r);
    const a = harness(JSON.stringify({ key, version: 7, job }), { fetch: async url => url.endsWith('?download=true') ? { ok: true, headers: { get: n => n === 'ETag' ? `"sha256-${hash}"` : null }, body: { getReader: () => ({ read: async () => { entered(); await barrier; return reads++ ? { done: true } : { value: raw, done: false }; }, cancel: async () => {}, releaseLock: () => {} }) } } : { ok: true, json: async () => ({ artifact_job_id: job, state: 'ready', manifest: {}, sha256: hash, bytes: raw.length }) } });
    await a.mount(); const inflight = a.click('Download evidence JSON'); await started; a.unmount(); release(); await inflight;
    assert.equal(a.publications.length, 0); assert.equal(a.requests[1].signal.aborted, true);
  },
  async readFailure() {
    const a = harness(saved, { failRead: true, props: { version: 9 } }); await a.mount();
    assert.equal(a.button('Export evidence JSON').props.disabled, true, 'unreadable storage must not permit a new command');
    assert.equal(a.requests.length, 0); assert.equal(a.writes.length, 0); assert.equal(a.getStored(), saved);
    assert.match(a.alert(), /recovery/i);
    await a.click('Retry recovery read');
    assert.equal(a.requests.length, 0, 'recovery read never auto-POSTs');
    await a.click('Retry same export request');
    const post = a.requests.find(r => r.method === 'POST');
    assert.equal(post.headers['Idempotency-Key'], key); assert.equal(post.headers['If-Match'], '"case-v7"');
    assert.equal(post.body, JSON.stringify({ case_version: 7, include_originals: false, redaction_profile: 'audit-v1' }));
  },
};
(async () => {
  const { pathToFileURL } = require('node:url');
  const { transformWithOxc } = await import(pathToFileURL(path.join(web, 'node_modules/vite/dist/node/index.js')));
  const src = fs.readFileSync(path.join(web, 'src/EvidenceExport.tsx'), 'utf8');
  compiled = (await transformWithOxc(src, 'EvidenceExport.tsx', { jsx: { runtime: 'classic' } })).code
    .replace('import React, { useEffect, useRef, useState } from "react";', 'const React=require("react"); const {useEffect,useRef,useState}=React;')
    .replace('export function EvidenceExport', 'exports.EvidenceExport = function EvidenceExport');
  assert.ok(compiled.includes('exports.EvidenceExport = function'));
  for (const name of process.argv[2] ? [process.argv[2]] : Object.keys(cases)) { assert.ok(cases[name], name); await cases[name](); console.log('PASS ' + name); }
})().catch(e => { console.error(e); process.exitCode = 1; });
