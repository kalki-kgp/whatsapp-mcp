import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  proto,
  useMultiFileAuthState,
  WASocket,
  makeCacheableSignalKeyStore,
  fetchLatestWaWebVersion,
} from "@whiskeysockets/baileys";
import express, { Request, Response } from "express";
import pino from "pino";
import * as QRCode from "qrcode";
import { Boom } from "@hapi/boom";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = join(__dirname, "..", "auth_info");
const PORT = parseInt(process.env.BRIDGE_PORT || "3010", 10);

const logger = pino({ level: process.env.LOG_LEVEL || "warn" });

// --- State ---
let sock: WASocket | null = null;
let qrDataURL: string | null = null;
let connectionStatus: "disconnected" | "qr_pending" | "connected" = "disconnected";
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 10;

// --- Incoming message buffer ---
interface IncomingMessage {
  id: string;
  chatJid: string;
  senderJid: string;
  pushName: string | null;
  text: string | null;
  messageType: string;
  timestamp: number; // unix seconds
  isGroup: boolean;
  durationSeconds?: number | null;
  mimetype?: string | null;
}

const incomingMessages: IncomingMessage[] = [];
const MAX_INCOMING_BUFFER = 200;

interface RecentMediaMessage {
  key: string;
  id: string;
  chatJid: string;
  senderJid: string;
  pushName: string | null;
  timestamp: number;
  messageType: "voice_note" | "audio";
  durationSeconds: number | null;
  mimetype: string | null;
  rawMessage: proto.IWebMessageInfo;
}

const recentMediaMessages = new Map<string, RecentMediaMessage>();
const MAX_MEDIA_BUFFER = 100;

function makeMediaLookupKey(chatJid: string, participantJid: string | undefined, messageId: string): string {
  return `${chatJid}::${participantJid || ""}::${messageId}`;
}

function getSingleQueryParam(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function findRecentMediaMessage(
  messageId: string,
  chatJid?: string,
  participantJid?: string
): RecentMediaMessage | null {
  if (chatJid) {
    const directKey = makeMediaLookupKey(chatJid, participantJid, messageId);
    const directMatch = recentMediaMessages.get(directKey);
    if (directMatch) {
      return directMatch;
    }
  }

  for (const item of recentMediaMessages.values()) {
    if (item.id !== messageId) {
      continue;
    }
    if (chatJid && item.chatJid !== chatJid) {
      continue;
    }
    if (participantJid && item.senderJid !== participantJid) {
      continue;
    }
    return item;
  }

  return null;
}

function rememberRecentMediaMessage(entry: RecentMediaMessage): void {
  recentMediaMessages.delete(entry.key);
  recentMediaMessages.set(entry.key, entry);

  while (recentMediaMessages.size > MAX_MEDIA_BUFFER) {
    const oldestKey = recentMediaMessages.keys().next().value;
    if (!oldestKey) {
      break;
    }
    recentMediaMessages.delete(oldestKey);
  }
}

function unwrapMessageContent(
  message: proto.IMessage | null | undefined
): proto.IMessage | null | undefined {
  let current = message;
  while (current) {
    if (current.ephemeralMessage?.message) {
      current = current.ephemeralMessage.message;
      continue;
    }
    if (current.viewOnceMessage?.message) {
      current = current.viewOnceMessage.message;
      continue;
    }
    if (current.viewOnceMessageV2?.message) {
      current = current.viewOnceMessageV2.message;
      continue;
    }
    if (current.viewOnceMessageV2Extension?.message) {
      current = current.viewOnceMessageV2Extension.message;
      continue;
    }
    if (current.documentWithCaptionMessage?.message) {
      current = current.documentWithCaptionMessage.message;
      continue;
    }
    break;
  }
  return current;
}

// --- Baileys connection ---
async function connectToWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // Fetch the latest WhatsApp web version to avoid 405 rejections
  const { version, isLatest } = await fetchLatestWaWebVersion({});
  console.log(`[bridge] Using WA web version ${version.join(".")}, isLatest: ${isLatest}`);

  sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    version,
    logger,
    browser: ["WhatsApp Assistant", "Chrome", "130.0.0"],
  });

  sock.ev.on("creds.update", saveCreds);

  // Listen for incoming messages
  sock.ev.on("messages.upsert", ({ messages: msgs, type }) => {
    if (type !== "notify") return; // only real-time messages, not history sync
    for (const msg of msgs) {
      if (!msg.message) continue;
      if (msg.key.fromMe) continue; // skip our own messages

      const chatJid = msg.key.remoteJid || "";
      const isGroup = chatJid.endsWith("@g.us");
      const senderJid = isGroup ? (msg.key.participant || "") : chatJid;

      // Extract text content
      let text: string | null = null;
      let messageType = "unknown";
      const m = unwrapMessageContent(msg.message);
      if (!m) {
        continue;
      }
      if (m.conversation) {
        text = m.conversation;
        messageType = "text";
      } else if (m.extendedTextMessage?.text) {
        text = m.extendedTextMessage.text;
        messageType = "text";
      } else if (m.imageMessage) {
        text = m.imageMessage.caption || null;
        messageType = "image";
      } else if (m.videoMessage) {
        text = m.videoMessage.caption || null;
        messageType = "video";
      } else if (m.documentMessage) {
        text = m.documentMessage.fileName || null;
        messageType = "document";
      } else if (m.audioMessage) {
        messageType = m.audioMessage.ptt ? "voice_note" : "audio";
      } else if (m.stickerMessage) {
        messageType = "sticker";
      } else if (m.contactMessage) {
        text = m.contactMessage.displayName || null;
        messageType = "contact";
      } else if (m.locationMessage) {
        messageType = "location";
      }

      const incoming: IncomingMessage = {
        id: msg.key.id || "",
        chatJid,
        senderJid,
        pushName: msg.pushName || null,
        text,
        messageType,
        timestamp: typeof msg.messageTimestamp === "number"
          ? msg.messageTimestamp
          : Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
        isGroup,
        durationSeconds: m.audioMessage?.seconds || null,
        mimetype: m.audioMessage?.mimetype || null,
      };

      incomingMessages.push(incoming);
      // Trim buffer
      if (incomingMessages.length > MAX_INCOMING_BUFFER) {
        incomingMessages.splice(0, incomingMessages.length - MAX_INCOMING_BUFFER);
      }

      if ((messageType === "voice_note" || messageType === "audio") && msg.key.id) {
        const key = makeMediaLookupKey(chatJid, senderJid || undefined, msg.key.id);
        rememberRecentMediaMessage({
          key,
          id: msg.key.id,
          chatJid,
          senderJid,
          pushName: msg.pushName || null,
          timestamp: incoming.timestamp,
          messageType: messageType as "voice_note" | "audio",
          durationSeconds: m.audioMessage?.seconds || null,
          mimetype: m.audioMessage?.mimetype || null,
          rawMessage: msg,
        });
      }

      const preview = text ? (text.length > 50 ? text.slice(0, 50) + "..." : text) : `[${messageType}]`;
      console.log(`[bridge] Incoming from ${msg.pushName || senderJid}: ${preview}`);
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Generate QR for HTTP API
      try {
        qrDataURL = await QRCode.toDataURL(qr, { width: 300, margin: 2 });
      } catch {
        qrDataURL = null;
      }
      // Print QR to terminal
      try {
        const terminalQR = await QRCode.toString(qr, { type: "terminal", small: true });
        console.log("\n" + terminalQR);
      } catch {
        // fall through
      }
      connectionStatus = "qr_pending";
      console.log("[bridge] QR code generated — scan with your phone");
    }

    if (connection === "open") {
      connectionStatus = "connected";
      qrDataURL = null;
      reconnectAttempts = 0;
      console.log("[bridge] Connected to WhatsApp");
    }

    if (connection === "close") {
      connectionStatus = "disconnected";
      qrDataURL = null;

      const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      if (shouldReconnect && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts++;
        const delay = Math.min(1000 * 2 ** reconnectAttempts, 60000);
        console.log(
          `[bridge] Disconnected (code ${statusCode}). Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`
        );
        setTimeout(connectToWhatsApp, delay);
      } else if (statusCode === DisconnectReason.loggedOut) {
        console.log("[bridge] Logged out. Delete auth_info/ and restart to re-authenticate.");
      } else {
        console.log("[bridge] Max reconnect attempts reached. Restart the bridge manually.");
      }
    }
  });
}

// --- Express API ---
const app = express();
app.use(express.json());

app.get("/api/status", (_req: Request, res: Response) => {
  res.json({ status: connectionStatus });
});

app.get("/api/qr", (_req: Request, res: Response) => {
  if (connectionStatus === "qr_pending" && qrDataURL) {
    res.json({ qr: qrDataURL });
  } else if (connectionStatus === "connected") {
    res.json({ message: "Already connected", status: "connected" });
  } else {
    res.json({ message: "No QR code available", status: connectionStatus });
  }
});

app.get("/api/incoming", (req: Request, res: Response) => {
  const since = parseInt(req.query.since as string, 10) || 0;
  const filtered = incomingMessages.filter((m) => m.timestamp > since);
  res.json({
    messages: filtered,
    count: filtered.length,
    latest_timestamp: filtered.length > 0 ? filtered[filtered.length - 1].timestamp : since,
  });
});

app.get("/api/messages/:messageId", (req: Request, res: Response) => {
  const messageId = getSingleQueryParam(req.params.messageId);
  const chatJid = getSingleQueryParam(req.query.chatJid);
  const participantJid = getSingleQueryParam(req.query.participantJid);
  if (!messageId) {
    res.status(400).json({ error: "Missing message ID" });
    return;
  }
  const mediaMessage = findRecentMediaMessage(messageId, chatJid, participantJid);

  if (!mediaMessage) {
    res.status(404).json({ error: "Recent media message not found" });
    return;
  }

  res.json({
    message: {
      id: mediaMessage.id,
      chatJid: mediaMessage.chatJid,
      senderJid: mediaMessage.senderJid,
      pushName: mediaMessage.pushName,
      timestamp: mediaMessage.timestamp,
      messageType: mediaMessage.messageType,
      durationSeconds: mediaMessage.durationSeconds,
      mimetype: mediaMessage.mimetype,
    },
  });
});

app.get("/api/messages/:messageId/media", async (req: Request, res: Response) => {
  const messageId = getSingleQueryParam(req.params.messageId);
  const chatJid = getSingleQueryParam(req.query.chatJid);
  const participantJid = getSingleQueryParam(req.query.participantJid);
  if (!messageId) {
    res.status(400).json({ error: "Missing message ID" });
    return;
  }
  const mediaMessage = findRecentMediaMessage(messageId, chatJid, participantJid);

  if (!mediaMessage) {
    res.status(404).json({ error: "Recent media message not found" });
    return;
  }

  if (!sock) {
    res.status(503).json({ error: "WhatsApp bridge is not connected" });
    return;
  }

  try {
    const mediaBuffer = await downloadMediaMessage(
      mediaMessage.rawMessage,
      "buffer",
      {},
      { logger, reuploadRequest: sock.updateMediaMessage }
    );

    res.setHeader("Content-Type", mediaMessage.mimetype || "audio/ogg");
    res.setHeader(
      "Content-Disposition",
      `inline; filename="${mediaMessage.id}.${mediaMessage.messageType === "voice_note" ? "ogg" : "bin"}"`
    );
    res.send(mediaBuffer);
  } catch (err: unknown) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    console.error(`[bridge] Failed to download media for ${messageId}:`, errorMsg);
    res.status(500).json({ error: `Failed to download media: ${errorMsg}` });
  }
});

app.post("/api/send", async (req: Request, res: Response) => {
  const { recipient, message } = req.body;

  if (!recipient || !message) {
    res.status(400).json({ error: "Missing 'recipient' or 'message' in request body" });
    return;
  }

  if (connectionStatus !== "connected" || !sock) {
    res.status(503).json({ error: "WhatsApp bridge is not connected", status: connectionStatus });
    return;
  }

  // Normalize recipient JID
  let jid = recipient;
  if (!jid.includes("@")) {
    jid = jid.replace(/[^0-9]/g, "") + "@s.whatsapp.net";
  }

  try {
    const sent = await sock.sendMessage(jid, { text: message });
    console.log(`[bridge] Message sent to ${jid}`);
    res.json({
      success: true,
      recipient: jid,
      message_id: sent?.key?.id || null,
    });
  } catch (err: unknown) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    console.error(`[bridge] Failed to send message to ${jid}:`, errorMsg);
    res.status(500).json({ error: `Failed to send message: ${errorMsg}` });
  }
});

// --- Start ---
app.listen(PORT, () => {
  console.log(`[bridge] WhatsApp bridge listening on http://localhost:${PORT}`);
  connectToWhatsApp();
});
