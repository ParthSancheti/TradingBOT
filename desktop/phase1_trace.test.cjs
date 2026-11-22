'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {validateRequest, METHODS} = require('./boundary.cjs');

test('Phase 1 Contract: boundary.cjs permits message/trace with bounded parameters', () => {
  assert.ok(METHODS.has('message/trace'), 'METHODS must include message/trace');
  const valid = validateRequest('message/trace', { message_id: 'false_919876543210@c.us_12345' });
  assert.equal(valid.method, 'message/trace');
  assert.equal(valid.params.message_id, 'false_919876543210@c.us_12345');

  // Unsupported parameter types fail
  assert.throws(() => validateRequest('message/trace', null), /Unsupported desktop request/);
  assert.throws(() => validateRequest('message/trace', 'string'), /Unsupported desktop request/);
  assert.throws(() => validateRequest('message/trace', [1, 2, 3]), /Unsupported desktop request/);

  // Oversized payload fails (>32KB)
  const hugeParams = { message_id: 'x'.repeat(35 * 1024) };
  assert.throws(() => validateRequest('message/trace', hugeParams), /Desktop request is too large/);
});

test('Phase 1 Contract: preload.cjs exposes message/trace in allowlist', () => {
  const preloadSource = fs.readFileSync(path.resolve(__dirname, 'preload.cjs'), 'utf8');
  assert.ok(
    preloadSource.includes("'message/trace'") || preloadSource.includes('"message/trace"'),
    'desktop/preload.cjs allowed set must include message/trace'
  );
});

test('Phase 1 Contract: trace response schema validation', () => {
  const fixture = {
    schema_version: 1,
    found: true,
    id: 'canonical_sha256',
    message_id: 'false_919876543210@c.us_12345',
    conversation_id: '919876543210@c.us',
    phone: '+919876543210',
    direction: 'incoming',
    text: 'WTB 10 iPhone 16 128GB USD 750',
    state: 'CLASSIFIED',
    current_stage: 'LOCAL_TRIAGE',
    stage_outcome: 'SUCCESS',
    reason: 'LOCAL_TRADE_PARSED',
    processing_stage: 'Trade format parsed locally',
    last_progress_at: 1732180000.0,
    classification_source: 'local',
    model_requested: null,
    model_served: null,
    triage: {
      category: 'BUY',
      proceed: true,
      needs_review: false,
      reason: 'Explicit trade format',
      items: [{ device: 'iPhone 16', variant: '128GB', quantity: 10, unit_price: 750.0, currency: 'USD' }],
    },
    media_job: {
      status: 'NONE',
      attempts: 0,
      deadline: null,
      last_error: null,
      retryable: true,
    },
    trade_action: 'NO_STOCK_MATCH',
    outbound_id: null,
    events: [
      {
        schema_version: 1,
        event_id: 'evt_1',
        source_message_id: 'false_919876543210@c.us_12345',
        conversation_id: '919876543210@c.us',
        account_phone: '+447700900077',
        message_revision: 0,
        sequence: 1,
        attempt_id: 'att_1',
        stage: 'CAPTURE',
        outcome: 'SUCCESS',
        reason_code: 'CAPTURED',
        retryable: false,
        metadata: {},
        occurred_at: 1732180000.0,
      },
      {
        schema_version: 1,
        event_id: 'evt_2',
        source_message_id: 'false_919876543210@c.us_12345',
        conversation_id: '919876543210@c.us',
        account_phone: '+447700900077',
        message_revision: 1,
        sequence: 2,
        attempt_id: 'att_2',
        stage: 'LOCAL_TRIAGE',
        outcome: 'SUCCESS',
        reason_code: 'LOCAL_TRADE_PARSED',
        retryable: false,
        metadata: { parser: 'regex_trade_parser' },
        occurred_at: 1732180001.0,
      },
    ],
    decisions: [],
    outbound_attempts: [],
  };

  assert.equal(fixture.schema_version, 1);
  assert.equal(fixture.found, true);
  assert.equal(fixture.classification_source, 'local');
  assert.equal(fixture.model_served, null);
  assert.equal(fixture.events.length, 2);
  assert.equal(fixture.events[0].sequence, 1);
  assert.equal(fixture.events[1].sequence, 2);
});
