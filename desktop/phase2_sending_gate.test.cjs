'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { WaDirectClient } = require('./wa-direct.cjs');

test('Phase 2 B13: Direct WhatsApp adapter accepts Buffer and does not fall back to text', async () => {
  const client = new WaDirectClient({ authDir: path.join(__dirname, '..', '.runtime', 'test-auth') });
  client.state = 'connected';
  let sentPayload = null;
  client.sock = {
    sendMessage: async (jid, payload) => {
      sentPayload = payload;
      return { key: { id: 'wamid.HBgLMTIzNDU2' } };
    },
  };

  const sampleBuf = Buffer.from('PDF_DOCUMENT_CONTENT');
  const res = await client.sendDirect('12025550102@c.us', ['Here is your quote'], {
    buffer: sampleBuf,
    name: 'quote.pdf',
    mime: 'application/pdf',
    type: 'document',
    caption: 'Official Quote',
  });

  assert.equal(res.ok, true);
  assert.equal(res.via, 'baileys_media');
  assert.deepEqual(res.sent_ids, ['wamid.HBgLMTIzNDU2']);
  assert.ok(sentPayload, 'Payload must be sent to Baileys');
  assert.ok(Buffer.isBuffer(sentPayload.document), 'Payload document must be a Buffer');
  assert.equal(sentPayload.document.toString(), 'PDF_DOCUMENT_CONTENT');
  assert.equal(sentPayload.fileName, 'quote.pdf');
  assert.equal(sentPayload.mimetype, 'application/pdf');
});

test('Phase 2 B13: Direct WhatsApp adapter never fabricates fake receipt IDs when key.id is absent', async () => {
  const client = new WaDirectClient({ authDir: path.join(__dirname, '..', '.runtime', 'test-auth') });
  client.state = 'connected';
  client.sock = {
    sendMessage: async () => {
      // Return response without key.id (e.g. malformed or dropped receipt)
      return { status: 1 };
    },
  };

  // Test media send
  const sampleBuf = Buffer.from('IMAGE_BYTES');
  const mediaRes = await client.sendDirect('12025550102@c.us', [], {
    buffer: sampleBuf,
    type: 'image',
  });
  assert.equal(mediaRes.ok, false);
  assert.equal(mediaRes.ambiguous, true);
  assert.equal(mediaRes.reason, 'MISSING_PROVIDER_RECEIPT');

  // Test text send
  const textRes = await client.sendDirect('12025550102@c.us', ['Hello world']);
  assert.equal(textRes.ok, false);
  assert.equal(textRes.ambiguous, true);
  assert.equal(textRes.reason, 'MISSING_PROVIDER_RECEIPT');
});

test('Phase 2 B01: UI label in index.html is Turbo (Autonomous Dispatch)', () => {
  const html = fs.readFileSync(path.resolve(__dirname, 'renderer', 'index.html'), 'utf8');
  assert.ok(html.includes('<option value="turbo">Turbo (Autonomous Dispatch)</option>'), 'Turbo option must have non-contradictory Autonomous Dispatch label');
  assert.ok(!html.includes('Autonomous Supervised Dispatch'), 'Contradictory Autonomous Supervised Dispatch must be removed');
});

test('Phase 2 B01: Turbo mode bypasses Windows confirmation dialog', () => {
  const mainSrc = fs.readFileSync(path.resolve(__dirname, 'main.cjs'), 'utf8');
  // Verify that main.cjs checks for turbo mode before opening showMessageBox
  assert.ok(mainSrc.includes("cachedAutonomyMode === 'turbo'"), 'main.cjs must check cachedAutonomyMode === turbo');
  assert.ok(mainSrc.includes("if (!isTurbo)"), 'Confirmation dialog must be gated on !isTurbo');
});

test('Phase 2 Contract: wa-direct rejects sends when disconnected', async () => {
  const client = new WaDirectClient({ authDir: path.join(__dirname, '..', '.runtime', 'test-auth') });
  client.state = 'disconnected';
  const res = await client.sendDirect('12025550102@c.us', ['Hello']);
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'DIRECT_WA_DISCONNECTED');
});

test('Phase 2 F07: wa-direct echo detection suppresses human reply classification for bot sent IDs', async () => {
  let humanReplies = [];
  const client = new WaDirectClient({
    authDir: path.join(__dirname, '..', '.runtime', 'test-auth'),
    onHumanReplies: (replies) => { humanReplies.push(...replies); },
  });
  client.state = 'connected';
  client._recentSentIds.set('3EB0BOTMSG123', Date.now());

  // Simulate messages.upsert handler directly
  assert.equal(client.isRecentSentId('3EB0BOTMSG123'), true);
  assert.equal(client.isRecentSentId('true_12025550102@c.us_3EB0BOTMSG123'), true);
  assert.equal(client.isRecentSentId('UNKNOWN_MANUAL_MSG'), false);
});

test('Phase 2 Two-Bubble: sendDirect preserves part 1 sent_ids if part 2 fails', async () => {
  const client = new WaDirectClient({ authDir: path.join(__dirname, '..', '.runtime', 'test-auth') });
  client.state = 'connected';
  let callCount = 0;
  client.sock = {
    sendMessage: async (jid, payload) => {
      callCount++;
      if (callCount === 1) {
        return { key: { id: 'PART1_SUCCESS_ID' } };
      }
      throw new Error('Connection timeout on part 2');
    },
  };

  const res = await client.sendDirect('12025550102@c.us', ['Bubble 1 quote', 'Bubble 2 total']);
  assert.equal(res.ok, false);
  assert.equal(res.ambiguous, true);
  assert.deepEqual(res.sent_ids, ['PART1_SUCCESS_ID']);
  assert.equal(res.parts_sent, 1);
  assert.equal(res.total_parts, 2);
  assert.ok(client.isRecentSentId('PART1_SUCCESS_ID'));
});
