'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');

function defaultAuthDir() {
  const base = process.env.LOCALAPPDATA || path.join(require('node:os').homedir(), '.jarvis');
  return path.join(base, 'JarvisTrade', 'wa-auth');
}

function jidToPhone(jid) {
  if (!jid || typeof jid !== 'string') return null;
  const [userPart, serverPart] = jid.split('@');
  if (serverPart !== 's.whatsapp.net' && serverPart !== 'c.us') return null;
  const digits = (userPart || '').split(':')[0].replace(/\D/g, '');
  return /^\d{7,15}$/.test(digits) ? `+${digits}` : null;
}

class WaDirectClient {
  constructor({ authDir = defaultAuthDir(), onState = () => {}, onMessages = () => {}, onHumanReplies = () => {} } = {}) {
    this.authDir = authDir;
    this.onState = onState;
    this.onMessages = onMessages;
    this.onHumanReplies = onHumanReplies;
    this._recentSentIds = new Map();
    this.sock = null;
    this.state = 'disconnected';
    this.qr = null;
    this.profile = null;
    this.error = '';
    this.stopping = false;
    this.connecting = null;
    this.scanSinceTs = null;
  }

  isRecentSentId(id) {
    if (!id) return false;
    const now = Date.now();
    for (const [k, ts] of this._recentSentIds.entries()) {
      if (now - ts > 180000) this._recentSentIds.delete(k);
    }
    const cleanId = String(id).trim();
    if (this._recentSentIds.has(cleanId)) return true;
    for (const k of this._recentSentIds.keys()) {
      if (cleanId.endsWith(k) || k.endsWith(cleanId)) return true;
    }
    return false;
  }

  hasSavedSession() {
    return fs.existsSync(path.join(this.authDir, 'creds.json'));
  }

  status() {
    const isConnected = this.state === 'connected';
    return {
      state: this.state,
      connected: isConnected,
      can_send: isConnected,
      can_receive: isConnected,
      qr: this.qr,
      profile: this.profile,
      error: this.error,
      has_session: this.hasSavedSession(),
    };
  }

  _emit() {
    try {
      this.onState(this.status());
    } catch {}
  }

  async connect(browserName = 'Jarvis Trade') {
    if (this.sock && this.state === 'connected') return this.status();
    if (this.connecting) return this.connecting;
    this.stopping = false;
    this.error = '';
    this.state = 'connecting';
    this._emit();

    this.connecting = (async () => {
      try {
        await fsp.mkdir(this.authDir, { recursive: true });
        const baileys = await import('@whiskeysockets/baileys');
        const makeWASocket = baileys.makeWASocket || baileys.default;
        const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, jidNormalizedUser } = baileys;
        const QRCode = require('qrcode');
        const pino = require('pino');

        const { state, saveCreds } = await useMultiFileAuthState(this.authDir);
        const versionInfo = await fetchLatestBaileysVersion().catch(() => null);
        const sock = makeWASocket({
          ...(versionInfo?.version ? { version: versionInfo.version } : {}),
          auth: state,
          logger: pino({ level: 'silent' }),
          printQRInTerminal: false,
          browser: [browserName, 'Desktop', '1.0'],
          syncFullHistory: false,
          markOnlineOnConnect: false,
        });
        this.sock = sock;

        sock.ev.on('creds.update', saveCreds);

        sock.ev.on('connection.update', async (update) => {
          if (this.sock !== sock) return;
          const { connection, lastDisconnect, qr } = update;
          if (qr) {
            try {
              this.qr = await QRCode.toDataURL(qr, { width: 220, margin: 1, color: { dark: '#0f172a', light: '#ffffff' } });
              this.state = 'qr';
              this.error = '';
              this._emit();
            } catch {}
          }
          if (connection === 'open') {
            this.qr = null;
            this.state = 'connected';
            this.error = '';
            const rawId = sock.user?.id || '';
            const normJid = typeof jidNormalizedUser === 'function' ? jidNormalizedUser(rawId) : rawId;
            const phone = jidToPhone(normJid || rawId);
            const name = String(sock.user?.name || sock.user?.verifiedName || sock.user?.notify || '').trim();
            let avatarUrl = null;
            if (normJid) {
              avatarUrl = await sock.profilePictureUrl(normJid, 'image').catch(() =>
                sock.profilePictureUrl(normJid, 'preview').catch(() => null)
              );
            }
            if (name) {
              this.profile = {
                verified: true,
                phone,
                name: name.slice(0, 100),
                avatar_url: typeof avatarUrl === 'string' && /^https:\/\//i.test(avatarUrl) ? avatarUrl : null,
                source: 'baileys_direct',
              };
            }
            this._emit();
          }
          if (connection === 'close') {
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const loggedOut = statusCode === DisconnectReason?.loggedOut;
            this.sock = null;
            this.qr = null;
            if (loggedOut) {
              this.state = 'disconnected';
              this.profile = null;
              await fsp.rm(this.authDir, { recursive: true, force: true }).catch(() => {});
              this.error = 'Signed out from phone. Pair QR again to reconnect.';
              this._emit();
            } else if (!this.stopping) {
              this.state = 'reconnecting';
              this._emit();
              setTimeout(() => {
                if (!this.stopping) this.connect(browserName).catch(() => {});
              }, 2200);
            } else {
              this.state = 'disconnected';
              this._emit();
            }
          }
        });

        sock.ev.on('messages.upsert', async ({ messages, type }) => {
          if (this.sock !== sock) return;
          if ((type !== 'notify' && type !== 'append') || !Array.isArray(messages)) return;
          const clean = [];
          const humanReplies = [];
          for (const msg of messages) {
            if (!msg || !msg.message) continue;
            const rawTs = Number(msg.messageTimestamp?.low ?? msg.messageTimestamp ?? 0);
            const msgTs = rawTs > 1e12 ? Math.floor(rawTs / 1000) : rawTs;
            if (type === 'append' && !msgTs) continue;
            if (typeof this.scanSinceTs === 'number' && this.scanSinceTs > 0 && msgTs > 0 && msgTs < this.scanSinceTs) continue;
            const jid = msg.key?.remoteJid || '';
            if (!jid || jid === 'status@broadcast') continue;

            const rawId = msg.key?.id || '';
            if (msg.key?.fromMe) {
              if (this.isRecentSentId(rawId)) {
                // Bot's own echo - ignore to prevent muting bot's own chat
                continue;
              }
              // Human sent manual reply from phone or desktop WhatsApp
              humanReplies.push({
                conversation_id: jid,
                message_id: rawId,
                timestamp: msgTs,
                fromMe: true,
              });
              continue;
            }

            const text =
              msg.message.conversation ||
              msg.message.extendedTextMessage?.text ||
              msg.message.imageMessage?.caption ||
              msg.message.videoMessage?.caption ||
              '';
            if (!text || !text.trim()) continue;
            const isGroup = jid.endsWith('@g.us');
            const senderJid = isGroup ? (msg.key.participant || '') : jid;
            const phone = jidToPhone(senderJid);
            const senderName = String(msg.pushName || '').trim().slice(0, 160);
            const msgId = `false_${jid}_${msg.key.id || Date.now()}` + (isGroup && senderJid ? `_${senderJid}` : '');
            clean.push({
              conversation_id: jid,
              sender_id: senderJid || jid,
              phone,
              sender_name: senderName,
              chat_name: isGroup ? '' : senderName,
              chat_kind: isGroup ? 'group' : 'direct',
              message_id: msgId,
              text: text.trim().slice(0, 12000),
              ...(msgTs > 0 ? { timestamp: msgTs } : {}),
              identity_verified: Boolean(phone && (senderJid.endsWith('@s.whatsapp.net') || senderJid.endsWith('@c.us'))),
            });
          }
          if (humanReplies.length && typeof this.onHumanReplies === 'function') {
            try { this.onHumanReplies(humanReplies); } catch {}
          }
          if (clean.length) {
            try { this.onMessages(clean); } catch {}
          }
        });

        return this.status();
      } catch (err) {
        this.state = 'error';
        this.error = String(err?.message || err);
        this._emit();
        return this.status();
      } finally {
        this.connecting = null;
      }
    })();

    return this.connecting;
  }

  async sendDirect(jid, textParts = [], attachment = null) {
    if (!this.sock || this.state !== 'connected') {
      return { ok: false, reason: 'DIRECT_WA_DISCONNECTED' };
    }
    try {
      let buf = null;
      if (Buffer.isBuffer(attachment?.buffer)) {
        buf = attachment.buffer;
      } else if (attachment?.dataUrl) {
        const b64 = String(attachment.dataUrl).split(',')[1] || '';
        buf = Buffer.from(b64, 'base64');
      }

      if (buf) {
        const payload = attachment.type === 'document'
          ? { document: buf, mimetype: attachment.mime || 'application/octet-stream', fileName: attachment.name || 'file.bin', caption: attachment.caption || '' }
          : { image: buf, caption: attachment.caption || textParts.join('\n') };
        const receipt = await this.sock.sendMessage(jid, payload);
        const msgId = receipt?.key?.id;
        if (!msgId) {
          return { ok: false, ambiguous: true, reason: 'MISSING_PROVIDER_RECEIPT' };
        }
        this._recentSentIds.set(msgId, Date.now());
        return { ok: true, via: 'baileys_media', sent_count: 1, sent_ids: [msgId] };
      }
      const parts = Array.isArray(textParts) ? textParts.filter((t) => String(t || '').trim()) : [];
      const sentIds = [];
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        let receipt;
        try {
          receipt = await this.sock.sendMessage(jid, { text: String(part) });
        } catch (err) {
          return {
            ok: false,
            ambiguous: true,
            sent_ids: sentIds,
            parts_sent: sentIds.length,
            total_parts: parts.length,
            reason: String(err?.message || err),
          };
        }
        const msgId = receipt?.key?.id;
        if (!msgId) {
          return {
            ok: false,
            ambiguous: true,
            sent_ids: sentIds,
            parts_sent: sentIds.length,
            total_parts: parts.length,
            reason: 'MISSING_PROVIDER_RECEIPT',
          };
        }
        this._recentSentIds.set(msgId, Date.now());
        sentIds.push(msgId);
        if (i < parts.length - 1) await new Promise((r) => setTimeout(r, 800));
      }
      return { ok: true, via: 'baileys_text', sent_count: parts.length, sent_ids: sentIds };
    } catch (err) {
      return { ok: false, ambiguous: true, reason: String(err?.message || err) };
    }
  }

  async disconnect({ wipe = false } = {}) {
    this.stopping = true;
    this.qr = null;
    const sock = this.sock;
    this.sock = null;
    if (sock) {
      try {
        if (wipe && typeof sock.logout === 'function') await sock.logout().catch(() => {});
        if (typeof sock.end === 'function') sock.end(undefined);
      } catch {}
    }
    this.state = 'disconnected';
    if (wipe) {
      this.profile = null;
      this.scanSinceTs = null;
      await fsp.rm(this.authDir, { recursive: true, force: true }).catch(() => {});
    }
    this._emit();
    return this.status();
  }
}

module.exports = { WaDirectClient, defaultAuthDir, jidToPhone };
