#!/usr/bin/env node
/*
 * zim_indexer.js - Nomad ZIM preprocessing.
 *
 * The ESP32 cannot random-access a multi-GB ZIM to answer a search: a title
 * binary search is dozens of scattered range reads into a 2GB FAT32 file, which
 * on real hardware is punishingly slow and floods/wedges the async web server.
 * So we do that work here on the PC (where random access is cheap) and emit a
 * compact sidecar index the device can search with ~2 small, sequential reads.
 *
 *   node zim_indexer.js <cardRoot>
 *
 * Reads every ZIM under <cardRoot>/Archive (single or FAT32-split) and writes
 * <cardRoot>/Archive/.nomad-zim/:
 *   manifest.json  - archives + where each one's article HTML lives (cluster/blob)
 *   words.dat      - every (word -> article) pair, sorted by word. Tab-separated:
 *                      word \t title \t archiveIdx \t cluster \t blob
 *                    Denormalized on purpose: one chunk read yields everything
 *                    needed to show AND open a result, no second lookup.
 *   words.skp      - sparse skip table {step, keys:[[word, byteOffset], ...]}
 *                    sampled every `step` lines, so the device binary-searches
 *                    this small file in RAM then range-reads exactly one chunk.
 *
 * Indexing reads only dirents (titles + cluster/blob numbers) - never article
 * bodies - so no decompression is needed and it stays fast. Redirects are
 * resolved to their target's cluster/blob so device opens are a single hop.
 *
 * Reuses the device's own reader (SD_Card_Template/assets/nomad-zim.js) so the
 * offsets written here are guaranteed to match how the device reads them.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

// Load the SAME nomad-zim.js the device runs, so the cluster/blob offsets we
// record here are read back identically on the device. Preference: the card's
// own /assets/nomad-zim.js (guaranteed to match that card), then the Manager's
// template, then an explicit override.
function loadNomadZim(cardRoot) {
  const candidates = [
    process.env.NOMAD_ZIM_JS,
    cardRoot && path.join(cardRoot, 'assets', 'nomad-zim.js'),
    path.resolve(__dirname, '../sd_template/assets/nomad-zim.js'),
    path.resolve(__dirname, 'nomad-zim.js'),
    path.resolve(__dirname, '../../jcorp-nomad-main (4)/jcorp-nomad-main/SD_Card_Template/assets/nomad-zim.js'),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(c)) { return { mod: require(c), from: c }; }
  throw new Error('nomad-zim.js not found (need it on the card at /assets/, in sd_template/assets/, or via NOMAD_ZIM_JS). Tried:\n  ' + candidates.join('\n  '));
}
let NomadZim; // populated in main() from the card's own reader

const FORMAT_VERSION = 2;   // v2: words.dat sharded by first char (FAT32 <4GB/file)
const SKIP_STEP = 512;               // sample the skip table every N lines
const LIST_MAX = 80000;              // max articles to emit a per-archive browse list for
const MAX_WORDS_PER_TITLE = 12;
const MIN_WORD_LEN = 2;
const LIMIT = parseInt(process.env.NOMAD_ZIM_LIMIT || '0', 10) || 0; // 0 = all (testing knob)

function log(msg) { process.stdout.write(msg + '\n'); }

// --- fs-backed byte source with the same interface HttpSource exposes ---
class FileSource {
  constructor(parts) {
    this.parts = parts.map(p => ({ path: p.path, size: p.size, fd: fs.openSync(p.path, 'r') }));
    this.size = this.parts.reduce((a, p) => a + p.size, 0);
  }
  read(offset, length) {
    if (offset >= this.size) return Promise.resolve(new Uint8Array(0));
    if (offset + length > this.size) length = this.size - offset;
    const out = Buffer.alloc(length);
    let written = 0, partStart = 0;
    for (const p of this.parts) {
      const partEnd = partStart + p.size;
      if (offset + written < partEnd && written < length) {
        const local = offset + written - partStart;
        const take = Math.min(p.size - local, length - written);
        fs.readSync(p.fd, out, written, take, local);
        written += take;
      }
      partStart = partEnd;
    }
    return Promise.resolve(new Uint8Array(out.subarray(0, written)));
  }
  close() { for (const p of this.parts) try { fs.closeSync(p.fd); } catch (e) {} }
}

// --- discover archives under /Archive, grouping split parts (kiwix zimsplit) ---
function discoverArchives(archiveDir) {
  const groups = new Map();
  for (const name of fs.readdirSync(archiveDir)) {
    if (name.startsWith('.')) continue;
    const full = path.join(archiveDir, name);
    let st; try { st = fs.statSync(full); } catch (e) { continue; }
    if (!st.isFile()) continue;
    const lower = name.toLowerCase();
    const isZim = lower.endsWith('.zim') ||
      (lower.length > 6 && lower.slice(-6, -2) === '.zim' &&
       lower[lower.length - 2] >= 'a' && lower[lower.length - 2] <= 'z' &&
       lower[lower.length - 1] >= 'a' && lower[lower.length - 1] <= 'z');
    if (!isZim) continue;
    let id = name, split = false;
    if (lower.length > 6 && lower.slice(-6, -2) === '.zim' && !lower.endsWith('.zim')) {
      split = true; id = name.slice(0, -6);
    } else { id = name.replace(/\.zim$/i, ''); }
    if (!groups.has(id)) groups.set(id, { id, split: false, parts: [] });
    const g = groups.get(id);
    if (split) g.split = true;
    g.parts.push({ path: full, rel: '/Archive/' + name, size: st.size });
  }
  const out = [...groups.values()];
  for (const g of out) {
    g.parts.sort((a, b) => a.rel < b.rel ? -1 : 1);
    g.totalSize = g.parts.reduce((s, p) => s + p.size, 0);
  }
  return out;
}

const STOPWORDS = new Set(['the', 'a', 'an', 'of', 'and', 'or', 'to', 'in', 'on', 'for', 'is', 'at', 'by']);
function tokenize(title) {
  const words = [];
  const seen = new Set();
  for (const raw of title.toLowerCase().split(/[^a-z0-9]+/)) {
    if (raw.length < MIN_WORD_LEN) continue;
    if (STOPWORDS.has(raw)) continue;
    if (seen.has(raw)) continue;
    seen.add(raw);
    words.push(raw);
    if (words.length >= MAX_WORDS_PER_TITLE) break;
  }
  return words;
}

// A tab/newline-safe single-line title for the .dat file.
function cleanTitle(t) { return (t || '').replace(/[\t\r\n]+/g, ' ').trim(); }

async function indexArchive(zim, archiveIdx, unsortedStream, titleRows) {
  const count = zim._titleIndex.count;
  const total = LIMIT ? Math.min(LIMIT, count) : count;
  let articles = 0, pairs = 0, lastPct = -1;
  for (let i = 0; i < total; i++) {
    let e;
    try { e = await zim._direntByTitleIndex(i); } catch (err) { continue; }
    if (e.namespace !== 'C' && e.namespace !== 'A') continue;
    // Resolve redirects so the stored cluster/blob opens in one hop.
    let target = e;
    if (e.isRedirect) {
      try { target = await zim.resolveRedirect(e); } catch (err) { continue; }
      if (target.isRedirect) continue;
    }
    const mime = target.mimeType || '';
    if (mime.indexOf('text/html') !== 0) continue; // articles only
    const title = cleanTitle(e.title || e.url);
    if (!title) continue;
    const words = tokenize(title);
    if (!words.length) continue;
    const suffix = '\t' + title + '\t' + archiveIdx + '\t' + target.clusterNumber + '\t' + target.blobNumber + '\n';
    // One entry per significant word (finds "Albert Einstein" from "einstein").
    let writable = true;
    for (const w of words) { writable = unsortedStream.write(w + suffix); pairs++; }
    // Plus one entry keyed by the full normalized title, so a multi-word query
    // ("world war") prefix-matches the real title ("world war ii") instead of
    // any title that merely contains each word as a substring.
    const fullKey = title.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    if (fullKey && fullKey !== words[0]) { writable = unsortedStream.write(fullKey + suffix); pairs++; }
    // Honor backpressure. FileSource.read is SYNCHRONOUS (fs.readSync), so every
    // `await` above resolves on the microtask queue and the loop never returns to
    // the macrotask phase where the fs write-stream flushes to disk. Without this,
    // millions of buffered write()s pile up in RAM and blow the V8 heap (~4GB) on
    // a full-Wikipedia index. Awaiting 'drain' both frees the buffer and yields.
    if (!writable) { await new Promise(res => unsortedStream.once('drain', res)); }
    // Per-archive browse list (one row per article). Collected only while the
    // archive is still under the cap; big archives (Wikipedia) rely on their
    // own alphabetical main-page index instead.
    if (titleRows && titleRows.length <= LIST_MAX) {
      titleRows.push(title + '\t' + target.clusterNumber + '\t' + target.blobNumber);
    }
    articles++;
    const pct = Math.floor(i * 100 / total);
    if (pct !== lastPct && pct % 5 === 0) {
      log(`    ${zim.__id}: ${pct}% (${articles} articles, ${pairs} words)`);
      // Machine-readable progress for the manager UI: the title walk is the first
      // ~45% of "Build search index" (sort ~5%, sharding the last ~50%).
      log(`@@PCT ${Math.floor(pct * 0.45)} indexing titles — ${articles.toLocaleString()} articles`);
      lastPct = pct;
    }
  }
  return { articles, pairs };
}

async function main() {
  const cardRoot = process.argv[2];
  if (!cardRoot) { log('usage: node zim_indexer.js <cardRoot>'); process.exit(2); }
  const archiveDir = path.join(cardRoot, 'Archive');
  if (!fs.existsSync(archiveDir)) { log('No /Archive directory on card: ' + archiveDir); process.exit(1); }

  const zimLib = loadNomadZim(cardRoot);
  NomadZim = zimLib.mod;
  log('Reader: ' + zimLib.from);

  const archives = discoverArchives(archiveDir);
  if (!archives.length) { log('No ZIM files found under /Archive.'); process.exit(1); }
  log(`Found ${archives.length} archive(s):`);
  for (const a of archives) log(`  • ${a.id} (${(a.totalSize / 1e6).toFixed(0)}MB${a.split ? ', ' + a.parts.length + ' parts' : ''})`);

  const outDir = path.join(archiveDir, '.nomad-zim');
  fs.mkdirSync(outDir, { recursive: true });
  const tmpUnsorted = path.join(os.tmpdir(), `nomad-words-${process.pid}.tsv`);
  // 4MB buffer: batches disk writes so the tight (sync-read) index loop stalls on
  // 'drain' far less often than the 16KB default would - see the backpressure note
  // in indexArchive. Bounded, so RAM stays flat even on a full-Wikipedia index.
  const unsorted = fs.createWriteStream(tmpUnsorted, { highWaterMark: 1 << 22 });

  const manifestArchives = [];
  let totalArticles = 0, totalPairs = 0;

  for (let idx = 0; idx < archives.length; idx++) {
    const a = archives[idx];
    log(`\nIndexing [${idx + 1}/${archives.length}] ${a.id} …`);
    const src = new FileSource(a.parts);
    const zim = new NomadZim.ZimArchive(src, {});
    zim.__id = a.id;
    let header;
    try { header = await zim.open(); }
    catch (err) { log(`  ! open failed: ${err.message}; skipping`); src.close(); continue; }
    if (zim._titleIndex.mode === 'none') { log('  ! no title index in this ZIM; skipping'); src.close(); continue; }

    // Main page location (for a future "browse" landing).
    let mainCluster = -1, mainBlob = -1;
    try {
      const mp = await zim.getMainPage();
      if (mp && !mp.isRedirect) { mainCluster = mp.clusterNumber; mainBlob = mp.blobNumber; }
    } catch (e) {}

    const titleRows = [];
    const res = await indexArchive(zim, idx, unsorted, titleRows);
    totalArticles += res.articles; totalPairs += res.pairs;

    // Write a per-archive browse list for reasonably-sized archives (TED,
    // Gutenberg, php.net...). This backs the reader's own book/talk list, since
    // these ZIMs' built-in browse pages (bookshelves, author lists, language
    // pickers) are JavaScript-driven and can't run in the sandboxed reader.
    let hasList = false;
    if (res.articles > 0 && titleRows.length <= LIST_MAX) {
      titleRows.sort((x, y) => {
        const nx = x.toLowerCase(), ny = y.toLowerCase();
        return nx < ny ? -1 : nx > ny ? 1 : 0;
      });
      fs.writeFileSync(path.join(outDir, idx + '.list'), titleRows.join('\n') + '\n');
      hasList = true;
      log(`  → wrote browse list (${titleRows.length} titles)`);
    }

    manifestArchives.push({
      idx, id: a.id, name: a.id, split: a.split, totalSize: a.totalSize,
      parts: a.parts.map(p => ({ path: p.rel, size: p.size })),
      articleCount: res.articles, mainCluster, mainBlob, hasList,
    });
    log(`  → ${res.articles} articles, ${res.pairs} word entries`);
    src.close();
  }
  await new Promise(r => unsorted.end(r));

  if (!totalPairs) { log('\nNo indexable articles found; nothing written.'); process.exit(1); }

  // External sort by word (col 1). Uses the system `sort` so memory stays flat.
  // Sort to a TEMP file on fast local disk (NOT the card): the full-Wikipedia
  // sorted stream is ~6-7GB - over FAT32's 4GB per-file cap - so it can never
  // live on the card as one file. We shard it below.
  const tmpSorted = path.join(os.tmpdir(), `nomad-words-sorted-${process.pid}.tsv`);
  log(`\nSorting ${totalPairs} word entries …`);
  log(`@@PCT 47 sorting ${totalPairs.toLocaleString()} word entries`);
  // Cross-platform sort. On Linux/mac the system `sort` is fast and keeps memory
  // flat; on Windows (whose sort.exe is incompatible) — or anywhere the system
  // sort is missing — we fall back to a built-in external merge sort. Both paths
  // produce byte-identical output to `LC_ALL=C sort -t '\t' -k1,1 -s`: a stable,
  // unsigned-byte sort on column 1 (the word key). See sortByKey().
  await sortByKey(tmpUnsorted, tmpSorted, log);
  fs.unlinkSync(tmpUnsorted);

  // Shard the sorted index by the word key's first character: words-<c>.dat plus
  // a per-shard skip table words-<c>.skp. A single words.dat for full English
  // Wikipedia is ~6.7GB - impossible on FAT32; shards keep every file well under
  // 4GB AND let the device load just the one shard's skip table a query needs
  // (see NomadArchiveIndex._getShard). The sorted file groups each first-char's
  // rows contiguously, so one sequential streaming pass emits every shard.
  log('Sharding index by first character (FAT32 4GB-safe) …');
  const shards = await splitShards(tmpSorted, outDir);
  fs.unlinkSync(tmpSorted);

  let indexBytes = 0, maxShard = 0;
  Object.keys(shards).forEach(c => { indexBytes += shards[c].bytes; if (shards[c].bytes > maxShard) maxShard = shards[c].bytes; });

  const manifest = {
    version: FORMAT_VERSION,
    built: new Date().toISOString(),
    articleCount: totalArticles,
    wordCount: totalPairs,
    skipStep: SKIP_STEP,
    sharded: true,
    shards,                       // { "a": { dat, skp, bytes }, ... }
    archives: manifestArchives,
  };
  fs.writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2));
  spawnSync('sync');

  log(`\nDone.`);
  log(`  archives : ${manifestArchives.length}`);
  log(`  articles : ${totalArticles}`);
  log(`  words    : ${totalPairs}`);
  log(`  index    : ${(indexBytes / 1e6).toFixed(1)}MB across ${Object.keys(shards).length} shards (largest ${(maxShard / 1e6).toFixed(1)}MB)`);
  log(`  output   : ${outDir}`);
}

// Stream the sorted TSV and emit one shard per first character of the word key:
//   words-<c>.dat  - rows whose key starts with <c> (still word-sorted)
//   words-<c>.skp  - { step, keys:[[word, byteOffsetInShard], ...] } sampled every
//                    SKIP_STEP lines; offsets are RELATIVE to that shard file.
// Keys are lowercase [a-z0-9] (see tokenize), so <c> is always a filename-safe,
// FAT32-case-safe char. Honors write backpressure so RAM stays flat on a
// multi-GB index. Returns { "<c>": { dat, skp, bytes } }.
async function splitShards(sortedPath, outDir) {
  const readline = require('readline');
  const totalSize = fs.statSync(sortedPath).size || 1;
  const rl = readline.createInterface({
    input: fs.createReadStream(sortedPath, { highWaterMark: 1 << 20 }),
    crlfDelay: Infinity,
  });
  const shards = {};
  let curChar = null, curStream = null, curBytes = 0, curLine = 0, curKeys = null, curDat = null, curSkp = null;
  let processed = 0, lastP = -1;   // for @@PCT progress (sharding = last 50%)

  const finalize = async () => {
    if (!curStream) return;
    await new Promise(r => curStream.end(r));
    fs.writeFileSync(path.join(outDir, curSkp), JSON.stringify({ step: SKIP_STEP, keys: curKeys }));
    shards[curChar] = { dat: curDat, skp: curSkp, bytes: curBytes };
  };

  for await (const line of rl) {
    if (!line) continue;
    const ch = line[0];
    if (ch !== curChar) {
      await finalize();
      curChar = ch; curBytes = 0; curLine = 0; curKeys = [];
      const safe = /[a-z0-9]/.test(ch) ? ch : 'u' + ch.charCodeAt(0);
      curDat = `words-${safe}.dat`; curSkp = `words-${safe}.skp`;
      curStream = fs.createWriteStream(path.join(outDir, curDat), { highWaterMark: 1 << 22 });
    }
    if (curLine % SKIP_STEP === 0) {
      const tab = line.indexOf('\t');
      curKeys.push([tab < 0 ? line : line.slice(0, tab), curBytes]);
    }
    const nbytes = Buffer.byteLength(line, 'utf8') + 1;
    const ok = curStream.write(line + '\n');
    curBytes += nbytes;                                // byte offset of the NEXT line's start
    processed += nbytes;
    curLine++;
    const p = 50 + Math.floor(processed * 50 / totalSize);
    if (p !== lastP) { log(`@@PCT ${p} writing shard ${curChar} (${Object.keys(shards).length + 1} shards)`); lastP = p; }
    if (!ok) await new Promise(r => curStream.once('drain', r));
  }
  await finalize();
  log('@@PCT 100 index complete');
  return shards;
}

// ── cross-platform external sort ──────────────────────────────────────────────
// Reproduces `LC_ALL=C sort -t '\t' -k1,1 -s`: a STABLE sort on column 1 (the
// text up to the first tab) using unsigned byte order. Keys are ASCII here, so
// JS string comparison equals byte order; stability (GNU -s) means equal keys
// keep their original input order, which the device's binary search relies on.

const RUN_BYTES = 128 * 1024 * 1024;   // in-memory line data per sorted run

function keyOf(line) { const t = line.indexOf('\t'); return t < 0 ? line : line.slice(0, t); }

// Sort inPath -> outPath. Prefers the system sort off Windows; else built-in.
async function sortByKey(inPath, outPath, log) {
  // NOMAD_FORCE_JS_SORT=1 forces the built-in sort even on Linux/mac — lets you
  // exercise the Windows path in tests. Output is identical either way.
  if (process.platform !== 'win32' && !process.env.NOMAD_FORCE_JS_SORT) {
    const env = Object.assign({}, process.env, { LC_ALL: 'C' });
    const r = spawnSync('sort', ['-t', '\t', '-k1,1', '-s', '-o', outPath, inPath],
      { env, stdio: 'inherit', maxBuffer: 1 << 30 });
    if (r.status === 0) return;
    const why = r.error ? r.error.code : ('exit ' + r.status);
    log(`  system sort unavailable (${why}); using built-in sort`);
  }
  await jsExternalSort(inPath, outPath, log);
}

// Stable in-place sort of a line array by key (V8's Array.sort is stable, so
// equal keys keep input order — matching GNU -s). Precomputes keys once.
function stableSortLines(lines) {
  const keyed = lines.map(line => ({ k: keyOf(line), line }));
  keyed.sort((x, y) => (x.k < y.k ? -1 : x.k > y.k ? 1 : 0));
  for (let i = 0; i < keyed.length; i++) lines[i] = keyed[i].line;
}

async function jsExternalSort(inPath, outPath, log) {
  const readline = require('readline');
  const runs = [];
  let buf = [], bytes = 0;

  const flushRun = () => {
    if (!buf.length) return;
    stableSortLines(buf);
    const runPath = path.join(os.tmpdir(), `nomad-run-${process.pid}-${runs.length}.tsv`);
    fs.writeFileSync(runPath, buf.join('\n') + '\n');
    runs.push(runPath);
    log(`@@PCT 48 building sort runs (${runs.length})`);
    buf = []; bytes = 0;
  };

  const rl = readline.createInterface({
    input: fs.createReadStream(inPath, { highWaterMark: 1 << 20 }), crlfDelay: Infinity,
  });
  for await (const line of rl) {
    if (!line) continue;
    buf.push(line);
    bytes += Buffer.byteLength(line, 'utf8') + 1;
    if (bytes >= RUN_BYTES) flushRun();
  }
  flushRun();

  if (runs.length === 0) { fs.writeFileSync(outPath, ''); return; }
  if (runs.length === 1) { fs.renameSync(runs[0], outPath); return; }
  mergeRuns(runs, outPath, log);
  for (const r of runs) { try { fs.unlinkSync(r); } catch (e) {} }
}

// Synchronous UTF-8-safe line reader over one run file (runs live on fast local
// disk, so blocking reads keep the k-way merge simple and correct).
class RunReader {
  constructor(filePath) {
    const { StringDecoder } = require('string_decoder');
    this.fd = fs.openSync(filePath, 'r');
    this.buf = Buffer.allocUnsafe(1 << 20);
    this.dec = new StringDecoder('utf8');
    this.rest = '';
    this.eof = false;
  }
  next() {                              // next line (no newline), or null at EOF
    for (;;) {
      const nl = this.rest.indexOf('\n');
      if (nl >= 0) { const s = this.rest.slice(0, nl); this.rest = this.rest.slice(nl + 1); return s; }
      if (this.eof) {
        if (this.rest.length) { const s = this.rest; this.rest = ''; return s; }
        return null;
      }
      const n = fs.readSync(this.fd, this.buf, 0, this.buf.length, null);
      if (n === 0) { this.eof = true; this.rest += this.dec.end(); continue; }
      this.rest += this.dec.write(this.buf.subarray(0, n));
    }
  }
  close() { try { fs.closeSync(this.fd); } catch (e) {} }
}

// K-way merge of pre-sorted runs into outPath, preserving stability: on equal
// keys the lower run index wins (earlier runs hold earlier input lines).
function mergeRuns(runPaths, outPath, log) {
  const readers = runPaths.map(p => new RunReader(p));
  const heap = [];                      // min-heap of { k, line, run }
  const less = (a, b) => a.k < b.k || (a.k === b.k && a.run < b.run);
  const up = (i) => { while (i > 0) { const p = (i - 1) >> 1; if (less(heap[i], heap[p])) { [heap[i], heap[p]] = [heap[p], heap[i]]; i = p; } else break; } };
  const down = (i) => { for (;;) { let s = i, l = 2 * i + 1, r = 2 * i + 2; if (l < heap.length && less(heap[l], heap[s])) s = l; if (r < heap.length && less(heap[r], heap[s])) s = r; if (s === i) break; [heap[i], heap[s]] = [heap[s], heap[i]]; i = s; } };
  const push = (x) => { heap.push(x); up(heap.length - 1); };
  const pop = () => { const top = heap[0], last = heap.pop(); if (heap.length) { heap[0] = last; down(0); } return top; };

  for (let run = 0; run < readers.length; run++) {
    const line = readers[run].next();
    if (line !== null) push({ k: keyOf(line), line, run });
  }
  const out = fs.openSync(outPath, 'w');
  let wbuf = '', wlen = 0;
  const flush = () => { if (wbuf) { fs.writeSync(out, wbuf); wbuf = ''; wlen = 0; } };
  while (heap.length) {
    const top = pop();
    wbuf += top.line + '\n'; wlen += top.line.length + 1;
    if (wlen >= (1 << 22)) flush();
    const nl = readers[top.run].next();
    if (nl !== null) push({ k: keyOf(nl), line: nl, run: top.run });
  }
  flush();
  fs.closeSync(out);
  for (const r of readers) r.close();
}

main().catch(err => { log('FATAL: ' + (err && err.stack || err)); process.exit(1); });
